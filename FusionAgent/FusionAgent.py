"""FusionAgent - lets any program drive Autodesk Fusion through files.

A background thread watches a folder for jobs. Each job names a Python file;
the add-in runs it on Fusion's main thread (the Fusion API is not thread safe)
and writes the result back. The watcher thread never touches the API.

Folder (override with the FUSION_AGENT_DIR environment variable):
    Windows: %LOCALAPPDATA%\\FusionAgent
    macOS:   ~/Library/Application Support/FusionAgent

Files in that folder:
    queue/<ns>-<id>.json   waiting jobs (1.2.0+), run oldest first:
                 {"id", "action": "exec_file" | "ping", "path", "args", "doc", "expires",
                  "read_only", "receipt", "receipt_max_bodies", "caller"}. "receipt": true
                 (counts), "full" (counts and geometry, skipped in a design over
                 receipt_max_bodies bodies) or false. A job whose "expires" has passed is
                 dropped unrun: its client has stopped waiting.
    job.json     the single job slot that 1.0/1.1 clients use; still served first
    job.running  the job being executed (claimed by renaming)
    job.done     the last finished job
    result.json  the running or last job: {"id", "status": "running" | "done" | "error",
                  "out", "stdout", "stderr", "traceback", "error", "ran_on", "changes", ...}
    results/<id>.json   each finished job's result, so queued clients never miss theirs
    journal.jsonl        one line per finished job: who sent what, on which document, what changed
    alive.json   heartbeat, rewritten every second while the add-in runs

SECURITY: this add-in runs any Python file a job points to, with your user's
rights. Keep the folder private (never in a shared or cloud-synced folder) and
only run scripts you have read.
"""

import adsk.core
import adsk.fusion
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import traceback

VERSION = "1.5.0"
EVENT_ID = "FusionAgentJobEvent"
POLL_SEC = 0.05         # how often the watcher looks for a job: a stat call, nothing more
BEAT_SEC = 1.0          # how often it rewrites alive.json
JOURNAL_MAX = 2000000   # bytes; the journal then moves to journal.1.jsonl and starts again


def _default_dir():
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "FusionAgent")


AGENT_DIR = os.environ.get("FUSION_AGENT_DIR") or _default_dir()
JOB = os.path.join(AGENT_DIR, "job.json")
RUNNING = os.path.join(AGENT_DIR, "job.running")
DONE = os.path.join(AGENT_DIR, "job.done")
RESULT = os.path.join(AGENT_DIR, "result.json")
BEAT = os.path.join(AGENT_DIR, "alive.json")
QUEUE = os.path.join(AGENT_DIR, "queue")
RESULTS = os.path.join(AGENT_DIR, "results")
JOURNAL = os.path.join(AGENT_DIR, "journal.jsonl")
CANCEL = os.path.join(AGENT_DIR, "cancel")    # cancel/<id>: stop that job at its next flush() or check_cancel()


def _file_sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return None


SELF_SHA = _file_sha(__file__)   # what is running; `run_job.py doctor` compares it with what is installed

_app = None
_ui = None
_event = None
_handler = None
_stop = None
_thread = None
_target = None          # the document a job is pinned to (job["doc"]); None means the active one
STDOUT_KEEP = 20000     # characters of printed output kept in result.json


# ----------------------------------------------------------------------- files

