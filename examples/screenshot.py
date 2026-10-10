"""Save an image of the viewport. Camera and visibility are restored afterwards.

    python run_job.py examples/screenshot.py
    python run_job.py examples/screenshot.py --args "{\"path\": \"C:/img/view.png\", \"hide\": [\"Gear\"]}"

args:
    path    default <agent folder>/screenshot.png
    width   default 1600
    height  default 1000
    fit     fit the whole model in view first (default true)
    hide    occurrence or component names to hide just for the shot
"""
import os

vp = app.activeViewport
path = args.get("path") or os.path.join(agent_dir, "screenshot.png")
cam0 = vp.camera
hidden = []
try:
    for name in args.get("hide", []):
        o = find_occurrence(name)
        if o.isLightBulbOn:
            o.isLightBulbOn = False
            hidden.append(o)
    out["ok"] = save_viewport(path, args.get("width", 1600), args.get("height", 1000),
                              fit=args.get("fit", True))
finally:
    for o in hidden:
        o.isLightBulbOn = True
    cam0.isSmoothTransition = False
    vp.camera = cam0
    vp.refresh()

out["path"] = path
print(("saved " if out["ok"] else "FAILED ") + path)
