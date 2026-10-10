"""Turn a round pin into a split snap-fit pin, and give the part that goes on it a
bore with an internal catch groove: the part clicks on, stays on, and still turns.

    python run_job.py examples/snap_fit.py --args-file examples/demo_args.json

args (mm, world coordinates):
    mount        component that owns the pin                          (required)
    part         component that snaps onto the pin                    (required)
    origin       [x, y, z] centre of the pin root on the mount face   (required)
    direction    [x, y, z] pin axis, pointing away from the mount     (required)
    pin_dia, pin_length                                               (required)
    part_offset  gap from the mount face to the part's back face (default 2)
    old_bore     {"dia", "depth"}: an existing blind hole in the part, filled first
    clearance    radial running gap, pin to bore (default 0.2)
    barb         how far the barb stands proud of the pin, radially (default 0.5).
                 On FDM prints 0.5 is about one nozzle line and comes out rounded;
                 0.8 held firmly in a test print with a 3 mm lead_in.
    lead_in      length of the barb's lead-in cone (default 2; longer pushes on easier)
    slot_width   default 3
    slot_normal  [x, y, z] normal of the slot plane. Default: horizontal, so the slot
                 plane is vertical and both halves carry the part's weight.
    mount_body, part_body   body names, only when a component has several solids
    group_name   timeline group name (default "Snap-fit")
    dry_run      true: compute and check everything, change nothing

What it builds, with u = distance from the mount face along the axis, L = pin_length:
    pin   the tip from u = L-lead_in-0.5 becomes a barb (pin_dia + 2*barb) with a flat
          catch face, coned down to pin_dia - 1.6 over lead_in; a slot runs the whole pin.
    part  the old bore is filled slightly oversize (an exact-size fill can leave a
          0.1 um slit); a bore of pin_dia + 2*clearance with a 0.6 mm mouth chamfer;
          a catch groove with 0.3 mm axial play whose 45-degree roof ends where the
          barb cone is a full clearance inside the bore; a 90-degree drill-point end.
          Both slopes print without support.
Then it measures the result: interference must be 0 and the smallest gap > 0.
"""
import math

tbm = adsk.fusion.TemporaryBRepManager.get()
UNION = adsk.fusion.BooleanTypes.UnionBooleanType


def unit(v):
    n = math.sqrt(sum(c * c for c in v))
    if n < 1e-9:
        raise ValueError("zero-length vector %r" % (v,))
    return [c / n for c in v]


def cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def one_solid_body(occ, wanted):
    bodies = [b for b in occ.bRepBodies if b.isSolid and (not wanted or b.name == wanted)]
    if len(bodies) != 1:
        raise RuntimeError("%s has %d matching solid bodies; name one with mount_body / part_body"
                           % (occ.component.name, len(bodies)))
    return bodies[0]


# ---------------------------------------------------------------- inputs
O = [float(c) for c in args["origin"]]
D = unit([float(c) for c in args["direction"]])
L = float(args["pin_length"])
r = float(args["pin_dia"]) / 2.0
off = float(args.get("part_offset", 2.0))
cl = float(args.get("clearance", 0.2))
barb = float(args.get("barb", 0.5))
sw = float(args.get("slot_width", 3.0))
lead = float(args.get("lead_in", 2.0))
play = 0.3
if lead <= 0:
    raise ValueError("lead_in must be positive")

if args.get("slot_normal"):
    n = unit([float(c) for c in args["slot_normal"]])
else:
    n = cross(D, [0.0, 0.0, 1.0])
    if math.sqrt(dot(n, n)) < 1e-6:          # vertical pin: any horizontal normal will do
        n = cross(D, [1.0, 0.0, 0.0])
    n = unit(n)
if abs(dot(n, D)) > 1e-6:
    raise ValueError("slot_normal must be perpendicular to direction")

barb_r, tip_r = r + barb, r - 0.8
barb_u, land_u = L - (lead + 0.5), L - lead
bore_r = r + cl
groove_r = barb_r + 0.25
taper = (barb_r - tip_r) / lead                  # radius the lead-in cone loses per mm
groove_u0 = barb_u - play
# The 45-degree roof must end where the barb cone is already a full clearance inside the
# bore, measured square to the cone. A long, shallow lead-in otherwise grazes the corner
# where the roof meets the bore (measured on a test coupon: 0.012 mm).
groove_u2 = max(land_u + play + (groove_r - bore_r),
                land_u + (barb_r - bore_r + cl * math.hypot(1.0, taper)) / taper)
groove_u1 = groove_u2 - (groove_r - bore_r)
bore_end = L + 0.5
point_end = bore_end + bore_r                    # 90-degree drill point
mouth_u0, mouth_u1, mouth_r0 = off - 0.4, off + 0.6, bore_r + 1.0

problems = []
if tip_r <= 0.5:
    problems.append("pin_dia is too small for the tip cone")
if barb <= cl:
    problems.append("barb (%.2f) must exceed clearance (%.2f) or nothing catches" % (barb, cl))
if sw > 2 * r - 2.0:
    problems.append("slot_width leaves halves under 1 mm thick")
if sw < 2 * (barb_r - bore_r) + 0.5:
    problems.append("slot_width is too narrow for the halves to close past the bore")
if barb_u - off < 3.0:
    problems.append("pin_length is too short: the barb would sit at the part's mouth")
if problems:
    raise ValueError("; ".join(problems))

mount_occ = find_occurrence(args["mount"])
part_occ = find_occurrence(args["part"])
part_proxy = one_solid_body(part_occ, args.get("part_body"))
one_solid_body(mount_occ, args.get("mount_body"))

