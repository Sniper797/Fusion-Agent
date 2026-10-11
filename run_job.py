#!/usr/bin/env python3
"""Send a job to the FusionAgent add-in running inside Autodesk Fusion and print the result.

    python run_job.py ping                          is the add-in alive? which documents are open?
    python run_job.py SCRIPT.py                     run a script inside Fusion
    python run_job.py SCRIPT.py --args "{\"k\": 1}"   arguments; the script sees them as `args`
    python run_job.py SCRIPT.py --args-file a.json
    python run_job.py SCRIPT.py --doc "Car"         run on that open document, not the active one
    python run_job.py -c "print(design().rootComponent.name)"   code without a file
    python run_job.py -e "design().timeline.count"              one expression, returned as out["value"]
    python run_job.py SCRIPT.py --timeout 300 --save result.json
    python run_job.py SCRIPT.py --read-only         fail if the job changed anything (nothing is undone)
    python run_job.py doctor                        check the whole chain and say what to fix

Before sending, the script is checked without contacting Fusion. Patterns that have
frozen Fusion before are refused (exit 5); --allow RULE or --no-preflight overrides.

With add-in 1.2.0+ jobs wait in line instead of failing as busy, and every result
lists what changed in the active and the pinned document (--no-receipt skips it).

Exit code: 0 done, 1 the script raised (traceback printed), 2 agent not running
or bad input (including a --doc that matches no open document), 3 agent busy
(add-ins older than 1.2.0), 4 timed out, 5 refused by the pre-flight check,
6 a --read-only job changed something.
Standard library only.
"""
import argparse
import ast
import json
import os
import re
import sys
import time
import uuid

VERSION = "1.2.0"
NEEDS_FOR_DOC = (1, 1, 0)
NEEDS_FOR_QUEUE = (1, 2, 0)
KEEP_SECONDS = 2 * 24 * 3600        # inline scripts and saved results older than this are swept

# ------------------------------------------------------------- pre-flight check

DATA_COLLECTIONS = {"dataHubs", "dataProjects", "dataFolders", "dataFiles"}
LOOKUPS = {"itemByName", "itemById"}
MODAL_UI = {"messageBox", "inputBox", "createFileDialog", "createFolderDialog", "selectEntity"}
ACTIVE_NAMES = {"activeProduct", "activeDocument", "activeViewport", "activeSelections"}
ITERATING = {"list", "tuple", "set", "sorted", "reversed", "map", "filter", "enumerate", "zip",
             "sum", "any", "all", "max", "min", "iter"}
