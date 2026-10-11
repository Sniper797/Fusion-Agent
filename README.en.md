# FusionAgent: drive Autodesk Fusion with Python scripts from outside it

A small Autodesk Fusion add-in that lets any program outside Fusion read the open design, measure it and edit it, with no screen and no clicks. That program can be a terminal, Claude Code, or any other AI agent.

**How it works:**
1. You write a Python script and run it with `run_job.py`.
2. `run_job.py` drops a "job" into a folder, and the add-in inside Fusion picks it up within about 0.05 s.
3. The add-in runs the script and sends the result back as JSON.

Arabic version: [README.md](README.md)

## What you can do with it

- **Read and measure a design.** Components, bodies, their volumes and sizes, joints and their health, in seconds, even for a 270-body assembly.
- **Check before printing.** Do two parts overlap? What is the smallest gap between them?
- **Edit a design precisely, then prove the edit with numbers.** For example, turning a pin into a snap-fit that a gear clicks onto and still turns on (`snap_fit.py`).
- **Export in one go.** An STL for every body, and an .f3d backup.
- **Pictures of the model** for reports and posts.
- **Generate parts from data.** For example, a medal with each student's name from a list. That one is not among the examples; you write its script yourself.
- **Anything the Fusion API allows.** The script is full Python running inside Fusion.

## What's in the package

| File | What it is |
|---|---|
| `FusionAgent/` | The add-in itself; it goes into Fusion's add-ins folder |
| `run_job.py` | Sends a script to Fusion and waits for the result |
| `install.py` | Copies the add-in into place on Windows and macOS, and tells you whether to restart it |
| `install-windows.bat` | The same on Windows, by double-click |
| `examples/` | Ready-made examples |
| `tools/bench_mcp.py` | Measures both routes with the same code: this add-in, and Fusion's built-in MCP |
| `tests/` | Tests that need no Fusion: `python -m unittest discover -s tests` |
| `README.md` | This guide in Arabic |
| `README.en.md` | This guide in English |
| `CLAUDE.md` | Instructions for Claude Code, if you open it in this folder |

## Installation (once)

You need Autodesk Fusion, and Python 3.8 or newer to run `run_job.py`. `run_job.py` needs no extra libraries.

**Get the package:** from GitHub, or from a zip someone sent you.

```
git clone https://github.com/Sniper797/fusion-agent.git
cd fusion-agent
```

**On Windows:**
1. Run `python install.py`, or double-click `install-windows.bat`.
2. In Fusion open **Utilities > Add-Ins** (or Shift+S), find **FusionAgent** in the list, and start it with its run button or the switch next to it. This window looks slightly different between Fusion versions. From then on it starts by itself every time Fusion starts.
3. Check that everything is fine: `python run_job.py doctor`. It checks Python, the installed and the running copy, a window holding the jobs, sends a test job, and says what to do about each problem. `python run_job.py ping` prints every document Fusion has loaded, and which one is active. That includes documents Fusion loads for referenced parts of your assembly even though you never opened them: one assembly with referenced parts had six of them besides its open tab.

> **`run_job.py` does not go into Fusion.** Don't add it to the Scripts and Add-Ins list; running it from there does nothing. It is the sender: it runs from a terminal (cmd, PowerShell or Terminal), or Claude Code runs it. The only thing that goes into Fusion is the `FusionAgent` add-in.

**On macOS:** run `python3 install.py`, which copies the folder to
`~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`
then do steps 2 and 3. This has not been tried on a Mac yet.

**Updating:** run `git pull`, then `python install.py`, then restart the add-in. The add-in is a copy inside Fusion's folder, so it does not update when the repository does. `run_job.py doctor` tells you when the installed or the running copy is older than the package. To restart the add-in without closing Fusion: Shift+S, then stop FusionAgent and run it again.

## Usage

```
python run_job.py ping
python run_job.py examples/inspect_design.py
python run_job.py examples/check_clearance.py --args "{\"only\": [\"Gear\"]}"
python run_job.py examples/export_stl.py --args-file my_args.json --save result.json
python run_job.py examples/inspect_design.py --doc "Car"
python run_job.py -c "print(design().rootComponent.name)"
python run_job.py -e "design().timeline.count"
python run_job.py examples/inspect_design.py --read-only
python run_job.py doctor
python run_job.py api offset wire
python run_job.py cancel <job id>
```