def _jsonable(value):
    """JSON keys must be strings: a script may well return {(a, b): gap}."""
    if isinstance(value, dict):
        return {k if isinstance(k, str) else str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def _dumps(data):
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return json.dumps(_jsonable(data), ensure_ascii=False, indent=2,
                          default=lambda v: try_value(lambda: str(v), "<unprintable %s>" % type(v).__name__))


def _write_json(path, data):
    """Write atomically, so a reader never sees half a file. Windows refuses to
    replace a file another process has open, hence the short retry."""
    text = _dumps(data)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            time.sleep(0.05)
    with io.open(path, "w", encoding="utf-8") as f:     # last resort, not atomic
        f.write(text)


# --------------------------------------------------------- helpers for scripts
# Every function below is available by name inside a job script.

def design():
    """The design of the document this job is pinned to (run_job.py --doc), else
    the active design. Every helper below goes through here, so they follow the pin."""
    if _target is not None:
        d = adsk.fusion.Design.cast(_target.products.itemByProductType("DesignProductType"))
        if d is None:
            raise RuntimeError("Document %r has no Fusion design." % _target.name)
        return d
    d = adsk.fusion.Design.cast(_app.activeProduct)
    if d is None:
        raise RuntimeError("No active Fusion design. Open a design first.")
    return d


def mm(value_cm):
    """Fusion's internal length unit is the centimetre."""
    return value_cm * 10.0


def cm(value_mm):
    return value_mm / 10.0


def point_mm(x, y, z):
    """A Point3D from millimetre coordinates."""
    return adsk.core.Point3D.create(x / 10.0, y / 10.0, z / 10.0)


def find_occurrence(name):
    """First occurrence whose component name or occurrence name matches."""
    for o in design().rootComponent.allOccurrences:
        if o.component.name == name or o.name == name:
            return o
    raise RuntimeError("No occurrence or component named %r" % name)


def bbox_mm(entity):
    """Bounding box of a body, face, occurrence... in millimetres."""
    bb = entity.boundingBox
    lo = [round(mm(v), 3) for v in (bb.minPoint.x, bb.minPoint.y, bb.minPoint.z)]
    hi = [round(mm(v), 3) for v in (bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z)]
    return {"min": lo, "max": hi, "size": [round(h - l, 3) for l, h in zip(lo, hi)]}


def safe_name(text):
    """Turn a document or body name into a valid file name."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip() or "unnamed"


def try_value(fn, default=None):
    """fn() or `default` when Fusion raises. Real designs contain bodies whose
    volume, mass or distance the kernel cannot compute; one of them must not
    stop a whole report."""
    try:
        return fn()
    except Exception:
        return default


def to_component_space(temp_body, occurrence):
    """Move a TemporaryBRepManager body built in world coordinates into the
    occurrence's component space, ready for add_tool_and_combine()."""
    m = (getattr(occurrence, "transform2", None) or occurrence.transform).copy()
    m.invert()
    adsk.fusion.TemporaryBRepManager.get().transform(temp_body, m)
    return temp_body


def add_tool_and_combine(component, target_body, tool_body, operation="cut", name=None):
    """Add a temporary body to `component` and combine it into `target_body`.

    operation: "cut", "join" or "intersect". In a parametric design the tool
    enters through a Base Feature, so each call adds two timeline items.
    """
    ops = {"cut": adsk.fusion.FeatureOperations.CutFeatureOperation,
           "join": adsk.fusion.FeatureOperations.JoinFeatureOperation,
           "intersect": adsk.fusion.FeatureOperations.IntersectFeatureOperation}
    target = target_body.nativeObject or target_body
    tools = adsk.core.ObjectCollection.create()
    if design().designType == adsk.fusion.DesignTypes.ParametricDesignType:
        bf = component.features.baseFeatures.add()
        bf.startEdit()
        try:
            component.bRepBodies.add(tool_body, bf)
        finally:
            bf.finishEdit()
        if name:
            bf.name = name + " tool"
        for i in range(bf.bodies.count):
            tools.add(bf.bodies.item(i))
    else:
        tools.add(component.bRepBodies.add(tool_body))
    inp = component.features.combineFeatures.createInput(target, tools)
    inp.operation = ops[operation]
    inp.isKeepToolBodies = False
    feature = component.features.combineFeatures.add(inp)
    if name and feature is not None:
        feature.name = name
    return feature


def require_timeline_at_end():
    """New features land at the timeline marker; refuse when it is rolled back."""
    d = design()
    if d.designType == adsk.fusion.DesignTypes.ParametricDesignType:
        tl = d.timeline
        if tl.markerPosition != tl.count:
            raise RuntimeError("Move the timeline marker to the end before editing.")


def group_timeline(start_index, name):
    """Wrap every timeline item from start_index to the end in one named group.
    Record design().timeline.count before adding features, pass it here after."""
    d = design()
    if d.designType != adsk.fusion.DesignTypes.ParametricDesignType:
        return None
    tl = d.timeline
    end = tl.count - 1
    if end < start_index:
        return None
    group = tl.timelineGroups.add(start_index, end)
    group.name = name
    group.isCollapsed = True
    return group


def save_viewport(path, width=1600, height=1000, fit=False):
    """Save the active viewport as an image. Returns True when the file exists."""
    if _target is not None and not _target.isActive:
        raise RuntimeError("save_viewport captures the active window, but this job is pinned to %r, "
                           "which is not the active document. Make it active first." % _target.name)
    vp = _app.activeViewport
    if fit:
        cam = vp.camera
        cam.isSmoothTransition = False
        cam.isFitView = True
        vp.camera = cam
        vp.refresh()
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    ok = vp.saveAsImageFile(path, width, height)
    return bool(ok) and os.path.isfile(path)


HELPERS = {f.__name__: f for f in (
    design, mm, cm, point_mm, find_occurrence, bbox_mm, safe_name, try_value,
    to_component_space, add_tool_and_combine, require_timeline_at_end,
    group_timeline, save_viewport)}


# ------------------------------------------------------------------------ jobs

def _open_documents():
    docs = _app.documents
    return [docs.item(i) for i in range(docs.count)]


def _doc_info(doc):
    if doc is None:
        return None
    return {"name": doc.name, "isActive": try_value(lambda: doc.isActive),
            "isVisible": try_value(lambda: doc.isVisible),       # True even for referenced parts, measured
            "isModified": try_value(lambda: doc.isModified)}


class JobInputError(LookupError):
    """A job asked for something that is not there; reported without a traceback."""


class JobCancelled(BaseException):
    """Raised in a job at flush() or check_cancel() once cancel/<id> exists. BaseException, so
    try_value() and `except Exception` in scripts cannot swallow it."""


class ReadOnlyViolation(RuntimeError):
    """A job sent with --read-only changed something. Detected after the fact, never undone."""


def _loose(text):
    return " ".join((text or "").split()).casefold()


def _resolve_doc(name):
    """The one open document called `name`: its tab name ("Car v12"), or its name
    without the version ("Car"), which survives saving. Spaces and case are loose:
    Fusion's own tab names can carry a double space. Open documents only, including
    the hidden ones Fusion loads for referenced parts; this never searches the cloud."""
    want = _loose(name)
    if not want:
        raise JobInputError("--doc is empty. Give a document name, or leave --doc out.")
    docs = _open_documents()

    def file_name(d):
        n = try_value(lambda: d.dataFile.name)           # None for a document never saved
        return _loose(n) if n else None

    tests = (lambda d: d.name == name,
             lambda d: _loose(d.name) == want,
             lambda d: _loose(re.sub(r"\s+v\d+$", "", d.name.strip())) == want,
             lambda d: file_name(d) == want)
    for test in tests:
        hits = [d for d in docs if test(d)]
        if len(hits) > 1:                                 # same name twice: the active, else the one with a tab
            for prefer in (lambda d: try_value(lambda: d.isActive), lambda d: try_value(lambda: d.isVisible)):
                preferred = [d for d in hits if prefer(d)]
                if len(preferred) == 1:
                    hits = preferred
                    break
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise JobInputError("%d open documents match %r and none of them is the active or the only "
                                "visible one: %s" % (len(hits), name, [_doc_info(d) for d in hits]))
    raise JobInputError("No open document named %r. Open documents: %s" % (name, [d.name for d in docs]))


def _ping():
    doc = _app.activeDocument
    return {"agent": VERSION, "sha": SELF_SHA, "file": __file__,
            "fusion": _app.version, "python": sys.version.split()[0],
            "agent_dir": AGENT_DIR, "document": doc.name if doc else None,
            "documents": [_doc_info(d) for d in _open_documents()]}


def _geometry(d):
    """Per body: volume (mm3), area (mm2) and centre of mass (mm) from one low-accuracy physical
    properties call, plus face and edge counts; per occurrence: its placement. The centre moves when
    a hole slides or a body moves, even when volume and topology stay the same. Low accuracy repeats
    exactly, so any difference is a real change. Measured: 0.2 s for 633 bodies (ARIES)."""
    A = adsk.fusion.CalculationAccuracy.LowCalculationAccuracy
    bodies = {}
    for comp in d.allComponents:
        cname = "<root>" if comp == d.rootComponent else try_value(lambda: comp.name, "?")
        for b in comp.bRepBodies:
            key = "%s/%s" % (cname, try_value(lambda: b.name, "?"))
            while key in bodies:
                key += "'"
            props = try_value(lambda: b.getPhysicalProperties(A))
            com = try_value(lambda: props.centerOfMass) if props else None
            bodies[key] = [try_value(lambda: round(props.volume * 1000, 3)) if props else None,
                           try_value(lambda: round(props.area * 100, 3)) if props else None,
                           [round(com.x * 10, 3), round(com.y * 10, 3), round(com.z * 10, 3)] if com else None,
                           try_value(lambda: [b.faces.count, b.edges.count])]
    placements = {}
    for o in d.rootComponent.allOccurrences:
        m = try_value(lambda: o.transform2.asArray())
        name = try_value(lambda: o.fullPathName)
        if m and name:
            placements[name] = [round(m[i], 6) for i in (0, 1, 2, 4, 5, 6, 8, 9, 10)] + \
                               [round(m[i] * 10, 3) for i in (3, 7, 11)]
    return {"bodies": bodies, "placements": placements}


def _geometry_diff(gb, ga, limit=30):
    """What moved or changed shape, in words a person can check in Fusion."""
    if not gb or not ga:                     # one side could not be measured: say so, never "all removed"
        return {"not_measured": ["the geometry could not be measured %s the job" % ("before" if not gb else "after")]}
    out_ = {}
    bb, ba = gb.get("bodies", {}), ga.get("bodies", {})
    added = sorted(set(ba) - set(bb))
    removed = sorted(set(bb) - set(ba))
    renamed = []
    for r in list(removed):                  # same body under a new name: a rename, not a loss and a gain
        twin = next((a for a in added if ba[a] == bb[r]), None)
        if twin:
            renamed.append("%s -> %s" % (r, twin))
            removed.remove(r)
            added.remove(twin)
    changed = []
    for k in sorted(set(bb) & set(ba)):
        (v0, s0, c0, n0), (v1, s1, c1, n1) = bb[k], ba[k]
        what = []
        if v0 is not None and v1 is not None and abs(v1 - v0) > 1e-3:
            what.append("volume %s -> %s mm3" % (v0, v1))
        if s0 is not None and s1 is not None and abs(s1 - s0) > 1e-3:
            what.append("area %s -> %s mm2" % (s0, s1))
        if n0 != n1:
            what.append("faces/edges %s -> %s" % (n0, n1))
        if c0 and c1:
            shift = [round(y - x, 3) for x, y in zip(c0, c1)]
            if max(abs(x) for x in shift) > 1e-3:
                what.append(("moved %s mm in its component" if not what else "centre moved %s mm") % shift)
        if what:
            changed.append("%s: %s" % (k, "; ".join(what)))
    pb, pa = gb.get("placements", {}), ga.get("placements", {})
    moved = []
    for k in sorted(set(pb) & set(pa)):
        a, b = pb[k], pa[k]
        shift = [round(y - x, 3) for x, y in zip(a[9:], b[9:])]
        turned = max(abs(x - y) for x, y in zip(a[:9], b[:9])) > 1e-5
        if turned or max(abs(x) for x in shift) > 1e-3:
            moved.append("%s: moved %s mm%s" % (k, shift, ", turned" if turned else ""))
    parts_added = ["%s at %s mm" % (k, pa[k][9:]) for k in sorted(set(pa) - set(pb))]
    parts_removed = ["%s (was at %s mm)" % (k, pb[k][9:]) for k in sorted(set(pb) - set(pa))]
    for name, items in (("bodies_added", added), ("bodies_removed", removed), ("bodies_renamed", renamed),
                        ("bodies_changed", changed), ("parts_moved", moved), ("parts_added", parts_added),
                        ("parts_removed", parts_removed)):
        if items:
            out_[name] = items[:limit] + (["... and %d more" % (len(items) - limit)] if len(items) > limit else [])
    return out_


def _fingerprint(doc, geometry=False, max_bodies=None):
    """Cheap facts that change when a job edits a document: counts, and with geometry=True the
    shape and placement of every body (see _geometry). A design with more than max_bodies bodies
    keeps its counts only and says how many bodies it skipped."""
    info = {"isModified": try_value(lambda: doc.isModified)}
    d = try_value(lambda: adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType")))
    if d is not None:
        info["components"] = try_value(lambda: d.allComponents.count)
        info["occurrences"] = try_value(lambda: d.rootComponent.allOccurrences.count)
        info["bodies"] = try_value(lambda: sum(c.bRepBodies.count for c in d.allComponents))
        if try_value(lambda: d.designType) == adsk.fusion.DesignTypes.ParametricDesignType:
            info["timeline"] = try_value(lambda: [d.timeline.count, d.timeline.markerPosition])
            info["parameters"] = try_value(lambda: sorted("%s=%s" % (p.name, p.expression)
                                                          for p in d.userParameters))
        if geometry:
            n = info.get("bodies")
            if max_bodies is not None and n is not None and n > max_bodies:
                info["geometry_skipped"] = n
            else:
                info["geometry"] = try_value(lambda: _geometry(d))
    return info


def _doc_key(doc):
    """Unique per open document. Names are not: two new documents are both "Untitled"."""
    return try_value(lambda: doc.creationId) or try_value(lambda: doc.name)


def _snapshot(geometry=False, max_bodies=None, only=None):
    """The open documents, the active one, and fingerprints of the active and the pinned one,
    all keyed by creationId. A document the job closed is simply missing afterwards.
    geometry: measure shapes, in documents up to max_bodies bodies, or only in the keys in `only`."""
    active = _app.activeDocument
    docs = {}
    for d in {_doc_key(x): x for x in (active, _target) if x is not None}.values():   # once, if pinned to the active
        key = _doc_key(d) if d is not None else None
        name = try_value(lambda: d.name) if d is not None else None
        if key is not None and name is not None:
            measure = geometry and (only is None or key in only)
            docs[key] = dict(_fingerprint(d, measure, max_bodies), name=name)
    open_docs = {}
    for d in _open_documents():
        key, name = _doc_key(d), try_value(lambda d=d: d.name)
        if key is not None:
            open_docs[key] = name
    return {"open": open_docs, "active": [_doc_key(active), try_value(lambda: active.name)] if active else None,
            "docs": docs}


def _diff(before, after, notes=None):
    """What differs between two snapshots; {} when nothing visible changed. Names label the
    documents, keys tell them apart. Geometry that could not be measured goes to notes, not to the
    changes: unknown is not changed, and must not fail a --read-only job on its own."""
    changes = {}
    opened = [after["open"][k] for k in after["open"] if k not in before["open"]]
    closed = [before["open"][k] for k in before["open"] if k not in after["open"]]
    if opened:
        changes["opened"] = opened
    if closed:
        changes["closed"] = closed
    ab, aa = before.get("active") or [None, None], after.get("active") or [None, None]
    if ab[0] != aa[0]:
        changes["active"] = [ab[1], aa[1] if aa[1] != ab[1] else "%s (another document)" % aa[1]]
    for key, fb in before["docs"].items():
        fa = after["docs"].get(key)
        if fa is None:
            continue                         # closed: reported above
        delta = {k: [fb.get(k), fa.get(k)] for k in sorted(set(fb) | set(fa))
                 if k not in ("name", "geometry_skipped") and fb.get(k) != fa.get(k)}
        gb, ga = fb.get("geometry", False), fa.get("geometry", False)   # False: not asked; None: failed
        if gb is not False or ga is not False:
            delta.pop("geometry", None)
            if not gb or not ga:
                if notes is not None:
                    notes.setdefault("not_measured", []).append(
                        "%s (%s the job)" % (fa.get("name") or fb.get("name") or key, "before" if not gb else "after"))
            elif gb != ga:
                g = _geometry_diff(gb, ga)
                if g:
                    delta["geometry"] = g
        if "parameters" in delta:            # say which, not the whole list twice
            b, a = set(delta["parameters"][0] or []), set(delta["parameters"][1] or [])
            delta["parameters"] = {"removed": sorted(b - a), "added": sorted(a - b)}
        if delta:
            changes.setdefault("documents", {})[fa.get("name") or fb.get("name") or key] = delta
    return changes


def _geometry_summary(before, after, notes):
    """For a full receipt: which documents had their geometry compared, which were skipped as
    too big (name: bodies), and which could not be measured."""
    s = {"compared": [], "skipped": {}, "not_measured": list(notes.get("not_measured", []))}
    for key, fb in before["docs"].items():
        fa = ((after or {}).get("docs") or {}).get(key) or {}
        if "geometry_skipped" in fb:
            s["skipped"][fb.get("name") or key] = fb["geometry_skipped"]
        elif fb.get("geometry") and fa.get("geometry"):
            s["compared"].append(fa.get("name") or fb.get("name") or key)
    return s


def _run_job(job, result):
    global _target
    action = job.get("action", "exec_file")
    if action == "ping":
        result["out"] = _ping()
        return
    if action != "exec_file":
        raise ValueError("unknown action %r" % action)

    path = job["path"]
    with io.open(path, "r", encoding="utf-8-sig") as f:
        code = f.read()
    if job.get("doc"):
        _target = _resolve_doc(job["doc"])
    out = {}
    buf, err = io.StringIO(), io.StringIO()
    result["out"] = out
    result["script"] = path
    result["ran_on"] = _doc_info(_target or _app.activeDocument)

    cancel_file = os.path.join(CANCEL, str(job.get("id")))

    if os.path.exists(cancel_file):          # cancelled while it waited (in line, or behind a window)
        raise JobCancelled("cancelled before it started")

    def check_cancel():
        """Stop here if this job was cancelled (run_job.py cancel, Ctrl+C, or its client timed out)."""
        if os.path.exists(cancel_file):
            raise JobCancelled("cancelled at the script's request point; the result so far is kept")

    def flush():
        """Write the result so far, so a long or stuck job still shows progress."""
        text = buf.getvalue()
        result["stdout"] = text[-STDOUT_KEEP:]
        result["stdout_truncated"] = len(text) > STDOUT_KEEP
        result["stderr"] = err.getvalue()[-STDOUT_KEEP:]
        _write_json(RESULT, result)
        check_cancel()

    scope = dict(HELPERS)
    scope.update({"__name__": "__fusion_agent_job__", "__file__": path,
                  "adsk": adsk, "app": _app, "ui": _ui,
                  "doc": _target or _app.activeDocument,
                  "args": job.get("args") or {}, "out": out, "flush": flush,
                  "agent_dir": AGENT_DIR, "check_cancel": check_cancel})
    t = time.time()
    full = job.get("receipt") == "full"
    cap = job.get("receipt_max_bodies")
    cap = cap if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 0 else None
    before = try_value(lambda: _snapshot(full, cap)) if job.get("receipt", True) else None
    receipt_s = time.time() - t
    flush()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            exec(compile(code, path, "exec"), scope)
    finally:
        _release_log_handlers((buf, err))
        text = buf.getvalue()
        result["stdout"] = text[-STDOUT_KEEP:]
        result["stdout_truncated"] = len(text) > STDOUT_KEEP
        result["stderr"] = err.getvalue()[-STDOUT_KEEP:]
        if before is not None:
            t = time.time()
            # Measure afterwards exactly what was measured before, whatever the body count is now.
            measured = {k for k, f in before["docs"].items() if "geometry" in f}
            after = try_value(lambda: _snapshot(bool(measured), only=measured))
            notes = {}
            result["changes"] = try_value(lambda: _diff(before, after, notes)) if after else None
            if full:
                result["receipt_geometry"] = try_value(lambda: _geometry_summary(before, after, notes))
            result["receipt_ms"] = round((receipt_s + time.time() - t) * 1000, 1)
    if job.get("read_only") and result.get("changes"):
        raise ReadOnlyViolation("This job was sent with --read-only but changed: %s. Nothing was undone."
                                % json.dumps(result["changes"], ensure_ascii=False))


def _release_log_handlers(buffers):
    """logging.basicConfig() inside a job binds a handler to the job's own buffer; point it
    back at the real stderr, or every later log line in this Fusion session is lost."""
    logging = sys.modules.get("logging")
    if logging is None:
        return
    loggers = [logging.getLogger()] + [l for l in logging.Logger.manager.loggerDict.values()
                                       if isinstance(l, logging.Logger)]
    for lg in loggers:
        for h in list(lg.handlers):
            if isinstance(h, logging.StreamHandler) and any(h.stream is b for b in buffers):
                try_value(lambda: h.setStream(sys.stderr))


def _same_file(a, b):
    return os.path.normcase(os.path.normpath(a or "")) == os.path.normcase(os.path.normpath(b or ""))


def _describe_error(exc, script):
    """The traceback without this add-in's own frames on top, plus the failing
    line of the job script. Helper frames below the script stay: they show which
    helper raised."""
    if isinstance(exc, SyntaxError) and script and _same_file(exc.filename, script):
        text = "".join(traceback.format_exception_only(type(exc), exc))
        return text, {"type": type(exc).__name__, "message": exc.msg, "file": exc.filename,
                      "line": exc.lineno, "source": (exc.text or "").strip()}
    tb = exc.__traceback__
    while tb is not None and _same_file(tb.tb_frame.f_code.co_filename, __file__):
        tb = tb.tb_next
    if tb is None:                           # the add-in itself failed: keep everything
        tb = exc.__traceback__
    text = "".join(traceback.format_exception(type(exc), exc, tb))
    frames = traceback.extract_tb(tb)
    mine = [f for f in frames if script and _same_file(f.filename, script)] or frames[-1:]
    where = mine[-1] if mine else None
    error = {"type": type(exc).__name__, "message": str(exc),
             "file": where.filename if where else None, "line": where.lineno if where else None,
             "source": (where.line or "").strip() if where else None}
    return text, error


def _record_error(result, exc):
    result["status"] = "error"
    if isinstance(exc, (JobInputError, ReadOnlyViolation, JobCancelled)):   # not a bug in the code: no traceback
        result["traceback"] = "%s: %s" % (type(exc).__name__, exc)
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
        return
    try:
        result["traceback"], result["error"] = _describe_error(exc, result.get("script"))
    except Exception:                        # describing must never hide the real error
        result["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _forget_cancel(job_id):
    try:
        os.remove(os.path.join(CANCEL, str(job_id)))
    except OSError:
        pass


def _journal(job, result, started):
    """One line per finished job, so a session can see later what another one did."""
    entry = {"id": result.get("id"), "t": round(started, 2), "seconds": result.get("seconds"),
             "status": result.get("status"), "action": result.get("action"), "script": result.get("script"),
             "doc": job.get("doc"), "ran_on": (result.get("ran_on") or {}).get("name"),
             "caller": job.get("caller"), "read_only": bool(job.get("read_only")),
             "changes": result.get("changes"), "error": (result.get("error") or {}).get("type")}
    try:
        if os.path.getsize(JOURNAL) > JOURNAL_MAX:
            os.replace(JOURNAL, os.path.join(AGENT_DIR, "journal.1.jsonl"))
    except OSError:
        pass
    with io.open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(_dumps(entry).replace("\n", " ") + "\n")


def _execute():
    global _target
    result = {"id": None, "status": "running", "agent": VERSION}
    job = {}
    t0 = time.time()
    try:
        result["active_before"] = try_value(lambda: _app.activeDocument.name)
        with io.open(RUNNING, "r", encoding="utf-8-sig") as f:
            job = json.load(f)
        result["id"] = job.get("id")
        result["action"] = job.get("action", "exec_file")
        _run_job(job, result)
        result["status"] = "done"
    except SystemExit as e:                  # exit() / sys.exit(): 0 or None means done
        if e.code in (None, 0):
            result["status"] = "done"
        else:
            _record_error(result, e)
    except BaseException as e:               # nothing a script does may kill the add-in
        _record_error(result, e)
    finally:
        _target = None                       # a pin never leaks into the next job
        result["active_after"] = try_value(lambda: _app.activeDocument.name)
        result["seconds"] = round(time.time() - t0, 2)
        try:
            _write_json(RESULT, result)
        except Exception as e:               # never leave the client waiting on "running"
            try:
                _write_json(RESULT, {"id": result.get("id"), "status": "error", "agent": VERSION,
                                     "seconds": result["seconds"],
                                     "traceback": "The result could not be written as JSON: %r" % e,
                                     "error": {"type": "ResultNotWritten", "message": repr(e)}})
            except Exception:
                pass
        _forget_cancel(result.get("id"))
        if result.get("id"):                 # its own file: the next job overwrites result.json
            try:
                os.makedirs(RESULTS, exist_ok=True)
                _write_json(os.path.join(RESULTS, "%s.json" % result["id"]), result)
            except Exception:
                pass
        try:
            _journal(job, result, t0)
        except Exception:
            pass
        try:
            os.replace(RUNNING, DONE)
        except Exception:
            pass


class _Handler(adsk.core.CustomEventHandler):
    def __init__(self):
        super(_Handler, self).__init__()

    def notify(self, args):
        try:
            _execute()
        except BaseException:
            pass


class _Watcher(threading.Thread):
    """File system only. Never touches the Fusion API."""

    def __init__(self, stop_event):
        threading.Thread.__init__(self, name="FusionAgentWatcher")
        self.daemon = True
        self._stop_event = stop_event

    def run(self):
        last_beat = 0.0
        while not self._stop_event.wait(POLL_SEC):
            try:
                now = time.time()
                if now - last_beat >= BEAT_SEC:
                    os.makedirs(AGENT_DIR, exist_ok=True)     # survives the folder being deleted
                    _write_json(BEAT, {"t": now, "pid": os.getpid(), "agent": VERSION,
                                       "sha": SELF_SHA, "file": __file__,
                                       "features": ["doc", "queue", "receipt", "cancel", "full-receipt", "receipt-limit"]})
                    last_beat = now
                if os.path.isfile(RUNNING):
                    continue
                job = _next_job(now)
                if job:
                    os.replace(job, RUNNING)              # claim it, then hand off
                    _app.fireCustomEvent(EVENT_ID, "")
            except Exception:
                pass


def _next_job(now):
    """The legacy job.json first, then the oldest queued job. Queued jobs whose client
    has stopped waiting are dropped unrun, with a result saying so."""
    if os.path.isfile(JOB):
        return JOB
    try:
        names = sorted(n for n in os.listdir(QUEUE) if n.endswith(".json"))
    except OSError:
        return None
    for name in names:
        path = os.path.join(QUEUE, name)
        try:
            with io.open(path, "r", encoding="utf-8-sig") as f:
                job = json.load(f)
        except (OSError, ValueError):
            continue
        cancelled = os.path.exists(os.path.join(CANCEL, str(job.get("id"))))
        if cancelled or (job.get("expires") and job["expires"] < now):
            kind = "JobCancelled" if cancelled else "JobExpired"
            why = "cancelled before it started" if cancelled else "dropped unrun: its client stopped waiting"
            try:
                os.remove(path)
                os.makedirs(RESULTS, exist_ok=True)
                _write_json(os.path.join(RESULTS, "%s.json" % job.get("id")),
                            {"id": job.get("id"), "status": "error", "agent": VERSION,
                             "error": {"type": kind, "message": why}, "traceback": "%s: %s" % (kind, why)})
                _forget_cancel(job.get("id"))
            except OSError:
                pass
            continue
        return path
    return None


# --------------------------------------------------------------- add-in entry

def run(context):
    global _app, _ui, _event, _handler, _stop, _thread
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    try:
        os.makedirs(AGENT_DIR, exist_ok=True)
        if os.path.isfile(RUNNING):          # left by a crash: never run a job twice
            os.replace(RUNNING, os.path.join(AGENT_DIR, "job.abandoned"))
    except Exception:
        pass
    try:
        _app.unregisterCustomEvent(EVENT_ID)
    except Exception:
        pass
    _event = _app.registerCustomEvent(EVENT_ID)
    _handler = _Handler()
    _event.add(_handler)
    _stop = threading.Event()
    _thread = _Watcher(_stop)
    _thread.start()


def stop(context):
    global _event, _handler, _stop, _thread
    try:
        if _stop:
            _stop.set()
        if _thread:
            _thread.join(2.0)                # so no heartbeat lands after it is removed
    except Exception:
        pass
    try:
        if _event and _handler:
            _event.remove(_handler)
        _app.unregisterCustomEvent(EVENT_ID)
    except Exception:
        pass
    try:
        os.remove(BEAT)
    except Exception:
        pass
    _event = _handler = _stop = _thread = None