LOOPS = (ast.For, ast.AsyncFor, ast.While, ast.comprehension,
         ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

REFUSE = {
    "cloud-walk": "{what}. Walking cloud folders froze Fusion for 9 and about 40 minutes. Open the file "
                  "in Fusion first, or go straight to it with dataFolders.itemByName(...) one level at a time.",
    "compute-all": "calls computeAll(). It re-solved every joint to other values.",
    "modal-ui": "calls {attr}(). It waits for a click or a pick in Fusion and holds every job behind it; "
                "return results in `out` instead.",
}


def preflight(code, filename="<code>", pinned=False):
    """Return (refusals, warnings), each a list of (rule, line, message). Heuristic: it reads
    the code, it does not run it, so it can miss things and it can be overridden."""
    tree = ast.parse(code, filename)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def in_loop(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, LOOPS):
                return True
        return False

    def collection(node):
        """A cloud-folder collection read other than by name: x.dataFolders, not x.dataFolders.itemByName(...)."""
        if not (isinstance(node, ast.Attribute) and node.attr in DATA_COLLECTIONS):
            return False
        parent = parents.get(node)
        return not (isinstance(parent, ast.Attribute) and parent.attr in LOOKUPS)

    # Names bound to a collection: files = folder.dataFiles, arr = folder.dataFolders.asArray(), b = files.
    aliases = set()

    def is_collection_value(v):
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "asArray":
            v = v.func.value
        return collection(v) or (isinstance(v, ast.Name) and v.id in aliases)

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None \
                    and is_collection_value(node.value):
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    if isinstance(target, ast.Name) and target.id not in aliases:
                        aliases.add(target.id)
                        changed = True

    def reaches(node):
        return any(collection(n) or (isinstance(n, ast.Name) and n.id in aliases) for n in ast.walk(node))

    # Functions by name (methods too), which of them read collections, and which recurse
    # (directly, through self.name, or in a cycle with other functions).
    funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(node.name, []).append(node)

    def called_name(call):
        f = call.func
        return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None

    calls = {name: {called_name(c) for d in defs for c in ast.walk(d) if isinstance(c, ast.Call)} & set(funcs)
             for name, defs in funcs.items()}
    reading = {name for name, defs in funcs.items() if any(reaches(d) for d in defs)}
    changed = True
    while changed:                           # a function that calls a reading function reads too
        changed = False
        for name in funcs:
            if name not in reading and calls[name] & reading:
                reading.add(name)
                changed = True

    def in_cycle(name):
        seen, stack = set(), list(calls.get(name, ()))
        while stack:
            n = stack.pop()
            if n == name:
                return True
            if n not in seen:
                seen.add(n)
                stack.extend(calls.get(n, ()))
        return False

    recursive_readers = {n for n in reading if in_cycle(n)}

    refusals, warnings = [], []

    def walk_refusal(line, what):
        refusals.append(("cloud-walk", line, REFUSE["cloud-walk"].format(what=what)))

    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Attribute):
            if collection(node) and in_loop(node):
                walk_refusal(line, "reads %s inside a loop" % node.attr)
            elif node.attr == "computeAll":
                refusals.append(("compute-all", line, REFUSE["compute-all"]))
            elif node.attr in MODAL_UI:
                refusals.append(("modal-ui", line, REFUSE["modal-ui"].format(attr=node.attr)))
            elif node.attr in ("allJoints", "allAsBuiltJoints"):
                warnings.append(("all-joints", line, "%s returned 0 of 111 joints on a direct design; "
                                 "count them per component with component.joints." % node.attr))
            elif pinned and node.attr in ACTIVE_NAMES:
                warnings.append(("active-with-doc", line, "uses %s, which is the ACTIVE document, not the one "
                                 "--doc pins. Use design() and doc instead." % node.attr))
        elif isinstance(node, ast.Name) and node.id in aliases and isinstance(node.ctx, ast.Load) and in_loop(node):
            walk_refusal(line, "uses %s, a cloud-folder collection, inside a loop" % node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in recursive_readers:
            walk_refusal(line, "%s() reads cloud folders and calls itself (directly or through another "
                               "function)" % node.name)
        if isinstance(node, ast.Call):
            name = called_name(node)
            if name in reading and in_loop(node):
                walk_refusal(line, "calls %s(), which reads cloud folders, inside a loop" % name)
            if isinstance(node.func, ast.Name) and node.func.id in ITERATING and any(reaches(a) for a in node.args):
                walk_refusal(line, "%s() runs over a cloud-folder collection" % node.func.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr, args = node.func.attr, node.args
            owner = node.func.value
            if attr == "open" and isinstance(owner, ast.Attribute) and owner.attr == "documents":
                visible = args[1] if len(args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "visible"), None)
                if not (isinstance(visible, ast.Constant) and visible.value is False):
                    warnings.append(("visible-open", line, "documents.open(...) without False opens a visible "
                                     "tab (41 s once) and makes it active. Use documents.open(file, False) "
                                     "to read it hidden, then close(False)."))
            elif attr in ("save", "saveAs"):
                warnings.append(("save", line, "%s(): saving the user's document is their call; "
                                 "the add-in never saves on its own." % attr))
            elif attr == "close" and args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, bool):
                if args[0].value:
                    warnings.append(("close", line, "close(True) saves the document before closing it."))
                else:
                    warnings.append(("close", line, "close(False) throws away unsaved changes. Use it only on a "
                                     "document this script opened itself."))
            elif attr == "add" and isinstance(owner, ast.Attribute) and owner.attr == "documents" and pinned:
                warnings.append(("active-with-doc", line, "makes a new document, but design() still follows the "
                                 "pinned one. Use the new document's own design."))
    def once_per_line(findings):
        seen = {}
        for f in sorted(findings, key=lambda f: (f[1], f[0], f[2])):
            seen.setdefault((f[0], f[1]), f)
        return list(seen.values())

    return once_per_line(refusals), once_per_line(warnings)


