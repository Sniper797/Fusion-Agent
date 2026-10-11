"""Laser-cut DXF files from the flat parts of the open design (read-only for the design).

    python run_job.py examples/laser_dxf.py
    python run_job.py examples/laser_dxf.py --doc "Tower" --args "{\"bed\": [600, 400]}"
    python run_job.py examples/laser_dxf.py --args "{\"demo\": true}"     no design needed

A part is a solid with two parallel flat faces a thickness apart and every other face
square to them: what a laser cuts from sheet. For each part this writes a DXF of its
larger flat face in mm (lines, arcs and circles as real arcs, other curves as short lines),
then lays the parts of each thickness out on sheets of the laser bed.

Every file is checked against the model: the cut length against the face's edge lengths,
and the area inside the outline against the face's area.

args:
    folder         default <agent folder>/laser/<document>
    only           list of component or body names to export (default: every visible solid)
    exclude        list of name parts to leave out, e.g. ["REF", "VEX"] (any case)
    sheet_mm       {"name part": thickness} for parts cut from a sheet thicker or thinner than the
                   model, e.g. {"Base": 6}: the outline stays, the sheet grouping uses this
    bed            [width, height] of the laser bed in mm (default [1300, 900])
    gap            mm between parts on a sheet (default 5)
    max_thickness  thicker parts are skipped, in mm (default 20)
    demo           true: export three built-in test shapes instead of the design

Not done here: kerf compensation (cut the coupon on your machine and offset in the laser
software) and real nesting (the sheets are simple rows, a fit check more than a layout).
"""
import math
import os

V = adsk.core.Vector3D.create
P = adsk.core.Point3D.create
TOL_CM = 0.0001                                  # 1 micron, for "same point" and "square to"

bed_w, bed_h = (float(x) for x in args.get("bed", [1300, 900]))
gap = float(args.get("gap", 5))
max_t = float(args.get("max_thickness", 20))
only = set(args.get("only") or [])
exclude = [x.lower() for x in args.get("exclude") or []]
sheet_mm = {k.lower(): float(v) for k, v in (args.get("sheet_mm") or {}).items()}
PRISM_TOL = 0.002          # volume may differ from outline area x thickness by 0.2 %, no more


# ------------------------------------------------------------------ the parts

def face_normal(face):
    ok, n = face.evaluator.getNormalAtPoint(face.pointOnFace)   # outward, unlike Plane.normal
    n.normalize()
    return n


def flat_part(body):
    """(cap face, its outward normal, thickness in mm) or (None, reason)."""
    planes = [f for f in body.faces if adsk.core.Plane.cast(f.geometry)]
    if len(planes) < 2:
        return None, "fewer than two flat faces"
    cap = max(planes, key=lambda f: f.area)
    n = face_normal(cap)
    o = cap.pointOnFace
    others = [f for f in planes if f != cap and face_normal(f).dotProduct(n) < -0.99999]
    if not others:
        return None, "no flat face opposite the largest one"
    depth = [abs(o.vectorTo(v.geometry).dotProduct(n)) for v in body.vertices] or [0]
    t_cm = max(depth)
    for f in body.faces:
        if f == cap or f in others:   # == compares the faces; `is` compares Python wrappers
            continue
        if abs(face_normal(f).dotProduct(n)) > 1e-4:
            return None, "a side face is not square to the sheet (%s)" % f.geometry.objectType.split("::")[-1]
    t_mm = t_cm * 10
    if t_mm > max_t:
        return None, "%.2f mm thick, over max_thickness %.0f mm" % (t_mm, max_t)
    ratio = try_value(lambda: body.volume / (cap.area * t_cm))
    if ratio is not None and abs(ratio - 1) > PRISM_TOL:     # a pocket, a step or a chamfer
        return None, ("not cut straight through: its volume is %.2f %% of outline x thickness "
                      "(a pocket, step or chamfer)" % (ratio * 100))
    return (cap, n, t_mm), None


def frame(face, n):
    """Origin and two in-plane axes, so (u, v, n) is right-handed: looking at the face from
    outside, the drawing is not mirrored."""
    plane = adsk.core.Plane.cast(face.geometry)
    u = plane.uDirection.copy()
    u.normalize()
    v = n.crossProduct(u)
    v.normalize()
    return plane.origin, u, v


def to2d(p, o, u, v):
    d = o.vectorTo(p)
    return (d.dotProduct(u) * 10.0, d.dotProduct(v) * 10.0)       # cm -> mm


