#!/usr/bin/env python3
"""Copy the FusionAgent add-in into Fusion's add-ins folder, on Windows or macOS.

    python install.py

Then, the first time: in Fusion open Utilities > Add-Ins (Shift+S), find FusionAgent
in the list, run it, and tick Run on Startup. After an update: stop it and run it again
there (or restart Fusion), so Fusion loads the new copy. Standard library only.
"""
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_job  # noqa: E402  (same folder: addins_dir, agent_dir, file_sha, file_version)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    src = os.path.join(HERE, "FusionAgent")
    dest = os.path.join(run_job.addins_dir(), "FusionAgent")
    os.makedirs(dest, exist_ok=True)
    copied = []
    for name in ("FusionAgent.py", "FusionAgent.manifest"):
        shutil.copy2(os.path.join(src, name), os.path.join(dest, name))
        copied.append(name)
    new_sha = run_job.file_sha(os.path.join(dest, "FusionAgent.py"))
    ok = new_sha == run_job.file_sha(os.path.join(src, "FusionAgent.py"))
    print("%s FusionAgent %s into:" % ("Installed" if ok else "FAILED to install",
                                       run_job.file_version(os.path.join(dest, "FusionAgent.py"))))
    print("  " + dest)
    if not ok:
        return 1
    if sys.platform == "darwin":
        print("(macOS: this path is the documented one but has not been tested on a Mac yet.)")

    beat = run_job.read_json(os.path.join(run_job.agent_dir(), "alive.json"))
    fresh = beat and time.time() - beat.get("t", 0) < 5
    print()
    if not fresh:
        print("Next: in Fusion open Utilities > Add-Ins (Shift+S), find FusionAgent in the list, run it,")
        print("and tick Run on Startup. Then check everything with: python run_job.py doctor")
    elif beat.get("sha") == new_sha:
        print("Fusion is already running this version. Nothing else to do.")
    else:
        print("Fusion is still running FusionAgent %s. To load the new one: Shift+S, find FusionAgent," % beat.get("agent"))
        print("stop it and run it again (or restart Fusion). Then: python run_job.py doctor")
    print("run_job.py itself is never added to Fusion: it runs from a terminal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