def fix_bash_paths(value, notes):
    """Git Bash paths (/c/Users/...) fail silently inside Fusion; rewrite them to C:/Users/..."""
    if isinstance(value, dict):
        return {k: fix_bash_paths(v, notes) for k, v in value.items()}
    if isinstance(value, list):
        return [fix_bash_paths(v, notes) for v in value]
    if isinstance(value, str):
        m = re.match(r"^/([a-zA-Z])/(.*)$", value)
        if m:
            fixed = "%s:/%s" % (m.group(1).upper(), m.group(2))
            notes.append("%s -> %s" % (value, fixed))
            return fixed
    return value


# ---------------------------------------------------------- windows that block

def fusion_windows(pid):
    """Visible top-level windows of the Fusion process. Windows only; no Fusion API."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    H = wintypes.HWND                        # declared, or 64-bit handles get squeezed into a C int
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, H, wintypes.LPARAM)
    for fn, argtypes, restype in (
            ("EnumWindows", [proto, wintypes.LPARAM], wintypes.BOOL),
            ("GetWindowThreadProcessId", [H, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            ("IsWindowVisible", [H], wintypes.BOOL),
            ("IsWindowEnabled", [H], wintypes.BOOL),
            ("GetWindowTextLengthW", [H], ctypes.c_int),
            ("GetWindowTextW", [H, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            ("GetWindowRect", [H, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            ("GetWindow", [H, wintypes.UINT], H)):
        getattr(user32, fn).argtypes = argtypes
        getattr(user32, fn).restype = restype
    found = []

    def visit(hwnd, _):
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value == pid and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, title, n + 1)
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            found.append({"title": title.value, "enabled": bool(user32.IsWindowEnabled(hwnd)),
                          "owner": user32.GetWindow(hwnd, 4) or 0,          # GW_OWNER
                          "area": (r.right - r.left) * (r.bottom - r.top)})
        return True

    user32.EnumWindows(proto(visit), 0)
    return found


def blocking_windows(pid):
    """Titles of the windows holding Fusion's job queue: [] when nothing blocks, None when
    it cannot tell (not Windows, no pid). A modal window disables Fusion's main window."""
    if os.name != "nt" or not pid:
        return None
    try:
        wins = fusion_windows(pid)
    except Exception:
        return None
    tops = sorted((w for w in wins if not w["owner"]), key=lambda w: -w["area"])
    if not tops:
        return None
    main = tops[0]
    if main["enabled"]:
        return []
    names = [w["title"] for w in wins
             if w is not main and w["enabled"] and w["title"] and w["title"] != "Fusion360"]
    return names or ["(a window with no title)"]


def say_blocked(names):
    return ("Fusion has %s open, which holds every job until it is closed: %s. "
            "Close it in Fusion and the job starts by itself."
            % ("a window" if len(names) == 1 else "windows", ", ".join("'%s'" % n for n in names)))


# ------------------------------------------------------------------ the result