def angle(c, p):
    return math.degrees(math.atan2(p[1] - c[1], p[0] - c[0])) % 360.0


def ccw_sweep(a0, a1):
    return (a1 - a0) % 360.0


def entities_of(face, n):
    """DXF entities for every edge of the face, in mm, plus the measured lengths."""
    o, u, v = frame(face, n)
    ents, model_len = [], 0.0
    for loop in face.loops:
        for co in loop.coEdges:
            e = co.edge
            model_len += e.length * 10.0
            ev = e.evaluator
            ok, t0, t1 = ev.getParameterExtents()
            ok, ps = ev.getPointAtParameter(t0)
            ok, pm = ev.getPointAtParameter((t0 + t1) / 2)
            ok, pe = ev.getPointAtParameter(t1)
            g = e.geometry
            line, arc, circle = adsk.core.Line3D.cast(g), adsk.core.Arc3D.cast(g), adsk.core.Circle3D.cast(g)
            if line:
                ents.append(("LINE", to2d(ps, o, u, v), to2d(pe, o, u, v)))
            elif circle:
                ents.append(("CIRCLE", to2d(circle.center, o, u, v), circle.radius * 10.0))
            elif arc:
                c = to2d(arc.center, o, u, v)
                a_s, a_m, a_e = (angle(c, to2d(p, o, u, v)) for p in (ps, pm, pe))
                if ccw_sweep(a_s, a_m) <= ccw_sweep(a_s, a_e):   # counter-clockwise start -> end
                    ents.append(("ARC", c, arc.radius * 10.0, a_s, a_e))
                else:
                    ents.append(("ARC", c, arc.radius * 10.0, a_e, a_s))
            else:                                               # splines, ellipses: short lines
                ok, pts = ev.getStrokes(t0, t1, 0.001)
                pts = [to2d(p, o, u, v) for p in pts]
                ents += [("LINE", a, b) for a, b in zip(pts, pts[1:])]
    return ents, model_len


def loop_area(face, n):
    """Area inside the outline minus the holes, from the model's own edges (mm2)."""
    o, u, v = frame(face, n)
    total = 0.0
    for loop in face.loops:
        pts = []
        for co in loop.coEdges:
            ev = co.edge.evaluator
            ok, t0, t1 = ev.getParameterExtents()
            ok, seg = ev.getStrokes(t0, t1, 0.0005)
            seg = [to2d(p, o, u, v) for p in seg]
            pts += list(reversed(seg)) if co.isOpposedToEdge else seg
        a = 0.5 * sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))
        total += abs(a) if loop.isOuter else -abs(a)
    return total


def length_of(ent):
    if ent[0] == "LINE":
        return math.dist(ent[1], ent[2])
    if ent[0] == "CIRCLE":
        return 2 * math.pi * ent[2]
    return math.radians(ccw_sweep(ent[3], ent[4])) * ent[2]


def bounds(ents):
    xs, ys = [], []
    for e in ents:
        if e[0] == "LINE":
            xs += [e[1][0], e[2][0]]
            ys += [e[1][1], e[2][1]]
        else:
            (cx, cy), r = e[1], e[2]
            if e[0] == "CIRCLE":
                xs += [cx - r, cx + r]
                ys += [cy - r, cy + r]
            else:                                   # the arc's ends, plus every axis it crosses
                a0, sweep = e[3], ccw_sweep(e[3], e[4])
                for a in [a0, e[4]] + [k * 90.0 for k in range(4) if ccw_sweep(a0, k * 90.0) <= sweep]:
                    xs.append(cx + r * math.cos(math.radians(a)))
                    ys.append(cy + r * math.sin(math.radians(a)))
    return min(xs), min(ys), max(xs), max(ys)


def moved(ents, dx, dy, turn=False):
    """Shift every entity; turn=True first rotates it 90 degrees counter-clockwise."""
    def pt(p):
        x, y = (-p[1], p[0]) if turn else p
        return (x + dx, y + dy)
    out_ = []
    for e in ents:
        if e[0] == "LINE":
            out_.append(("LINE", pt(e[1]), pt(e[2])))
        elif e[0] == "CIRCLE":
            out_.append(("CIRCLE", pt(e[1]), e[2]))
        else:
            out_.append(("ARC", pt(e[1]), e[2], (e[3] + (90 if turn else 0)) % 360, (e[4] + (90 if turn else 0)) % 360))
    return out_


# ---------------------------------------------------------------- DXF writer

