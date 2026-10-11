#!/usr/bin/env python3
"""Run the same read-only Python through FusionAgent and through Fusion's built-in MCP, and compare.

    python tools/bench_mcp.py                   find the MCP port by itself, write bench_out/
    python tools/bench_mcp.py --port 65463 --out C:/temp/bench

Needs Fusion open with a design, FusionAgent running, and the MCP server switched on
(Preferences > General > API). Every task only reads: nothing is created, moved or saved,
and the camera is not touched. Both routes use the ACTIVE document: FUSION_AGENT_DOC is ignored
here. Results go outside the repo by default (<agent folder>/bench). Standard library only; the
MCP port discovery is Windows only (pass --port elsewhere).

Tasks: T1 a trivial round trip (5 runs each, alternating), T2 every body's volume and size
(3 runs each, plus the MCP's readOnly mode), T3 how an error comes back, T4 when the caller
first sees progress, T5 a 1600x1000 screenshot of the current view.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_JOB = os.path.join(ROOT, "run_job.py")


def agent_dir():
    if os.environ.get("FUSION_AGENT_DIR"):
        return os.environ["FUSION_AGENT_DIR"]
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Application Support"), "FusionAgent")
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "FusionAgent")


# ------------------------------------------------------------------ MCP client

class MCP:
    """Minimal streamable-HTTP MCP client."""

    def __init__(self, url):
        self.url, self.sid, self.n = url, None, 0

    def _post(self, payload, timeout=600):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.sid:
            h["MCP-Session-Id"] = self.sid
        req = urllib.request.Request(self.url, json.dumps(payload).encode(), h, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            self.sid = r.headers.get("MCP-Session-Id") or self.sid
            body = r.read().decode("utf-8", "replace")
            ctype = r.headers.get("Content-Type", "")
        if not body.strip():
            return None
        if "event-stream" in ctype:
            data = [l[5:].strip() for l in body.splitlines() if l.startswith("data:")]
            body = data[-1] if data else "{}"
        return json.loads(body)

    def call(self, method, params=None, timeout=600):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        return self._post(msg, timeout)

    def init(self):
        r = self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "fusionagent-bench", "version": "1"}}, timeout=5)
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass
        return r

    def tool(self, name, args, timeout=600):
        t = time.perf_counter()
        r = self.call("tools/call", {"name": name, "arguments": args}, timeout)
        return r, time.perf_counter() - t


def find_mcp_port(pid):
    """Probe every port the Fusion process listens on for an MCP initialize answer. The state
    word is localized (LISTENING, ABHÖREN...), so a listener is told by its foreign address *:0."""
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, errors="replace").stdout
    pattern = r"(?:127\.0\.0\.1|0\.0\.0\.0|\[::1?\]):(\d+)\s+(?:0\.0\.0\.0|\[::\]):0\s+\S+\s+%d\s*$" % pid
    ports = sorted({int(m.group(1)) for m in re.finditer(pattern, out, re.M)})
    for port in ports:
        try:
            r = MCP("http://127.0.0.1:%d/mcp" % port).init()
            if r and "result" in r and "serverInfo" in r["result"]:
                return port
        except Exception:
            continue
    return None


def mcp_text(r):
    """The printed text of an MCP script call. Fusion wraps it as {"message": ..., "success": ...}."""
    if r is None:
        return None, None
    if "error" in r:
        return None, r["error"]
    res = r.get("result", {})
    text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    if res.get("isError"):
        return None, text or res
    try:
        wrapped = json.loads(text)
        if isinstance(wrapped, dict) and ("message" in wrapped or "error" in wrapped):
            if wrapped.get("success") is False:
                return None, wrapped.get("error") or wrapped
            return wrapped.get("message", ""), None
    except ValueError:
        pass
    return text, None


def last_json(text):
    for line in reversed((text or "").strip().splitlines()):
        if line.strip().startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                pass
    return None


# ------------------------------------------------------- the shared task code

TRIVIAL = '''
import adsk.core
def task():
    app = adsk.core.Application.get()
    return {"version": app.version, "document": app.activeDocument.name}
'''

INSPECT = '''
import adsk.core, adsk.fusion, time
def task():
    t0 = time.perf_counter()
    app = adsk.core.Application.get()
    d = adsk.fusion.Design.cast(app.activeProduct)
    comps = d.allComponents
    bodies, failed = [], 0
    for c in comps:
        for b in c.bRepBodies:
            try:
                v = round(b.volume, 4)
            except Exception:
                v, failed = None, failed + 1
            try:
                bb = b.boundingBox
                size = [round((bb.maxPoint.x - bb.minPoint.x) * 10, 2), round((bb.maxPoint.y - bb.minPoint.y) * 10, 2),
                        round((bb.maxPoint.z - bb.minPoint.z) * 10, 2)]
            except Exception:
                size = None
            bodies.append([c.name, b.name, v, size])
    return {"document": app.activeDocument.name, "components": comps.count,
            "occurrences": d.rootComponent.allOccurrences.count, "bodies": len(bodies),
            "volume_failed": failed, "volume_total_cm3": round(sum(x[2] or 0 for x in bodies), 3),
            "joints": sum(c.joints.count for c in comps), "body_list": bodies,
            "inside_ms": round((time.perf_counter() - t0) * 1000, 1)}
'''

ERROR = '''
def helper(x):
    return 10 / x
def task():
    return helper(0)
'''

PROGRESS = '''
import time
def task(report):
    for i in range(1, 4):
        time.sleep(0.6)
        report(i)
    return {"steps": 3}
'''


class Bench:
    def __init__(self, mcp, jobs_dir):
        self.mcp, self.jobs = mcp, jobs_dir

    def agent_file(self, code, name, progress=False):
        tail = ("def _rep(i):\n    out['step'] = i\n    flush()\nout.update(task(_rep))\n" if progress
                else "out.update(task())\n")
        path = os.path.join(self.jobs, name + ".py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(code + "\n" + tail)
        return path

    @staticmethod
    def mcp_script(code, progress=False):
        call = "task(lambda i: print('step', i))" if progress else "task()"
        return code + "\nimport json\ndef run(_context: str):\n    print(json.dumps(%s))\n" % call

    def agent(self, path, timeout=300):
        save = os.path.join(self.jobs, "last.json")
        if os.path.exists(save):
            os.remove(save)
        env = dict(os.environ)
        env.pop("FUSION_AGENT_DOC", None)        # the MCP reads the active document, so must we
        t = time.perf_counter()
        p = subprocess.run([sys.executable, RUN_JOB, path, "--save", save, "--timeout", str(timeout),
                            "--print-limit", "200"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env)
        wall = time.perf_counter() - t
        res = json.load(open(save, encoding="utf-8")) if os.path.exists(save) else None
        return {"exit": p.returncode, "wall": round(wall, 3), "result": res}

    def via_mcp(self, script, read_only=None):
        obj = {"script": script}
        if read_only is not None:
            obj["readOnly"] = read_only
        r, dt = self.mcp.tool("fusion_mcp_execute", {"featureType": "script", "object": obj})
        text, err = mcp_text(r)
        return {"wall": round(dt, 3), "text": text, "error": err}


def protocol_round_trip(script, queued):
    """Hand a job to the add-in the way run_job.py does and poll every 5 ms for its result."""
    import uuid
    d = agent_dir()
    jid = uuid.uuid4().hex
    job = {"id": jid, "action": "exec_file", "path": script, "args": {}, "receipt": False}
    if queued:
        os.makedirs(os.path.join(d, "queue"), exist_ok=True)
        path = os.path.join(d, "queue", "%020d-%s.json" % (time.time_ns(), jid))
        job["expires"] = time.time() + 30
    else:
        path = os.path.join(d, "job.json")
    t = time.perf_counter()
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(job, f)
    os.replace(path + ".tmp", path)
    final, live = os.path.join(d, "results", jid + ".json"), os.path.join(d, "result.json")
    while time.perf_counter() - t < 30:
        for p in (final, live):
            try:
                r = json.load(open(p, encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if r.get("id") == jid and r.get("status") in ("done", "error"):
                return round(time.perf_counter() - t, 3)
        time.sleep(0.005)
    return None


def digest(obj):
    return hashlib.md5(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:10]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, help="MCP port (default: found from the Fusion process)")
    ap.add_argument("--out", default=os.path.join(agent_dir(), "bench"),
                    help="folder for results and screenshots (default: <agent folder>/bench, outside the repo)")
    ap.add_argument("--only", choices=["T0"], help="T0: time the folder protocol alone, without the MCP")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    beat = json.load(open(os.path.join(agent_dir(), "alive.json"), encoding="utf-8-sig"))
    if a.only == "T0":
        b = Bench(None, tempfile.mkdtemp(prefix="fa_bench_"))
        j = b.agent_file(TRIVIAL, "t0")
        queued = tuple(int(x) for x in str(beat.get("agent", "0")).split(".")[:3]) >= (1, 2, 0)
        pt = [protocol_round_trip(j, queued) for _ in range(7)]
        print("T0 folder protocol alone (FusionAgent %s), median of 7: %.3f s %s"
              % (beat.get("agent"), statistics.median(pt), pt))
        return 0
    port = a.port or find_mcp_port(beat["pid"])
    if not port:
        print("No MCP server found on the Fusion process. Switch it on in Preferences > General > API, or pass --port.")
        return 2
    mcp = MCP("http://127.0.0.1:%d/mcp" % port)
    mcp.init()
    print("MCP on port %d, FusionAgent %s" % (port, beat.get("agent")))
    b = Bench(mcp, tempfile.mkdtemp(prefix="fa_bench_"))
    R = {"port": port, "agent_version": beat.get("agent")}

    # T0: the folder protocol alone, from this process: no Python start-up for run_job.py.
    j = b.agent_file(TRIVIAL, "t0")
    queued = tuple(int(x) for x in str(beat.get("agent", "0")).split(".")[:3]) >= (1, 2, 0)
    pt = [protocol_round_trip(j, queued) for _ in range(7)]
    R["T0_protocol_s"] = {"agent": pt, "agent_median": statistics.median(pt)}
    print("T0 folder protocol alone, median of 7: FusionAgent %.3f s (%s)" % (statistics.median(pt), pt))
    if a.only == "T0":
        print(json.dumps(R["T0_protocol_s"]))
        return 0

    j, s = b.agent_file(TRIVIAL, "t1"), b.mcp_script(TRIVIAL)
    at, mt = [], []
    for _ in range(5):
        at.append(b.agent(j)["wall"])
        mt.append(b.via_mcp(s)["wall"])
    R["T1_round_trip_s"] = {"agent": at, "mcp": mt, "agent_median": statistics.median(at), "mcp_median": statistics.median(mt)}
    print("T1 round trip, median of 5: FusionAgent %.3f s, MCP %.3f s" % (statistics.median(at), statistics.median(mt)))

    j, s = b.agent_file(INSPECT, "t2"), b.mcp_script(INSPECT)
    rows = []
    for _ in range(3):
        x = b.agent(j)
        xo = (x["result"] or {}).get("out") or {}
        y = b.via_mcp(s)
        yo = last_json(y["text"]) or {}
        z = b.via_mcp(s, read_only=True)
        zo = last_json(z["text"]) or {}
        bl = xo.get("body_list")
        rows.append({"agent_wall": x["wall"], "agent_exit": x["exit"], "agent_inside_ms": xo.get("inside_ms"),
                     "mcp_wall": y["wall"], "mcp_error": y["error"], "mcp_inside_ms": yo.get("inside_ms"),
                     "mcp_readonly_wall": z["wall"], "mcp_readonly_error": z["error"],
                     "same_bodies": bool(bl) and digest(bl) == digest(yo.get("body_list")) == digest(zo.get("body_list"))})
    R["T2_inspect"] = {"runs": rows, "summary": {k: v for k, v in xo.items() if k != "body_list"}}
    print("T2 inspect %s bodies: FusionAgent %s s, MCP %s s, MCP readOnly %s s, identical results: %s"
          % (xo.get("bodies"), [r["agent_wall"] for r in rows], [r["mcp_wall"] for r in rows],
             [r["mcp_readonly_wall"] for r in rows], all(r["same_bodies"] for r in rows)))

    x = b.agent(b.agent_file(ERROR, "t3"))
    y = b.via_mcp(b.mcp_script(ERROR))
    R["T3_error"] = {"agent_exit": x["exit"], "agent_traceback": (x["result"] or {}).get("traceback"),
                     "agent_error": (x["result"] or {}).get("error"), "mcp_error": y["error"]}
    print("T3 error: FusionAgent exit %s, structured error %s" % (x["exit"], (x["result"] or {}).get("error")))

    seen, stop = [], threading.Event()
    result_json = os.path.join(agent_dir(), "result.json")

    def watch():
        last, t0 = None, time.perf_counter()
        try:                                     # the previous job's result is not progress
            old_id = json.load(open(result_json, encoding="utf-8-sig")).get("id")
        except (OSError, ValueError):
            old_id = None
        while not stop.is_set():
            try:
                r = json.load(open(result_json, encoding="utf-8-sig"))
                if r.get("id") == old_id:
                    time.sleep(0.05)
                    continue
                st = (r.get("status"), (r.get("out") or {}).get("step"))
                if st != last:
                    seen.append([round(time.perf_counter() - t0, 2)] + list(st))
                    last = st
            except Exception:
                pass
            time.sleep(0.05)

    th = threading.Thread(target=watch)
    th.start()
    b.agent(b.agent_file(PROGRESS, "t4", progress=True))
    stop.set()
    th.join()
    y = b.via_mcp(b.mcp_script(PROGRESS, progress=True))
    R["T4_progress"] = {"agent_seen": seen, "mcp_all_at_once_after_s": y["wall"]}
    print("T4 progress: FusionAgent saw %s; MCP returned everything after %.2f s" % (seen, y["wall"]))

    shot_a = os.path.join(a.out, "shot_agent.png").replace("\\", "/")
    shot_m = os.path.join(a.out, "shot_mcp.png")
    jp = os.path.join(b.jobs, "t5.py")
    with open(jp, "w", encoding="utf-8") as f:
        f.write("out['ok'] = save_viewport(%r, 1600, 1000, False)\n" % shot_a)
    x = b.agent(jp)
    r, dt = mcp.tool("fusion_mcp_read", {"queryType": "screenshot", "width": 1600, "height": 1000,
                                         "direction": "current", "transparentBackground": False})
    for c in (r or {}).get("result", {}).get("content", []):
        if c.get("type") == "image":
            with open(shot_m, "wb") as f:
                f.write(base64.b64decode(c["data"]))
    R["T5_screenshot_s"] = {"agent": x["wall"], "mcp": round(dt, 3),
                            "agent_bytes": os.path.getsize(shot_a) if os.path.exists(shot_a) else None,
                            "mcp_bytes": os.path.getsize(shot_m) if os.path.exists(shot_m) else None}
    print("T5 screenshot: FusionAgent %.3f s, MCP %.3f s" % (x["wall"], dt))

    path = os.path.join(a.out, "bench_results.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(R, f, ensure_ascii=False, indent=1)
    print("results: " + path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
