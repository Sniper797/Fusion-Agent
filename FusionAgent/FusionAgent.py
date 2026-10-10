"""FusionAgent - lets any program drive Autodesk Fusion through files.

A background thread watches a folder for job.json. Each job names a Python
file; the add-in runs it on Fusion's main thread (the Fusion API is not thread
safe) and writes result.json back. The watcher thread never touches the API.

Folder (override with the FUSION_AGENT_DIR environment variable):
    Windows: %LOCALAPPDATA%\\FusionAgent
    macOS:   ~/Library/Application Support/FusionAgent

Files in that folder:
    job.json     queued job: {"id", "action": "exec_file" | "ping", "path", "args"}
    job.running  the job being executed (claimed by renaming job.json)
    job.done     the last finished job
    result.json  {"id", "status": "running" | "done" | "error", "out", "stdout", "traceback"}
    alive.json   heartbeat, rewritten every half second while the add-in runs

SECURITY: this add-in runs any Python file a job points to, with your user's
rights. Keep the folder private (never in a shared or cloud-synced folder) and
only run scripts you have read.
"""

import adsk.core
import adsk.fusion
import contextlib
import io
import json
import os
import re
import sys
import threading
import time
import traceback

VERSION = "1.0.0"
EVENT_ID = "FusionAgentJobEvent"
POLL_SEC = 0.5


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

_app = None
_ui = None
_event = None
_handler = None
_stop = None
_thread = None


# ----------------------------------------------------------------------- files

def _write_json(path, data):
    """Write atomically, so a reader never sees half a file. Windows refuses to
    replace a file another process has open, hence the short retry."""
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
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
    """The active Fusion design, or a clear error when none is open."""
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

def _ping():
    doc = _app.activeDocument
    return {"agent": VERSION, "fusion": _app.version, "python": sys.version.split()[0],
            "agent_dir": AGENT_DIR, "document": doc.name if doc else None}


def _run_job(job, result):
    action = job.get("action", "exec_file")
    if action == "ping":
        result["out"] = _ping()
        return
    if action != "exec_file":
        raise ValueError("unknown action %r" % action)

    path = job["path"]
    with io.open(path, "r", encoding="utf-8-sig") as f:
        code = f.read()
    out = {}
    buf = io.StringIO()
    result["out"] = out
    result["script"] = path

    def flush():
        """Write the result so far, so a long or stuck job still shows progress."""
        result["stdout"] = buf.getvalue()[-20000:]
        _write_json(RESULT, result)

    scope = dict(HELPERS)
    scope.update({"__name__": "__fusion_agent_job__", "__file__": path,
                  "adsk": adsk, "app": _app, "ui": _ui,
                  "args": job.get("args") or {}, "out": out, "flush": flush,
                  "agent_dir": AGENT_DIR})
    flush()
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(code, path, "exec"), scope)
    finally:
        result["stdout"] = buf.getvalue()[-20000:]


def _execute():
    result = {"id": None, "status": "running", "agent": VERSION}
    t0 = time.time()
    try:
        with io.open(RUNNING, "r", encoding="utf-8-sig") as f:
            job = json.load(f)
        result["id"] = job.get("id")
        result["action"] = job.get("action", "exec_file")
        _run_job(job, result)
        result["status"] = "done"
    except BaseException:                    # a script calling exit() must not kill the add-in
        result["status"] = "error"
        result["traceback"] = traceback.format_exc()
    finally:
        result["seconds"] = round(time.time() - t0, 2)
        try:
            _write_json(RESULT, result)
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
        while not self._stop_event.wait(POLL_SEC):
            try:
                os.makedirs(AGENT_DIR, exist_ok=True)     # survives the folder being deleted
                _write_json(BEAT, {"t": time.time(), "pid": os.getpid(), "agent": VERSION})
                if os.path.isfile(JOB) and not os.path.isfile(RUNNING):
                    os.replace(JOB, RUNNING)              # claim it, then hand off
                    _app.fireCustomEvent(EVENT_ID, "")
            except Exception:
                pass


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