def write_dxf(path, ents):
    """Minimal AutoCAD R12 DXF in mm, one CUT layer: laser software reads this everywhere."""
    f = lambda x: "%.6f" % x
    rows = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009", "9", "$INSUNITS", "70", "4",
            "0", "ENDSEC", "0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER", "70", "1",
            "0", "LAYER", "2", "CUT", "70", "0", "62", "1", "6", "CONTINUOUS", "0", "ENDTAB", "0", "ENDSEC",
            "0", "SECTION", "2", "ENTITIES"]
    for e in ents:
        if e[0] == "LINE":
            rows += ["0", "LINE", "8", "CUT", "10", f(e[1][0]), "20", f(e[1][1]), "30", "0",
                     "11", f(e[2][0]), "21", f(e[2][1]), "31", "0"]
        elif e[0] == "CIRCLE":
            rows += ["0", "CIRCLE", "8", "CUT", "10", f(e[1][0]), "20", f(e[1][1]), "30", "0", "40", f(e[2])]
        else:
            rows += ["0", "ARC", "8", "CUT", "10", f(e[1][0]), "20", f(e[1][1]), "30", "0", "40", f(e[2]),
                     "50", f(e[3]), "51", f(e[4])]
    rows += ["0", "ENDSEC", "0", "EOF"]
    with open(path, "w", encoding="ascii", newline="\r\n") as fh:
        fh.write("\n".join(rows) + "\n")


def read_back(path):
    """Count what the file holds, by parsing it again."""
    with open(path, encoding="ascii") as fh:
        rows = [r.strip() for r in fh.read().splitlines()]
    kinds = [rows[i + 1] for i in range(len(rows) - 1) if rows[i] == "0" and rows[i + 1] in ("LINE", "ARC", "CIRCLE")]
    return {k: kinds.count(k) for k in ("LINE", "ARC", "CIRCLE")}


# --------------------------------------------------------------- the bodies

def demo_bodies():
    """Three shapes built in memory, no document: a plate with a hole and a slot,
    a rounded tab, and a cylinder that is too thick to be a sheet part."""
    tbm = adsk.fusion.TemporaryBRepManager.get()
    box = lambda cx, cy, w, h, t: tbm.createBox(adsk.core.OrientedBoundingBox3D.create(
        P(cx / 10, cy / 10, t / 20), V(1, 0, 0), V(0, 1, 0), w / 10, h / 10, t / 10))
    cyl = lambda cx, cy, r, z0, z1: tbm.createCylinderOrCone(P(cx / 10, cy / 10, z0 / 10), r / 10,
                                                             P(cx / 10, cy / 10, z1 / 10), r / 10)
    plate = box(50, 30, 100, 60, 3)
    tbm.booleanOperation(plate, cyl(20, 30, 5, -1, 4), adsk.fusion.BooleanTypes.DifferenceBooleanType)
    slot = box(65, 30, 20, 8, 5)
    tbm.booleanOperation(slot, cyl(55, 30, 4, -1, 4), adsk.fusion.BooleanTypes.UnionBooleanType)
    tbm.booleanOperation(slot, cyl(75, 30, 4, -1, 4), adsk.fusion.BooleanTypes.UnionBooleanType)
    tbm.booleanOperation(plate, slot, adsk.fusion.BooleanTypes.DifferenceBooleanType)
    tab = box(150, 20, 40, 40, 3)
    tbm.booleanOperation(tab, cyl(150, 40, 20, 0, 3), adsk.fusion.BooleanTypes.UnionBooleanType)
    rod = cyl(250, 20, 10, 0, 50)
    return [("demo plate", plate), ("demo tab", tab), ("demo rod", rod)]


def design_bodies():
    root = design().rootComponent
    found = [(root.name, b) for b in root.bRepBodies]
    for o in root.allOccurrences:                 # proxies: geometry in assembly coordinates
        if o.isLightBulbOn:
            found += [(o.fullPathName, b) for b in o.bRepBodies]
    keep = []
    for owner, b in found:
        if not (b.isSolid and b.isVisible):
            continue
        if only and not ({owner.split(":")[0], owner, b.name} & only):
            continue
        name = "%s - %s" % (owner, b.name)
        if any(x in name.lower() for x in exclude):
            continue
        keep.append((name, b))
    return keep


# ---------------------------------------------------------------------- run

demo = bool(args.get("demo"))
bodies = demo_bodies() if demo else design_bodies()
label = "demo" if demo else doc.name
folder = args.get("folder") or os.path.join(agent_dir, "laser", safe_name(label))
os.makedirs(folder, exist_ok=True)

