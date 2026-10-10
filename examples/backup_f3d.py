"""Save a copy of the open design as an .f3d file. The design itself is not touched.

    python run_job.py examples/backup_f3d.py
    python run_job.py examples/backup_f3d.py --args "{\"path\": \"C:/Backups/part.f3d\"}"

Default location: <agent folder>/backups/<document>-<date>-<time>.f3d
"""
import os
import time

d = design()
name = safe_name(doc.name)          # `doc` follows --doc (add-in 1.1.0+)
path = args.get("path") or os.path.join(agent_dir, "backups", name + time.strftime("-%Y%m%d-%H%M%S") + ".f3d")
os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

opts = d.exportManager.createFusionArchiveExportOptions(path)
ok = d.exportManager.execute(opts)

out["path"] = path
out["ok"] = bool(ok) and os.path.isfile(path)
out["bytes"] = os.path.getsize(path) if os.path.isfile(path) else 0
print(("saved " if out["ok"] else "FAILED ") + path)