- `-c` sends code without a file, and `-e` sends one expression whose value comes back in `out["value"]`.
- **What it prints:** what the script printed, then `out`.
- **Long output** is cut at 8,000 characters (`--print-limit`), and the full result is saved to a file whose path is printed.
- **If the script fails,** it prints the error with your script's own lines only, plus a hint when it matches a known trap.
- **`api`** searches the Fusion API documentation installed on your computer, without contacting Fusion: the method, its signature and its description. About 3 s the first time to build the index, then under a second.
- **The job id** is on the line starting with `job`. To stop a job: `cancel` with that id, or Ctrl+C.
  - **Queued:** it is dropped and never runs. On Windows, Ctrl+Break works like Ctrl+C.
  - **Running:** it stops at its next `flush()` or `check_cancel()` and returns what it got to.
  - **Timeout before it started** (a window holding Fusion, say): it is cancelled, so it never runs later with nobody waiting.
  - **Timeout while it runs:** it is stopped only with `--cancel-on-timeout`, because an edit stopped halfway leaves half a change.
- **After every job** it prints what changed in the document; see "Queue and change receipt" below.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Done |
| 1 | The script raised an error |
| 2 | The add-in is not running, or the input is wrong, including a `--doc` name that matches no open document |
| 3 | The add-in is busy with another job. Only with add-ins older than 1.2.0; newer ones queue the job |
| 4 | Timed out, or you stopped waiting. The job may still be running; its result lands in `results/<id>.json` |
| 5 | Refused by the pre-flight check, so it was not sent |
| 6 | A job sent with `--read-only` changed something. Nothing was undone |
| 7 | The job really was cancelled: it stopped, or it was taken out of the queue before it started. It prints what it got to |

## Choose the document: `--doc`

By default a script works on the active document, the one in front of you. `--doc` pins it to an open document by name, even when that document is not the active one:

```
python run_job.py examples/inspect_design.py --doc "Car"
```

- **The name** is the tab name ("Car v12"), or the name without the version ("Car"), which keeps working after a save. Spaces and letter case don't matter.
- **Where it looks:** only open documents, including the ones Fusion loads for referenced parts of an assembly. It never searches the cloud.
- **What follows the pin:** `design()` and every helper, and `doc` in the script is that document.
- **Pictures:** `save_viewport` refuses when the pinned document is not the active one, because it captures the visible window.
- **What it prints:** `run_job.py` names the document the job ran on, and warns when the active document changed during the job.
- **For a whole session:** put the name in the `FUSION_AGENT_DOC` environment variable.
- It needs add-in 1.1.0 or newer.

## Queue and change receipt

With add-in 1.2.0 or newer:

- **Queue:** when another job is running, from your session or another one, yours waits its turn and prints "in line: 1 job(s) ahead". `--timeout` covers the wait. If it runs out first, the job is taken out of the queue and never runs. If the client dies, the add-in drops the job once its time is up.
- **Speed:** the add-in checks the folder every 0.05 s instead of 0.5. A tiny job from the terminal went from 0.49 s to 0.13 s, and the protocol alone from 0.51 s to 0.06 s. A check pass costs about 0.1% of one core.
- **Change receipt:** after every job, `run_job.py` prints what changed in the active and the pinned document: a document opened or closed, the active document, the modified flag, the timeline's length and marker, components, bodies and user parameters. When nothing did, it says `changes: none seen`.
- **The receipt compares geometry by default** (add-in 1.5.0): every body's volume, surface area, face and edge counts and centre of mass, and every part's placement in the assembly. The centre of mass catches what changes no volume and no count: a hole that slid, a body moved inside its component. On ARIES it caught a body moved 3 mm inside its component, a cube cut from the edge of another (-62.5 mm3, +50 mm2, +5 faces, centre moved 0.024 mm) and a part moved 2 mm.
- **Why by default:** counting alone leans on the modified flag, which cannot change in a document that is already modified. On a modified ARIES copy a part was moved 2 mm: the light receipt said nothing changed, the default one said `Table Ears:1: moved [-2.0, 0.0, 0.0] mm`.
- **Cost and limit:** on ARIES (633 bodies) a job took 0.42 s, against 0.13 s counting only. A design over 2000 bodies is counted, not measured, and the receipt names it. `--full-receipt` measures with no limit, `--light-receipt` counts only, `--no-receipt` skips the receipt. With an add-in older than 1.5.0 the default stays counting.
- **When it cannot measure:** the receipt says a document's geometry was not measured, and does not count that as a change, so a `--read-only` job does not fail on it.
- **What the receipt cannot see:** the joint solver runs after the job ends, so a part moved by a joint does not show in that job's receipt. To be sure, measure again in a separate job a few seconds later.
- **`--read-only`:** if the receipt sees any change, the job fails with exit code 6. This detects after the fact; it undoes nothing.
- **Journal:** every finished job adds a line to `journal.jsonl` in the add-in's folder: who sent it (folder and process), which script, on which document, what changed, and the error type. Useful when several sessions share the add-in.