parts, skipped = [], []
out["folder"] = folder
out["parts"] = parts
out["skipped"] = skipped
for name, body in bodies:
    found, why = try_value(lambda: flat_part(body), (None, "could not be measured"))
    if not found:
        skipped.append({"body": name, "why": why})
        continue
    cap, n, t_mm = found
    ents, model_len = entities_of(cap, n)
    x0, y0, x1, y1 = bounds(ents)
    ents = moved(ents, -x0, -y0)
    path = os.path.join(folder, safe_name("%s %.2fmm" % (name, t_mm)) + ".dxf")
    write_dxf(path, ents)
    dxf_len = sum(length_of(e) for e in ents)
    area_model, area_dxf = cap.area * 100.0, loop_area(cap, n)
    vol_check = try_value(lambda: body.volume * 1000.0 / (cap.area * 100.0 * t_mm))
    sheet_t = next((v for k, v in sheet_mm.items() if k in name.lower()), round(t_mm, 3))
    parts.append({"body": name, "file": os.path.basename(path), "thickness_mm": round(t_mm, 3),
                  "sheet_mm": sheet_t,
                  "size_mm": [round(x1 - x0, 2), round(y1 - y0, 2)], "holes": cap.loops.count - 1,
                  "entities": read_back(path),
                  "cut_length_mm": round(dxf_len, 2),
                  "cut_length_error_pct": round(100 * abs(dxf_len - model_len) / model_len, 4),
                  "area_mm2": round(area_model, 2),
                  "area_error_pct": round(100 * abs(area_dxf - area_model) / area_model, 4),
                  "volume_over_area_x_t": round(vol_check, 5) if vol_check else None,
                  "_ents": ents, "_w": x1 - x0, "_h": y1 - y0})
    flush()

# Sheets: one set per thickness, parts in rows, turned 90 degrees when that is what fits.
sheets = []
for t in sorted({p["sheet_mm"] for p in parts}):          # the sheet it is cut from, not the model
    group = sorted((p for p in parts if p["sheet_mm"] == t), key=lambda p: -max(p["_w"], p["_h"]))
    pages, x, y, row_h = [[]], gap, gap, 0.0
    for p in group:
        w, h, turn = p["_w"], p["_h"], False
        if (w > bed_w - 2 * gap or h > bed_h - 2 * gap) and h <= bed_w - 2 * gap and w <= bed_h - 2 * gap:
            w, h, turn = h, w, True
        if w > bed_w - 2 * gap or h > bed_h - 2 * gap:
            p["fits_bed"] = False
            continue
        p["fits_bed"] = True
        if x + w > bed_w - gap:
            x, y, row_h = gap, y + row_h + gap, 0.0
        if y + h > bed_h - gap:
            pages.append([])
            x, y, row_h = gap, gap, 0.0
        pages[-1].append((p, x, y, turn))
        x, row_h = x + w + gap, max(row_h, h)
    for i, page in enumerate(p_ for p_ in pages if p_):
        ents = []
        for p, px, py, turn in page:
            e = p["_ents"]
            if turn:                                    # rotate, then bring back to (0, 0)
                e = moved(e, 0, 0, turn=True)
                bx0, by0, _, _ = bounds(e)
                e = moved(e, -bx0, -by0)
            ents += moved(e, px, py)
        path = os.path.join(folder, "sheet %.2fmm %d of %d.dxf" % (t, i + 1, len([q for q in pages if q])))
        write_dxf(path, ents)
        used = max(bounds(ents)[2], 1), max(bounds(ents)[3], 1)
        sheets.append({"file": os.path.basename(path), "thickness_mm": t, "parts": len(page),
                       "used_mm": [round(used[0], 1), round(used[1], 1)], "entities": read_back(path)})
out["sheets"] = sheets
out["bed_mm"] = [bed_w, bed_h]
for p in parts:
    for k in ("_ents", "_w", "_h"):
        p.pop(k, None)
too_big = [p["body"] for p in parts if p.get("fits_bed") is False]
worst = max([p["cut_length_error_pct"] for p in parts] + [p["area_error_pct"] for p in parts] or [0])
print("%d parts, %d skipped, %d sheets in %s" % (len(parts), len(skipped), len(sheets), folder))
print("largest disagreement with the model: %.4f %%" % worst)
if too_big:
    print("bigger than the %.0f x %.0f bed: %s" % (bed_w, bed_h, ", ".join(too_big)))