HINTS = [
    (r"InternalValidationError", "Fusion could not compute this for a damaged or open body or face. "
                                 "Wrap the call in try_value(lambda: ..., None)."),
    (r"3 : invalid argument value", "possible cause: a matrix typed from rounded numbers. Build it inside "
                                    "Fusion from transform2.asArray() instead."),
    (r"only supported in parametric", "this is a direct design (no timeline). Features that need history are "
                                      "refused; transforms hold positions by themselves."),
    (r"Invalid entity", "possible cause: a native body moved inside its own component. Use "
                        "root.features.moveFeatures with the body's proxy and axis.createForAssemblyContext(occ)."),
    (r"Can't resolve some component positions", "captured positions predate a joint change. With the user's "
                                                "agreement delete them, then call design.snapshots.add()."),
    (r"No active Fusion design", "open or activate a design, or pin the job with --doc NAME."),
    (r"No open document named|open documents match", "see the open documents with: python run_job.py ping"),
    (r"name 'doc' is not defined", "the installed add-in is older than 1.1.0: run install-windows.bat, then "
                                   "stop and run FusionAgent in Fusion (Shift+S)."),
]


def hints_for(text):
    return [h for pattern, h in HINTS if re.search(pattern, text or "")]


def capped(text, limit, keep="head"):
    """At most `limit` characters: the start for results, the end ("tail") for printed
    progress, where the latest lines matter most."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    return (text[-limit:] if keep == "tail" else text[:limit]), True


def describe(value):
    """A short map of what `out` holds, for when it is too long to print."""
    def size(v):
        if isinstance(v, (list, dict, str)):
            return "%s of %d" % (type(v).__name__, len(v))
        return type(v).__name__
    return {k: size(v) for k, v in value.items()} if isinstance(value, dict) else size(value)


# ---------------------------------------------------------------------- client

def agent_dir():
    """Must match the add-in: FUSION_AGENT_DIR, else the per-user default."""
    if os.environ.get("FUSION_AGENT_DIR"):
        return os.environ["FUSION_AGENT_DIR"]
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "FusionAgent")


def read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def version_tuple(text):
    try:
        return tuple(int(x) for x in str(text).split(".")[:3])
    except ValueError:
        return (0, 0, 0)


def sweep(folder):
    try:
        now = time.time()
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            if os.path.isfile(p) and now - os.path.getmtime(p) > KEEP_SECONDS:
                os.remove(p)
    except OSError:
        pass


def addins_dir():
    """Where Fusion looks for add-ins on this computer."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns")
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                        "Autodesk", "Autodesk Fusion 360", "API", "AddIns")


def package_addin():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "FusionAgent", "FusionAgent.py")


def file_sha(path):
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return None


def file_version(path):
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r'^VERSION = "([^"]+)"', f.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def queue_dir(d):
    return os.path.join(d, "queue")


def jobs_ahead(d, mine=None):
    """Jobs that run before ours: the one running now, plus older queued ones."""
    try:
        names = sorted(n for n in os.listdir(queue_dir(d)) if n.endswith(".json"))
    except OSError:
        names = []
    ahead = [n for n in names if mine is None or n < os.path.basename(mine)]
    busy = os.path.exists(os.path.join(d, "job.running")) or os.path.exists(os.path.join(d, "job.json"))
    return len(ahead) + (1 if busy else 0)