## Pre-flight check

`run_job.py` reads the script before sending it, without contacting Fusion.

**It refuses** (exit code 5) these, because each one has frozen Fusion or damaged a design before:
- Walking cloud folders, `dataHubs`, `dataProjects`, `dataFolders` and `dataFiles`: in a loop, through an alias (`files = folder.dataFiles`), in a function called inside a loop or calling itself, or with `sorted`, `list` and the like. Going straight to a folder by name, one level at a time with `itemByName`, is allowed.
- `computeAll()`.
- Anything that waits for a click in Fusion: `messageBox`, `inputBox`, the file pickers, and `selectEntity`.

**It only warns** about `allJoints`, `documents.open` without `False`, `save` and `saveAs`, `close(True)` and `close(False)`, and active-document names when you use `--doc`.

- **To override:** `--allow modal-ui` (or the rule's name), or `--no-preflight`.
- **Git Bash paths** such as `/c/Users/...` in `args` are rewritten to `C:/Users/...`, and it says so.
- The check reads the code without running it, so it can miss things. It does not replace reading the script.

## Examples

| File | What it does | Changes the design? |
|---|---|---|
| `inspect_design.py` | Components, bodies (volume, size, material), joints and parameters | No |
| `check_clearance.py` | Interference and smallest gap between every two bodies. Refuses a large assembly until you narrow it with `only` | No |
| `backup_f3d.py` | An .f3d copy of the open document | No |
| `export_stl.py` | An STL for every visible body **where it sits in the assembly**: Fusion writes a body in its component's coordinates, so it is moved and checked against the body's box | No |
| `screenshot.py` | Pictures from the directions you choose (`views`: front, top, iso...), on a transparent background if you want (`transparent`); parts can be hidden for the shots | No, and it restores the camera |
| `selection.py` | What you have selected in Fusion right now, measured, with a token per item for a later job: `design().findEntityByToken(token)`. Check that it returns exactly one entity: none if it was deleted, two if a later feature split it | No |
| `demo_scene.py` | A **new** document with a mount, a pin and a gear to experiment on | Only creates a new document |
| `snap_fit.py` | Turns the pin into a snap-fit, and opens a bore with a catch groove in the part | Yes, as one group at the end of the timeline |
| `laser_dxf.py` | A laser DXF for every flat part, cutting sheets per thickness, and each file checked against the model | No |

**Try the snap-fit without touching your own designs.** Run these steps without `--doc` and with `FUSION_AGENT_DOC` unset: `demo_scene.py` makes the new document the active one, and the steps after it work on the active document.

```
python run_job.py examples/demo_scene.py
python run_job.py examples/check_clearance.py
python run_job.py examples/snap_fit.py --args-file examples/demo_args.json
python run_job.py examples/check_clearance.py
```

- **The first check** finds the pin inside the gear: 904.779 mm³ of interference.
- **After the snap-fit:** zero interference, and a smallest gap of 0.2 mm.
- **When you are done:** close the demo document without saving.

### Laser files: `laser_dxf.py`

```
python run_job.py examples/laser_dxf.py
python run_job.py examples/laser_dxf.py --args "{\"bed\": [600, 400]}"
python run_job.py examples/laser_dxf.py --args "{\"demo\": true}"
python run_job.py examples/laser_dxf.py --doc "ARIES" --args "{\"exclude\": [\"REF\", \"VEX\"], \"sheet_mm\": {\"Base\": 6}}"
python run_job.py examples/laser_dxf.py --args "{\"kerf_mm\": 0.1}"
```

- **What counts as a part:** a solid with two parallel flat faces and every other face square to them. Parts thicker than 20 mm (`max_thickness`) are skipped, with the reason. So are parts with a pocket, step or chamfer: their volume differs from outline area x thickness by more than 0.2%.
- **Shape alone cannot tell a laser part:** an aluminium VEX plate or a bearing model is as much a prism as an acrylic panel. Pick with `only`, or leave out with `exclude` (part of the name, e.g. `["REF", "VEX"]`).
- **`sheet_mm`:** when the sheet differs from the model's thickness, e.g. `{"Base": 6}`. The outline stays; the part goes on the 6 mm sheet.
- **What it writes:** a DXF in mm for each part, from its larger flat face: real lines, arcs and circles, other curves as short lines. Then cutting sheets the size of the laser bed (`bed`, 1300 x 900 by default) per thickness, parts in rows 5 mm apart, turned 90 degrees when that is what fits.
- **What it checks:** the cut length in the file against the face's edge lengths, the area inside the outline against the face's area, and it reads each file back. On the demo shapes the largest disagreement was 0.0092%; on a plate inside a component turned 30 degrees and moved, 0.0017%.
- **On a real assembly** (ARIES, 633 bodies, 1.3 s, read-only): it found 13 prisms, VEX plates and bearing models among them. With `exclude` and `sheet_mm`, the three acrylic panels remained, on two sheets, 2.7 mm and 6 mm, with a worst disagreement of 0.0017%.
- **`kerf_mm`:** bakes the cut width into the files, the Center's way: outlines move out and holes in by half of it, with Fusion's own wire offset, and sharp corners stay sharp. Set the laser's offset to 0. A 20 mm square coupon comes with it: cut it first and measure it; real kerf = 20 + `kerf_mm` - your reading. Then it measures the distance from every moved loop to the model's curves: on ARIES with 0.10, the median and the minimum were exactly 0.050 mm.
- **Orientation:** each part is drawn from its face that looks out of the assembly, so it never comes out mirrored. A standing or sloping panel is drawn upright as in the model, with the modeling up from your Fusion preferences as up. If the model was made the other way, pass `up`, e.g. `[0, 1, 0]`.
- **`demo`:** builds three shapes in memory, so you can try it without any design.
- **Layout on the sheet (`layout`):** `maxrects` by default: each part, biggest first, goes where it fills the free space most snugly, turned 90 degrees when that fits better. On 200 random sets it needed 29% fewer sheets than rows (317 against 445). `rows` gives the old rows back. Each sheet reports how much of it is material, from the parts' real area, not their rectangles (`material_of_bed_pct`, and `material_of_used_area_pct` for the part of the bed in use).
- **Not done yet:** packing by the parts' true outlines. Parts are packed by their bounding rectangles, so an L-shaped part reserves its whole rectangle.

## Write your own script

A script is an ordinary Python file that runs inside Fusion, with these names ready:

| Name | What it is |
|---|---|
| `app`, `ui`, `adsk` | The Fusion application, its user interface, and the API library |
| `args` | What you sent with `--args` or `--args-file` |
| `doc` | The document the script works on: the one `--doc` pins, otherwise the active one |
| `out` | A dictionary that comes back to you as the result |
| `flush()` | Writes the result now. Call it after each step of a long job; it is also where a cancelled job stops |
| `check_cancel()` | Stops here if the job was cancelled, without writing anything |
| `agent_dir` | The add-in's folder, and the default place for output files |
| `design()` | The open design |
| `find_occurrence(name)` | A component by its name |
| `bbox_mm(e)`, `mm()`, `cm()`, `point_mm(x, y, z)` | Sizes and unit conversion |
| `try_value(fn, default)` | The value, or the fallback when Fusion refuses to compute it |
| `to_component_space`, `add_tool_and_combine`, `require_timeline_at_end`, `group_timeline` | Tools for editing safely; `snap_fit.py` shows how to use them |
| `save_viewport(path, w, h, fit)`, `safe_name(text)` | A picture of the view, and a valid file name |

- Whatever you `print` comes back to you too.
- Import standard modules such as `os` and `math` yourself.
- **Fusion's internal length unit is the centimetre**, so convert with `mm()` and `cm()`.

```python
# hello.py
d = design()
out["document"] = doc.name
out["occurrences"] = d.rootComponent.allOccurrences.count
print("ok")
```

Then: `python run_job.py hello.py`

## Rules that protect your design

1. **Make a backup before any edit:** `backup_f3d.py`.
2. **Read before you edit.** Measure the dimensions from the file itself; don't rely on memory.
3. **Add at the end of the timeline, and gather the additions in one group.** You can delete the group in one click if you don't like the result.
4. **Prove the edit with numbers:** run `check_clearance.py` afterwards.
5. **The add-in never saves your documents.** Saving is up to you.

## Security: read this

The add-in runs **any** Python file a job points to, with your account's rights on the computer. So anyone who can write to its folder can run code on your machine.

- **Where the folder is:** `%LOCALAPPDATA%\FusionAgent` on Windows, and `~/Library/Application Support/FusionAgent` on macOS. Both are inside your own account.
- **Don't move it to a shared or synced folder** such as OneDrive or Dropbox. The `FUSION_AGENT_DIR` variable can change its location, so be careful where you point it.
- **Don't run a script you haven't read.**
- **To stop it:** Shift+S, then FusionAgent, then **Stop**, and untick **Run on Startup**.

## Known issues

- **"FusionAgent is not running":** Fusion is closed, or the add-in is stopped. Start it from Shift+S.
- **"busy":** only with add-ins older than 1.2.0: another job is running right now, possibly from another session. With 1.2.0 the job waits its turn in the queue. If a window open in Fusion, such as Scripts and Add-Ins, is holding the jobs, `run_job.py` names it after three seconds, and the job starts by itself once you close it. If Fusion crashes during a job, the add-in cleans up after it the next time Fusion starts.
- **Timed out:** usually a dialog open in Fusion is blocking it, so close it. For long jobs use `--timeout 600`.
- **Paths inside `args`:** write them as `C:/...`. A Git Bash path such as `/c/Users/...` used to fail silently; `run_job.py` now rewrites it.
- **Fusion's interface freezes:** the script runs on Fusion's main thread, so the interface stops until it finishes. Split long work into parts.
- **Don't search cloud files inside a job.** A script that walked the `app.data` projects and folders froze Fusion ("Not Responding") twice, once for nine minutes and once for about forty, until it was restarted. The pre-flight check now refuses this. If you need a file that is not open, open it yourself in Fusion first. If you know its path, go straight to it by name with `dataFolders.itemByName`, one level at a time.
- **Scripts work on the active document.** `design()` and the editing helpers take whatever document is in front of you right now. When the document matters, pin the job to it with `--doc`.
- **A body whose volume can't be computed:** Fusion refuses to compute the volume of some bodies, or the distance to them, such as open surfaces and damaged geometry. The examples skip them and report them; use `try_value` in your own scripts.

## Compared with Fusion's built-in MCP

Fusion ships an official MCP server, switched on in Preferences > General > API. We measured both routes with the same code on a 633-body design, on 10 October 2026:

| | FusionAgent | Built-in MCP |
|---|---|---|
| Round trip for a tiny job | 0.06 s for the protocol alone, 0.13 s from the terminal (1.2.0; it was 0.5 s) | 0.03 to 0.05 s |
| Inspecting 633 bodies | Identical result body by body; the work inside Fusion takes the same time | |
| Progress during a long job | Step by step through `flush()` | Nothing until the end |
| Errors | Your script's lines and source, plus a structured error | Line numbers without source |
| Scripts are files with `args` | Yes | No, code text sent every time |
| Runs while a command such as Sketch is open | No | Yes, read-only |
| Prints what changed after every job | Yes | No |
| Laser DXF files from the design | Yes, `laser_dxf.py` | No |

Re-measure on your machine: `python tools/bench_mcp.py`. It finds the MCP port by itself and only reads.

## With Claude Code

Open Claude Code in this folder and ask for what you want in plain words, such as "check the open design for interference" or "export an STL of every body". `CLAUDE.md` explains the method and the rules to it.
