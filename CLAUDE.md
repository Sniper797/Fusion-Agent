# Driving Autodesk Fusion with FusionAgent

You cannot see or click Fusion from here. Drive it by writing a Python script and
running it inside Fusion through the FusionAgent add-in:

    python run_job.py ping                          # alive? which document is open?
    python run_job.py path/to/script.py --args "{\"key\": 1}"
    python run_job.py path/to/script.py --args-file args.json --timeout 300

The script runs on Fusion's main thread. Ready-made names: `app`, `ui`, `adsk`,
`args` (dict), `out` (dict returned to you), `flush()` (write progress now),
`agent_dir`, `design()`, `find_occurrence(name)`, `bbox_mm(e)`, `mm()`, `cm()`,
`point_mm(x, y, z)`, `try_value(fn, default)`, `to_component_space(temp_body, occ)`,
`add_tool_and_combine(comp, body, tool, "cut" | "join" | "intersect", name)`,
`require_timeline_at_end()`, `group_timeline(start, name)`,
`save_viewport(path, w, h, fit)`, `safe_name(text)`. `print()` output comes back too.
Import standard modules (`os`, `math`, ...) yourself. Working examples are in `examples/`.

## Rules

1. Fusion's internal length unit is the centimetre. Convert at the edges with `mm()`, `cm()`, `point_mm()`.
2. Read before you write. Inspect first (`examples/inspect_design.py`) and measure the real dimensions in the file. Never trust a number from memory, not even the user's: ask when the file and the user disagree.
3. Back up before editing: `examples/backup_f3d.py`.
4. Edit the way `examples/snap_fit.py` does: build `TemporaryBRepManager` bodies in world space, move them with `to_component_space`, apply them with `add_tool_and_combine`. Call `require_timeline_at_end()`, record `design().timeline.count` before the edit and call `group_timeline(start, name)` after, so the user can delete the whole change in one click.
5. Prove the edit with numbers: `examples/check_clearance.py` (interference in mm3, minimum gap), `body.isSolid`, joint health. Joints earlier in the timeline stay healthy even when later features cut the faces they reference.
6. Never save, close or discard the user's documents unless asked. Ask before opening a new document while the user may be working in Fusion.
7. Paths inside args are `C:/...` style. A Git Bash `/c/...` path fails silently.
8. Fill an existing hole slightly oversize (+0.05 mm): an exact-radius fill can leave a ~0.1 um slit. `body.volume` carries ~2 ppm noise, so do not chase sub-mm3 differences on large bodies.
9. Some bodies refuse volume or distance queries (open surfaces, damaged meshes). Wrap such calls in `try_value`.
10. Pairwise checks grow as n squared. On large assemblies narrow `check_clearance.py` with `only`.
11. Exit code 3 (busy) is normal when another session shares the add-in: wait and retry. If a job sits in `job.running` and never starts, a modal window is open in Fusion (Scripts and Add-Ins, a save dialog...). Ask the user to close it; never delete another session's job.
12. Never walk cloud folders inside a job (recursing `app.data` hubs, projects, folders or file properties). It froze Fusion ("Not Responding") for 9 minutes once and about 40 minutes another time, waiting on the network, until the user restarted it. If a file is not open, ask the user to open it. If its path is known, resolve it level by level with `dataFolders.itemByName`.
13. `design()`, `group_timeline()` and `require_timeline_at_end()` act on the ACTIVE document. Check its name with `ping` before editing. If another session may switch documents, resolve the target from `app.documents` by name and group with that document's own `timeline.timelineGroups`.
14. The joint solver runs after the job ends, so checks inside the job cannot see it. To confirm jointed parts did not move, measure again in a separate job a few seconds later.
