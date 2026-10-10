#!/usr/bin/env python3
"""Send a job to the FusionAgent add-in running inside Autodesk Fusion and print the result.

    python run_job.py ping                          is the add-in alive? which document is open?
    python run_job.py SCRIPT.py                     run a script inside Fusion
    python run_job.py SCRIPT.py --args "{\"k\": 1}"   arguments; the script sees them as `args`
    python run_job.py SCRIPT.py --args-file a.json
    python run_job.py SCRIPT.py --timeout 300 --save result.json

Exit code: 0 done, 1 the script raised (traceback printed), 2 agent not running
or bad input, 3 agent busy, 4 timed out. Standard library only.
"""
import argparse
import json
import os
import sys
import time
import uuid


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


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="Run a Python script inside Autodesk Fusion via FusionAgent.")
    p.add_argument("script", help='a .py file to run inside Fusion, or "ping"')
    p.add_argument("--args", help="JSON object passed to the script as `args`")
    p.add_argument("--args-file", help="JSON file passed to the script as `args`")
    p.add_argument("--timeout", type=float, default=120, help="seconds to wait (default 120)")
    p.add_argument("--save", help="also write the full result JSON to this file")
    a = p.parse_args()

    d = agent_dir()
    beat = read_json(os.path.join(d, "alive.json"))
    if not beat or time.time() - beat.get("t", 0) > 5:
        print("FusionAgent is not running (no fresh heartbeat in %s)." % d)
        print("Open Fusion, then Utilities > Add-Ins (Shift+S): find FusionAgent in the list and run it.")
        print("(This file, run_job.py, is not added to Fusion; it runs from a terminal.)")
        return 2
    job_path = os.path.join(d, "job.json")
    if os.path.exists(job_path) or os.path.exists(os.path.join(d, "job.running")):
        print("FusionAgent is busy with another job. Try again when it finishes.")
        return 3

    job = {"id": uuid.uuid4().hex}
    if a.script == "ping":
        job["action"] = "ping"
    else:
        script = os.path.abspath(a.script)
        if not os.path.isfile(script):
            print("No such script: " + script)
            return 2
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
        job.update({"action": "exec_file", "path": script, "args": args})

    tmp = job_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)
    os.replace(tmp, job_path)

    t0 = time.time()
    last = None
    while time.time() - t0 < a.timeout:
        time.sleep(0.3)
        r = read_json(os.path.join(d, "result.json"))
        if r and r.get("id") == job["id"]:
            last = r
            if r.get("status") in ("done", "error"):
                break

    if last is None:
        print("No result after %.0f s. Fusion may be busy or showing a dialog." % a.timeout)
        return 4
    if a.save:
        with open(a.save, "w", encoding="utf-8") as f:
            json.dump(last, f, ensure_ascii=False, indent=2)
    if last.get("stdout"):
        print("--- printed ---")
        print(last["stdout"].rstrip())
    if last.get("out"):
        print("--- out ---")
        print(json.dumps(last["out"], ensure_ascii=False, indent=2))
    if last.get("status") == "error":
        print("--- error ---")
        print(last.get("traceback", "").rstrip())
        return 1
    if last.get("status") != "done":
        print("--- still running after %.0f s; partial result above ---" % a.timeout)
        return 4
    print("--- done in %s s ---" % last.get("seconds"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
