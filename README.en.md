# FusionAgent: drive Autodesk Fusion with Python scripts from outside it

A small Autodesk Fusion add-in that lets any program outside Fusion read the open design, measure it and edit it, with no screen and no clicks. That program can be a terminal, Claude Code, or any other AI agent.

**How it works:**
1. You write a Python script and run it with `run_job.py`.
2. `run_job.py` drops a "job" into a folder, and the add-in inside Fusion picks it up within half a second.
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
| `install-windows.bat` | Copies the add-in into place on Windows |
| `examples/` | Ready-made examples |
| `README.md` | This guide in Arabic |
| `README.en.md` | This guide in English |
| `CLAUDE.md` | Instructions for Claude Code, if you open it in this folder |

## Installation (once)

You need Autodesk Fusion, and Python 3.7 or newer to run `run_job.py`. `run_job.py` needs no extra libraries.

**Get the package:** from GitHub, or from a zip someone sent you.

```
git clone https://github.com/Sniper797/fusion-agent.git
cd fusion-agent
```

**On Windows:**
1. Run `install-windows.bat`.
2. In Fusion open **Utilities > Add-Ins** (or Shift+S), find **FusionAgent** in the list, and start it with its run button or the switch next to it. This window looks slightly different between Fusion versions. From then on it starts by itself every time Fusion starts.
3. Check that it works: `python run_job.py ping` prints the name of the open document.

> **`run_job.py` does not go into Fusion.** Don't add it to the Scripts and Add-Ins list; running it from there does nothing. It is the sender: it runs from a terminal (cmd, PowerShell or Terminal), or Claude Code runs it. The only thing that goes into Fusion is the `FusionAgent` add-in.

**On macOS:** copy the `FusionAgent` folder to
`~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`
then do steps 2 and 3.

**Updating:** run `git pull`, run `install-windows.bat` again, then restart Fusion. The add-in is a copy inside Fusion's folder, so it does not update when the repository does.

## Usage

```
python run_job.py ping
python run_job.py examples/inspect_design.py
python run_job.py examples/check_clearance.py --args "{\"only\": [\"Gear\"]}"
python run_job.py examples/export_stl.py --args-file my_args.json --save result.json
```

**What it prints:** what the script printed, then `out`. If the script fails, it prints the full error.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Done |
| 1 | The script raised an error |
| 2 | The add-in is not running, or the input is wrong |
| 3 | The add-in is busy with another job |
| 4 | Timed out |

## Examples

| File | What it does | Changes the design? |
|---|---|---|
| `inspect_design.py` | Components, bodies (volume, size, material), joints and parameters | No |
| `check_clearance.py` | Interference and smallest gap between every two bodies. Refuses a large assembly until you narrow it with `only` | No |
| `backup_f3d.py` | An .f3d copy of the open document | No |
| `export_stl.py` | An STL for every visible body | No |
| `screenshot.py` | A picture of the view; parts can be hidden for the shot and shown again | No, and it restores the camera |
| `demo_scene.py` | A **new** document with a mount, a pin and a gear to experiment on | Only creates a new document |
| `snap_fit.py` | Turns the pin into a snap-fit, and opens a bore with a catch groove in the part | Yes, as one group at the end of the timeline |

**Try the snap-fit without touching your own designs:**

```
python run_job.py examples/demo_scene.py
python run_job.py examples/check_clearance.py
python run_job.py examples/snap_fit.py --args-file examples/demo_args.json
python run_job.py examples/check_clearance.py
```

- **The first check** finds the pin inside the gear: 904.779 mm³ of interference.
- **After the snap-fit:** zero interference, and a smallest gap of 0.2 mm.
- **When you are done:** close the demo document without saving.

## Write your own script

A script is an ordinary Python file that runs inside Fusion, with these names ready:

| Name | What it is |
|---|---|
| `app`, `ui`, `adsk` | The Fusion application, its user interface, and the API library |
| `args` | What you sent with `--args` or `--args-file` |
| `out` | A dictionary that comes back to you as the result |
| `flush()` | Writes the result now. Call it after each step of a long job |
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
out["document"] = app.activeDocument.name
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
- **"busy":** another job is running right now, possibly from another session using the same add-in, since jobs run one at a time. If the wait drags on, a window open in Fusion, such as Scripts and Add-Ins, is usually holding the jobs until it is closed. If Fusion crashes during a job, the add-in cleans up after it the next time Fusion starts.
- **Timed out:** usually a dialog open in Fusion is blocking it, so close it. For long jobs use `--timeout 600`.
- **Paths inside `args`:** write them as `C:/...`. A Git Bash path such as `/c/Users/...` fails silently.
- **Fusion's interface freezes:** the script runs on Fusion's main thread, so the interface stops until it finishes. Split long work into parts.
- **Don't search cloud files inside a job.** A script that walked the `app.data` projects and folders froze Fusion ("Not Responding") twice, once for nine minutes and once for about forty, until it was restarted. If you need a file that is not open, open it yourself in Fusion first. If you know its path, go straight to it by name with `dataFolders.itemByName`, one level at a time.
- **Scripts work on the active document.** `design()` and the editing helpers take whatever document is in front of you right now. Before any script that edits, run `ping` and check the document's name.
- **A body whose volume can't be computed:** Fusion refuses to compute the volume of some bodies, or the distance to them, such as open surfaces and damaged geometry. The examples skip them and report them; use `try_value` in your own scripts.

## With Claude Code

Open Claude Code in this folder and ask for what you want in plain words, such as "check the open design for interference" or "export an STL of every body". `CLAUDE.md` explains the method and the rules to it.
