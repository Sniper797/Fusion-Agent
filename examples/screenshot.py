"""Save images of the viewport, from chosen directions and on a transparent background if wanted.
The camera and visibility are restored afterwards.

    python run_job.py examples/screenshot.py
    python run_job.py examples/screenshot.py --args "{\"path\": \"C:/img/view.png\", \"hide\": [\"Gear\"]}"
    python run_job.py examples/screenshot.py --args "{\"views\": [\"front\", \"iso\"], \"transparent\": true}"

args:
    path         default <agent folder>/screenshot.png; with several views each gets a suffix (_front, _iso...)
    width        default 1600
    height       default 1000
    views        list of: current, front, back, top, bottom, left, right, iso (top right),
                 iso-top-left, iso-bottom-left, iso-bottom-right (default ["current"]).
                 They follow Fusion's ViewCube, so "front" is whatever the ViewCube calls front.
    fit          fit the whole model in view first (default true)
    transparent  transparent background, for posters and slides (default false)
    hide         occurrence or component names to hide just for the shots

Images come from the ACTIVE document's window, so this example does not follow --doc.
"""
import os

O = adsk.core.ViewOrientations
VIEWS = {"front": O.FrontViewOrientation, "back": O.BackViewOrientation, "top": O.TopViewOrientation,
         "bottom": O.BottomViewOrientation, "left": O.LeftViewOrientation, "right": O.RightViewOrientation,
         "iso": O.IsoTopRightViewOrientation, "iso-top-right": O.IsoTopRightViewOrientation,
         "iso-top-left": O.IsoTopLeftViewOrientation, "iso-bottom-left": O.IsoBottomLeftViewOrientation,
         "iso-bottom-right": O.IsoBottomRightViewOrientation}

if not doc.isActive:                      # --doc pins another document: its window is not the one shown
    raise RuntimeError("screenshot.py captures the active window; make %r active first, or run it without --doc"
                       % doc.name)
vp = app.activeViewport
path = args.get("path") or os.path.join(agent_dir, "screenshot.png")
views = args.get("views") or ["current"]
unknown = [v for v in views if v != "current" and v not in VIEWS]
if unknown:
    raise ValueError("unknown view %s; use: current, %s" % (unknown, ", ".join(VIEWS)))
os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
cam0 = vp.camera
cam0.isSmoothTransition = False
hidden = []
out["images"] = []
try:
    for name in args.get("hide", []):
        o = find_occurrence(name)
        if o.isLightBulbOn:
            o.isLightBulbOn = False
            hidden.append(o)
    for view in views:
        vp.camera = cam0                     # every view starts from the user's own camera
        cam = vp.camera
        cam.isSmoothTransition = False
        if view != "current":
            cam.viewOrientation = VIEWS[view]
        if args.get("fit", True):
            cam.isFitView = True
        vp.camera = cam
        vp.refresh()
        base, ext = os.path.splitext(path)
        target = path if len(views) == 1 else "%s_%s%s" % (base, view, ext or ".png")
        opts = adsk.core.SaveImageFileOptions.create(target)
        opts.width, opts.height = int(args.get("width", 1600)), int(args.get("height", 1000))
        opts.isAntiAliased = True
        opts.isBackgroundTransparent = bool(args.get("transparent", False))
        ok = vp.saveAsImageFileWithOptions(opts) and os.path.isfile(target)
        out["images"].append({"view": view, "path": target, "ok": bool(ok),
                              "bytes": os.path.getsize(target) if ok else 0})
        flush()
finally:
    for o in hidden:
        o.isLightBulbOn = True
    cam0.isSmoothTransition = False
    vp.camera = cam0
    vp.refresh()

out["ok"] = all(i["ok"] for i in out["images"])
out["path"] = out["images"][0]["path"] if out["images"] else path
for i in out["images"]:
    print(("saved " if i["ok"] else "FAILED ") + i["path"])
