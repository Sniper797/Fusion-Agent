# Driving Autodesk Fusion with FusionAgent

You cannot see or click Fusion from here. Drive it by writing a Python script and
running it inside Fusion through the FusionAgent add-in:

    python run_job.py ping                          # alive? which document is open?
    python run_job.py path/to/script.py --args "{\"key\": 1}"
    python run_job.py path/to/script.py --args-file args.json --timeout 300
    python run_job.py path/to/script.py --doc "Car"   # run on that open document, not the active one
    python run_job.py -e "design().timeline.count"    # quick probe, value in out["value"]
    python run_job.py -c "print(doc.name)"            # code without a file
    python run_job.py path/to/script.py --read-only   # exit 6 if it changed anything
    python run_job.py doctor                          # first thing to run when something looks wrong
    python run_job.py api offset wire                 # the installed Fusion API, offline: signature + doc
    python run_job.py cancel JOB_ID                   # the id is on the line starting with "job"

The script runs on Fusion's main thread. Ready-made names: `app`, `ui`, `adsk`,
`args` (dict), `doc` (the pinned or active document), `out` (dict returned to you), `flush()` (write progress now),
`agent_dir`, `design()`, `find_occurrence(name)`, `bbox_mm(e)`, `mm()`, `cm()`,
`point_mm(x, y, z)`, `try_value(fn, default)`, `to_component_space(temp_body, occ)`,
`add_tool_and_combine(comp, body, tool, "cut" | "join" | "intersect", name)`,
`require_timeline_at_end()`, `group_timeline(start, name)`,
`save_viewport(path, w, h, fit)`, `safe_name(text)`, `check_cancel()`. `print()` output comes back too.
Import standard modules (`os`, `math`, ...) yourself. Working examples are in `examples/`.

## Rules

1. Fusion's internal length unit is the centimetre. Convert at the edges with `mm()`, `cm()`, `point_mm()`.
2. Read before you write. Inspect first (`examples/inspect_design.py`) and measure the real dimensions in the file. Never trust a number from memory, not even the user's: ask when the file and the user disagree.
3. Back up before editing: `examples/backup_f3d.py`.
4. Edit the way `examples/snap_fit.py` does: build `TemporaryBRepManager` bodies in world space, move them with `to_component_space`, apply them with `add_tool_and_combine`. Call `require_timeline_at_end()`, record `design().timeline.count` before the edit and call `group_timeline(start, name)` after, so the user can delete the whole change in one click.
5. Prove the edit with numbers: `examples/check_clearance.py` (interference in mm3, minimum gap), `body.isSolid`, joint health. Joints earlier in the timeline stay healthy even when later features cut the faces they reference.
6. Never save, close or discard the user's documents unless asked. Ask before opening a new document while the user may be working in Fusion.
7. Paths inside args are `C:/...` style. run_job.py rewrites Git Bash `/c/...` paths and says so.
8. Fill an existing hole slightly oversize (+0.05 mm): an exact-radius fill can leave a ~0.1 um slit. `body.volume` carries ~2 ppm noise, so do not chase sub-mm3 differences on large bodies.
9. Some bodies refuse volume or distance queries (open surfaces, damaged meshes). Wrap such calls in `try_value`.
10. Pairwise checks grow as n squared. On large assemblies narrow `check_clearance.py` with `only`.
11. With add-in 1.2.0+ jobs wait in line ("in line: N job(s) ahead") and `--timeout` covers the wait. Exit code 3 (busy) only comes from older add-ins: wait and retry. If a job sits in `job.running` and never starts, a modal window is open in Fusion (Scripts and Add-Ins, a save dialog...). Ask the user to close it; never delete another session's job. run_job.py names the blocking window after 3 s.
12. Never walk cloud folders inside a job (recursing `app.data` hubs, projects, folders or file properties). It froze Fusion ("Not Responding") for 9 minutes once and about 40 minutes another time, waiting on the network, until the user restarted it. If a file is not open, ask the user to open it. If its path is known, resolve it level by level with `dataFolders.itemByName`. run_job.py refuses the common forms of such a walk before sending it (exit 5): loops, aliases, helper functions called in loops, recursion. It is a heuristic, so do not rely on it, and never add `--allow cloud-walk` on your own.
13. Pin every edit with `--doc NAME` (add-in 1.1.0+). `design()`, `doc` and every helper then follow that document even if another session switches tabs, and `save_viewport` refuses when it is not the active one. Without `--doc` everything acts on the ACTIVE document. `ping` lists every document Fusion has loaded, including the ones it loads for referenced parts of an assembly (ARIES: 6 besides its tab; the built-in MCP listed only the tab). `isVisible` was True for all of them, so it does not tell a referenced part from a tab. Never edit a referenced part's document unless the user asked: the change lands in another file they may not have open. A wrong --doc name exits 2.
14. The joint solver runs after the job ends, so checks inside the job cannot see it. To confirm jointed parts did not move, measure again in a separate job a few seconds later.
15. Exit code 5: the pre-flight check refused the script. Fix the script instead of adding `--allow`, unless the user agrees. Warnings print before the job runs; read them.
16. Output longer than 8,000 characters is cut, and the full result's path is printed. Read that file, or return less in `out`.
17. Read the receipt (`--- changes ---`) after every job. Send jobs that should only read with `--read-only`: exit 6 means something changed, and nothing was undone. The receipt counts documents, components, bodies, timeline and parameters; it does not compare geometry, so a moved part or a feature dimension may show only as isModified.
18. `journal.jsonl` in the agent folder has one line per finished job from every session: read it before assuming who changed a document.
19. Laser parts: `examples/laser_dxf.py` writes a DXF per flat part plus cutting sheets per thickness, and checks each file against the model (`cut_length_error_pct`, `area_error_pct`, measured at 0.00-0.01 %). `kerf_mm` bakes the kerf in (outlines out, holes in, by half; laser offset 0) and writes a 20 mm coupon: real kerf = 20 + kerf_mm - reading; read `kerf_offset_check_mm` (median and min must equal the target). No real nesting; say so when handing the files over. Geometry cannot tell an acrylic panel from a VEX plate or a bearing model: on ARIES 13 bodies passed, 3 were laser parts. Ask which parts are cut, then pass `only` or `exclude`, and `sheet_mm` when the model is thinner or thicker than the sheet (ARIES base: model 2.4 mm, cut from 6 mm).
20. Look the API up with `python run_job.py api WORDS` before guessing a method or its arguments: it reads the Fusion actually installed, offline. Example: `createWireFromCurves` takes a Python list, not an ObjectCollection.
21. STL: `examples/export_stl.py` moves bodies that Fusion wrote in their component's coordinates into the assembly and reports `written_in` and `box_error_mm`; use `frame: component` only when asked.
22. When the user says "this face" or "the selected part", run `examples/selection.py`, then resolve the tokens with `design().findEntityByToken(token)` in the editing job and stop unless it returns exactly one entity (none: deleted; two: split by a later feature). The selection is the active document's.
23. Long jobs: call `flush()` after each step. It writes progress and is where a cancelled job stops (exit 7, partial `out` kept). In an edit, call `group_timeline` in a `finally:` (as `snap_fit.py` does), so an edit stopped halfway is still one group to delete. Never wrap the whole script in a bare `except:`, which would swallow the cancel. A timeout stops a running job only with `--cancel-on-timeout`; exit 4 means it may still be running.