bb = part_proxy.boundingBox
corners = [[mm(x), mm(y), mm(z)] for x in (bb.minPoint.x, bb.maxPoint.x)
           for y in (bb.minPoint.y, bb.maxPoint.y) for z in (bb.minPoint.z, bb.maxPoint.z)]
front_u = max(dot([c[i] - O[i] for i in range(3)], D) for c in corners)
if front_u < point_end + 1.0:
    raise ValueError("the bore would break through the part's front face: it needs %.1f mm, "
                     "the part reaches %.1f mm along the axis" % (point_end + 1.0, front_u))

h = r - sw / 2.0                                 # thickness of one half, in the bending direction
deflection = barb_r - bore_r
strain = 1.5 * h * deflection / (L * L) * 100.0
warnings = []
if strain > 1.5:
    warnings.append("bending strain %.2f%% is high for printed PLA; lengthen the pin or "
                    "reduce barb" % strain)


# ---------------------------------------------------------------- tools, world space
def at(u):
    return point_mm(O[0] + D[0] * u, O[1] + D[1] * u, O[2] + D[2] * u)


def frustum(u0, r0, u1, r1):
    return tbm.createCylinderOrCone(at(u0), cm(r0), at(u1), cm(r1))


def union(*bodies):
    acc = bodies[0]
    for b in bodies[1:]:
        tbm.booleanOperation(acc, b, UNION)
    return acc


tip_off = frustum(barb_u, r + 0.6, L + 0.3, r + 0.6)
tip_new = union(frustum(barb_u, barb_r, land_u, barb_r),
                frustum(land_u, barb_r, L, tip_r))
slot_len = L + 0.5
slot = tbm.createBox(adsk.core.OrientedBoundingBox3D.create(
    at(slot_len / 2.0), adsk.core.Vector3D.create(*n), adsk.core.Vector3D.create(*D),
    cm(sw), cm(slot_len), cm(2 * r + 2.0)))
bore = union(frustum(mouth_u0, mouth_r0, mouth_u1, bore_r),
             frustum(mouth_u0, bore_r, bore_end, bore_r),
             frustum(groove_u0, groove_r, groove_u1, groove_r),
             frustum(groove_u1, groove_r, groove_u2, bore_r),
             frustum(bore_end, bore_r, point_end, 0.0))
fill = None
if args.get("old_bore"):
    ob_r = float(args["old_bore"]["dia"]) / 2.0 + 0.05
    fill = frustum(off, ob_r, off + float(args["old_bore"]["depth"]) + 0.05, ob_r)


# ---------------------------------------------------------------- the plan
out["dims_mm"] = {"pin_dia": 2 * r, "barb_dia": 2 * barb_r, "tip_dia": 2 * tip_r,
                  "lead_in": lead, "bore_dia": 2 * bore_r, "groove_dia": 2 * groove_r,
                  "slot_width": sw, "slot_length": slot_len,
                  "catch_face_u": barb_u, "groove_wall_u": groove_u0,
                  "roof_end_u": round(groove_u2, 3), "bore_depth_u": point_end}
out["strain_percent"] = round(strain, 2)
out["warnings"] = warnings

if args.get("dry_run"):
    out["dry_run"] = True
    print("dry run %s -> %s: barb %.1f, groove %.2f, strain %.2f%%; nothing changed"
          % (args["mount"], args["part"], 2 * barb_r, 2 * groove_r, strain))
else:
    # ------------------------------------------------------------ edit
    require_timeline_at_end()
    d = design()
    parametric = d.designType == adsk.fusion.DesignTypes.ParametricDesignType
    start = d.timeline.count if parametric else None
    steps = []
    out["steps"] = steps

    def apply(occ, wanted, tool, operation, label):
        target = one_solid_body(occ, wanted)
        add_tool_and_combine(occ.component, target, to_component_space(tool, occ), operation, label)
        steps.append(label)
        flush()

    apply(mount_occ, args.get("mount_body"), tip_off, "cut", "Snap pin: tip off")
    apply(mount_occ, args.get("mount_body"), tip_new, "join", "Snap pin: barb")
    apply(mount_occ, args.get("mount_body"), slot, "cut", "Snap pin: slot")
    if fill is not None:
        apply(part_occ, args.get("part_body"), fill, "join", "Snap part: fill old bore")
    apply(part_occ, args.get("part_body"), bore, "cut", "Snap part: bore + catch groove")
    group = group_timeline(start, args.get("group_name", "Snap-fit")) if parametric else None

    # ------------------------------------------------------------ verify
    mount_proxy = one_solid_body(mount_occ, args.get("mount_body"))
    part_proxy = one_solid_body(part_occ, args.get("part_body"))
    gap = mm(app.measureManager.measureMinimumDistance(part_proxy, mount_proxy).value)
    interference = 0.0
    if gap < 1e-4:
        t = tbm.copy(part_proxy)
        try:
            tbm.booleanOperation(t, tbm.copy(mount_proxy), adsk.fusion.BooleanTypes.IntersectionBooleanType)
            interference = t.volume * 1000.0
        except Exception:
            interference = None

    out["check"] = {"interference_mm3": None if interference is None else round(interference, 4),
                    "min_gap_mm": round(gap, 4),
                    "part_shells": sum(lump.shells.count for lump in part_proxy.lumps),
                    "solid": part_proxy.isSolid and mount_proxy.isSolid}
    out["ok"] = interference == 0.0 and gap > 0.0 and out["check"]["solid"]
    out["group"] = group.name if group else None
    print("snap-fit %s -> %s: %d steps, interference %s mm3, min gap %.3f mm, strain %.2f%%, ok=%s"
          % (args["mount"], args["part"], len(steps), out["check"]["interference_mm3"], gap, strain,
             out["ok"]))
for w in warnings:
    print("WARNING: " + w)