def submit(d, job, queued):
    """Queue the job (add-in 1.2.0+), or use the single job.json slot older add-ins read."""
    if queued:
        os.makedirs(queue_dir(d), exist_ok=True)
        path = os.path.join(queue_dir(d), "%020d-%s.json" % (time.time_ns(), job["id"]))
    else:
        path = os.path.join(d, "job.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def wait_for(d, job, timeout, pid, quiet=False):
    """Poll for the job's result every 50 ms. Returns (result or None, finished?)."""
    final = os.path.join(d, "results", job["id"] + ".json")
    live = os.path.join(d, "result.json")
    t0, last, seen_mtime, warned = time.time(), None, None, False
    while time.time() - t0 < timeout:
        time.sleep(0.05)
        if os.path.exists(final):
            r = read_json(final)
            if r and r.get("status") in ("done", "error"):
                return r, True
        try:
            mtime = os.stat(live).st_mtime_ns
        except OSError:
            mtime = None
        if mtime != seen_mtime:              # parse only when the file changed
            seen_mtime = mtime
            r = read_json(live)
            if r and r.get("id") == job["id"]:
                last = r
                if r.get("status") in ("done", "error"):
                    return r, True
        if not warned and last is None and time.time() - t0 > 3:
            warned = True                    # not started after 3 s: why?
            blocked = blocking_windows(pid)
            if blocked:
                print(say_blocked(blocked), flush=True)
            elif not quiet and job.get("queue_file") and os.path.exists(job["queue_file"]):
                print("waiting in line: %d job(s) ahead" % jobs_ahead(d, job["queue_file"]), flush=True)
    return last, False


def print_changes(changes):
    if changes is None:
        return
    if not changes:
        print("changes: none seen (open documents, modified flag, timeline, components, bodies, parameters)")
        return
    print("--- changes ---")
    for key in ("opened", "closed"):
        if changes.get(key):
            print("%s: %s" % (key, ", ".join(changes[key])))
    if changes.get("active"):
        print("active document: %s -> %s" % tuple(changes["active"]))
    for name, delta in (changes.get("documents") or {}).items():
        parts = []
        for field, v in delta.items():
            if field == "parameters":
                parts.append("parameters added %s, removed %s" % (v.get("added"), v.get("removed")))
            else:
                parts.append("%s %s -> %s" % (field, v[0], v[1]))
        print("%s: %s" % (name, "; ".join(parts)))


def doctor(d):
    """Check the whole chain and say what to do. Exit 0 when nothing failed."""
    fails = []

    def say(level, msg):
        print("%-5s %s" % (level, msg))
        if level == "FAIL":
            fails.append(msg)

    say("ok" if sys.version_info >= (3, 8) else "FAIL", "Python %s (needs 3.8+)" % sys.version.split()[0])
    synced = [s for s in ("OneDrive", "Dropbox", "iCloud", "Google Drive") if s.lower() in d.lower()]
    say("WARN" if synced else "ok", "job folder %s%s" % (d, " is inside %s: keep it private" % synced[0] if synced else ""))

    package, installed = package_addin(), os.path.join(addins_dir(), "FusionAgent", "FusionAgent.py")
    pv, iv = file_version(package), file_version(installed)
    ps, ins = file_sha(package), file_sha(installed)
    say("ok", "this package: client %s, add-in %s" % (VERSION, pv or "?"))
    if not os.path.isfile(installed):
        say("FAIL", "not installed in %s: run python install.py" % os.path.dirname(installed))
    elif ps and ins != ps:
        say("WARN", "installed add-in %s differs from this package's %s: run python install.py, then restart "
                    "FusionAgent (Shift+S: stop, run)" % (iv, pv))
    else:
        say("ok", "installed add-in %s matches this package" % iv)

    beat = read_json(os.path.join(d, "alive.json"))
    if not beat:
        say("FAIL", "no heartbeat: Fusion is closed or FusionAgent is not running (Shift+S, find FusionAgent, run)")
        return 2
    age = time.time() - beat.get("t", 0)
    if age > 5:
        say("FAIL", "heartbeat is %.0f s old: FusionAgent stopped, or Fusion is frozen" % age)
        blocked = blocking_windows(beat.get("pid"))
        if blocked:
            say("FAIL", say_blocked(blocked))
        return 2
    say("ok", "FusionAgent %s running in Fusion (pid %s), heartbeat %.1f s ago"
        % (beat.get("agent"), beat.get("pid"), age))
    running_v = beat.get("agent")
    if beat.get("sha") and ins and beat["sha"] != ins:
        if version_tuple(running_v) < version_tuple(iv):
            say("WARN", "Fusion runs %s but %s is installed: restart FusionAgent (Shift+S: stop, run)"
                % (running_v, iv))
        else:
            say("WARN", "Fusion runs a different copy (%s) than the installed file (%s), loaded from %s"
                % (running_v, iv, beat.get("file") or "outside the add-ins folder"))
    elif not beat.get("sha") and iv and version_tuple(running_v) < version_tuple(iv):
        say("WARN", "Fusion runs %s but %s is installed: restart FusionAgent (Shift+S: stop, run)" % (running_v, iv))

    blocked = blocking_windows(beat.get("pid"))
    if blocked:
        say("FAIL", say_blocked(blocked))
    running = os.path.join(d, "job.running")
    if os.path.exists(running):
        held = time.time() - os.path.getmtime(running)
        say("WARN" if held > 60 else "ok", "a job has been running for %.0f s%s" % (
            held, ": a long job, or a window holds it" if held > 60 else ""))
    waiting = jobs_ahead(d)
    if waiting:
        say("ok", "%d job(s) in line" % waiting)
    abandoned = os.path.join(d, "job.abandoned")
    if os.path.exists(abandoned):
        hours = (time.time() - os.path.getmtime(abandoned)) / 3600
        say("note", "a job was cut off when Fusion closed or crashed, %.1f hours ago (job.abandoned)" % hours)
    if fails:
        return 2

    job = {"id": uuid.uuid4().hex, "action": "ping", "caller": {"cwd": os.getcwd(), "pid": os.getpid()}}
    queued = version_tuple(beat.get("agent")) >= NEEDS_FOR_QUEUE
    if not queued and waiting:
        say("WARN", "skipped the test job: the add-in is busy and too old to queue")
        return 0
    job["queue_file"] = submit(d, job, queued) if queued else None
    if not queued:
        submit(d, job, False)
    t = time.time()
    r, done = wait_for(d, job, 20, beat.get("pid"), quiet=True)
    if done and r.get("status") == "done":
        out = r.get("out") or {}
        say("ok", "test job answered in %.2f s: Fusion %s, active document %r, %d loaded"
            % (time.time() - t, out.get("fusion"), out.get("document"), len(out.get("documents") or [])))
        return 0
    if job.get("queue_file") and os.path.exists(job["queue_file"]):
        os.remove(job["queue_file"])
    say("FAIL", "the test job got no answer in 20 s")
    return 2


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="Run a Python script inside Autodesk Fusion via FusionAgent.")
    p.add_argument("script", nargs="?", help='a .py file to run inside Fusion, "ping", or "doctor"')
    p.add_argument("-c", "--code", help='Python code to run instead of a file ("-" reads it from stdin)')
    p.add_argument("-e", "--eval", dest="expr", help='one expression; its value comes back as out["value"]')
    p.add_argument("--args", help="JSON object passed to the script as `args`")
    p.add_argument("--args-file", help="JSON file passed to the script as `args`")
    p.add_argument("--doc", default=os.environ.get("FUSION_AGENT_DOC"),
                   help="run on this open document (tab name, or file name without the version) instead of "
                        "the active one; default: the FUSION_AGENT_DOC environment variable")
    p.add_argument("--read-only", action="store_true",
                   help="fail (exit 6) when the job changes anything the receipt can see; nothing is undone")
    p.add_argument("--no-receipt", action="store_true", help="skip the before/after check of the documents")
    p.add_argument("--timeout", type=float, default=120, help="seconds to wait, in line and running (default 120)")
    p.add_argument("--save", help="also write the full result JSON to this file")
    p.add_argument("--print-limit", type=int, default=8000,
                   help="print at most this many characters of out and of printed output (0: no limit)")
    p.add_argument("--allow", default="", help="comma-separated pre-flight rules to let through, e.g. modal-ui")
    p.add_argument("--no-preflight", action="store_true", help="skip the pre-flight check")
    a = p.parse_args()

    sources = [x for x in (a.script, a.code, a.expr) if x is not None]
    if len(sources) != 1:
        p.error("give exactly one of: SCRIPT, ping, doctor, -c CODE, -e EXPRESSION")

    d = agent_dir()
    if a.script == "doctor":
        return doctor(d)
    beat = read_json(os.path.join(d, "alive.json"))
    if not beat or time.time() - beat.get("t", 0) > 5:
        print("FusionAgent is not running (no fresh heartbeat in %s)." % d)
        print("Open Fusion, then Utilities > Add-Ins (Shift+S): find FusionAgent in the list and run it.")
        print("(This file, run_job.py, is not added to Fusion; it runs from a terminal.)")
        print("For a full check: python run_job.py doctor")
        return 2
    installed = beat.get("agent", "0")
    if a.doc is not None and a.script == "ping":
        print("note: --doc (or FUSION_AGENT_DOC) is ignored by ping, which lists every open document.")
        a.doc = None
    if a.doc is not None and not a.doc.strip():
        print("--doc is empty. Give a document name, or leave --doc out (and unset FUSION_AGENT_DOC).")
        return 2
    if a.doc and version_tuple(installed) < NEEDS_FOR_DOC:
        print("--doc needs FusionAgent 1.1.0 or newer, and the installed add-in is %s." % installed)
        print("Run python install.py, then in Fusion (Shift+S) stop FusionAgent and run it again.")
        return 2
    queued = version_tuple(installed) >= NEEDS_FOR_QUEUE
    if a.read_only and not queued:
        print("--read-only needs FusionAgent 1.2.0 or newer, and the installed add-in is %s." % installed)
        print("Run python install.py, then in Fusion (Shift+S) stop FusionAgent and run it again.")
        return 2
    if version_tuple(installed) < version_tuple(VERSION):
        print("note: the installed add-in is %s and this package is %s. Run python install.py, "
              "then stop and run FusionAgent in Fusion (Shift+S)." % (installed, VERSION))

    if not queued and (os.path.exists(os.path.join(d, "job.json")) or os.path.exists(os.path.join(d, "job.running"))):
        print("FusionAgent is busy with another job. Try again when it finishes.")
        blocked = blocking_windows(beat.get("pid"))
        if blocked:
            print(say_blocked(blocked))
        return 3

    job = {"id": uuid.uuid4().hex, "caller": {"cwd": os.getcwd(), "pid": os.getpid()}}
    if a.script == "ping":
        job["action"] = "ping"
    else:
        if a.script is not None:
            script = os.path.abspath(a.script)
            if not os.path.isfile(script):
                print("No such script: " + script)
                return 2
            with open(script, encoding="utf-8-sig") as f:
                code = f.read()
        else:
            if a.code == "-":                # bytes, so Arabic names survive a Windows pipe
                code = sys.stdin.buffer.read().decode("utf-8-sig")
            else:
                code = a.code
            if a.expr is not None:           # own lines, so a trailing # comment cannot eat the bracket
                code = "out['value'] = (\n%s\n)\n" % a.expr
            inline = os.path.join(d, "inline")
            os.makedirs(inline, exist_ok=True)
            sweep(inline)
            script = os.path.join(inline, job["id"] + ".py")
            with open(script, "w", encoding="utf-8") as f:
                f.write(code)

        if not a.no_preflight:
            try:
                refusals, warnings = preflight(code, script, pinned=bool(a.doc))
            except SyntaxError as e:
                refusals, warnings = [], []
                print("note: could not check the script (local Python %s: %s, line %s); sending it anyway."
                      % (sys.version.split()[0], e.msg, e.lineno))
            allowed = {r.strip() for r in a.allow.split(",") if r.strip()}
            warnings += [f for f in refusals if f[0] in allowed]
            refusals = [f for f in refusals if f[0] not in allowed]
            for rule, line, msg in warnings:
                print("warning [%s] line %s: %s" % (rule, line, msg))
            if refusals:
                for rule, line, msg in refusals:
                    print("refused [%s] line %s: %s" % (rule, line, msg))
                print("Not sent. If you are sure, add --allow %s" % ",".join(sorted({f[0] for f in refusals})))
                return 5

        args = {}
        try:
            if a.args_file:
                with open(a.args_file, encoding="utf-8-sig") as f:
                    args.update(json.load(f))
            if a.args:
                args.update(json.loads(a.args))
        except (OSError, ValueError) as e:
            print("Bad --args / --args-file: %s" % e)
            return 2
        if os.name == "nt":
            notes = []
            args = fix_bash_paths(args, notes)
            for n in notes:
                print("note: Git Bash path rewritten: " + n)
        job.update({"action": "exec_file", "path": script, "args": args})
        if a.doc:
            job["doc"] = a.doc
        if a.read_only:
            job["read_only"] = True
        if a.no_receipt:
            job["receipt"] = False

    if queued:
        job["expires"] = time.time() + a.timeout + 5          # dropped unrun if we have stopped waiting
        job["queue_file"] = submit(d, job, True)
        ahead = jobs_ahead(d, job["queue_file"])
        if ahead:
            print("in line: %d job(s) ahead" % ahead, flush=True)
    else:
        submit(d, job, False)

    last, finished = wait_for(d, job, a.timeout, beat.get("pid"))
    if not finished:
        qf = job.get("queue_file")
        if qf:
            try:
                os.remove(qf)
                print("Gave up after %.0f s in line; the job was taken out of the queue and never ran." % a.timeout)
                return 4
            except OSError:
                pass                         # the add-in took it meanwhile
        if last is None:
            print("No result after %.0f s." % a.timeout)
            blocked = blocking_windows(beat.get("pid"))
            print(say_blocked(blocked) if blocked else "Fusion may be busy or showing a dialog.")
            if queued:
                print("If it still runs, its result will land in %s"
                      % os.path.normpath(os.path.join(d, "results", job["id"] + ".json")))
            return 4

    results = os.path.join(d, "results")
    os.makedirs(results, exist_ok=True)
    sweep(results)
    full = os.path.join(results, job["id"] + ".json")
    for path in [full] + ([a.save] if a.save else []):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(last, f, ensure_ascii=False, indent=2)

    full = os.path.normpath(full)
    ran = last.get("ran_on")
    if job.get("doc") and ran:
        where = []
        if not ran.get("isActive"):
            where.append("not the active document")
        if ran.get("isVisible") is False:
            where.append("isVisible is false")
        print("ran on: %s%s" % (ran.get("name"), " (%s)" % "; ".join(where) if where else ""))
    if "changes" not in last and "active_after" in last and last.get("active_before") != last.get("active_after"):
        print("warning: the active document changed during the job: %s -> %s"
              % (last.get("active_before"), last.get("active_after")))
    cut_any = False
    if last.get("stdout"):
        text, cut = capped(last["stdout"].rstrip(), a.print_limit, keep="tail")
        cut_any |= cut or bool(last.get("stdout_truncated"))
        print("--- printed ---")
        print(("[... earlier lines cut]\n" if cut or last.get("stdout_truncated") else "") + text)
    if last.get("stderr"):
        text, cut = capped(last["stderr"].rstrip(), a.print_limit, keep="tail")
        cut_any |= cut
        print("--- stderr ---")
        print(("[... earlier lines cut]\n" if cut else "") + text)
    if last.get("out"):
        text, cut = capped(json.dumps(last["out"], ensure_ascii=False, indent=2), a.print_limit)
        print("--- out ---")
        if cut:
            cut_any = True
            print("too long to print (%d characters). What it holds: %s"
                  % (len(json.dumps(last["out"], ensure_ascii=False, indent=2)),
                     json.dumps(describe(last["out"]), ensure_ascii=False)))
            print(text + "\n[... cut]")
        else:
            print(text)
    if cut_any:
        print("--- full result: %s ---" % full)
    if last.get("action") != "ping" and last.get("status") != "running":
        print_changes(last.get("changes"))
    if last.get("status") == "error":
        print("--- error ---")
        print(last.get("traceback", "").rstrip())
        for h in hints_for(last.get("traceback", "")):
            print("hint: " + h)
        kind = (last.get("error") or {}).get("type")
        if kind == "JobInputError":
            return 2                         # a wrong --doc is bad input, not a failing script
        if kind == "ReadOnlyViolation":
            return 6
        return 1
    if last.get("status") != "done":
        print("--- still running after %.0f s; partial result above ---" % a.timeout)
        return 4
    print("--- done in %s s ---" % last.get("seconds"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
