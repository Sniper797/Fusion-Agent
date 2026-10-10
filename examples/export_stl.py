"""Export every visible solid body as its own binary STL (read-only for the design).

    python run_job.py examples/export_stl.py
    python run_job.py examples/export_stl.py --args "{\"folder\": \"C:/print\", \"refinement\": \"medium\"}"

args:
    folder      default <agent folder>/exports/<document>
    refinement  low | medium | high (default high)
"""
import os

d = design()
em = d.exportManager
folder = args.get("folder") or os.path.join(agent_dir, "exports", safe_name(doc.name))
os.makedirs(folder, exist_ok=True)
levels = {"low": adsk.fusion.MeshRefinementSettings.MeshRefinementLow,
          "medium": adsk.fusion.MeshRefinementSettings.MeshRefinementMedium,
          "high": adsk.fusion.MeshRefinementSettings.MeshRefinementHigh}
level = levels[args.get("refinement", "high")]

root = d.rootComponent
bodies = [(root.name, b) for b in root.bRepBodies]
for o in root.allOccurrences:
    bodies += [(o.fullPathName, b) for b in o.bRepBodies]

files = []
out["folder"] = folder
out["files"] = files
for owner, b in bodies:
    if not (b.isSolid and b.isVisible):
        continue
    path = os.path.join(folder, safe_name("%s - %s" % (owner, b.name)) + ".stl")
    rec = {"file": os.path.basename(path)}
    try:
        opts = em.createSTLExportOptions(b, path)
        opts.meshRefinement = level
        opts.isBinaryFormat = True
        em.execute(opts)
        rec["bytes"] = os.path.getsize(path) if os.path.isfile(path) else 0
    except Exception as e:
        rec["error"] = str(e)
    files.append(rec)
    flush()

failed = [f for f in files if not f.get("bytes")]
print("%d STL files in %s%s" % (len(files) - len(failed), folder,
                                "; %d failed" % len(failed) if failed else ""))
