"""Export every visible solid body as its own binary STL, where it sits in the assembly
(read-only for the design).

    python run_job.py examples/export_stl.py
    python run_job.py examples/export_stl.py --args "{\"folder\": \"C:/print\", \"refinement\": \"medium\"}"
    python run_job.py examples/export_stl.py --args "{\"only\": [\"Gear\"], \"frame\": \"component\"}"

Fusion writes a body that lives inside a component in that component's own coordinates,
not where the assembly puts it (measured: a gear came out lying flat that stands upright in
the assembly). So each file is checked against the body: the distance from its vertices to the
body in the assembly and in its component tells which one Fusion used, and the file is moved
into the assembly when needed. Files are always written in mm, whatever the design's units.

args:
    folder      default <agent folder>/exports/<document>
    refinement  low | medium | high (default high)
    only        component or body names to export (default: every visible solid)
    frame       world (default): as placed in the assembly | component: as Fusion writes it
"""
import os
import struct

d = design()
em = d.exportManager
folder = args.get("folder") or os.path.join(agent_dir, "exports", safe_name(doc.name))
os.makedirs(folder, exist_ok=True)
levels = {"low": adsk.fusion.MeshRefinementSettings.MeshRefinementLow,
          "medium": adsk.fusion.MeshRefinementSettings.MeshRefinementMedium,
          "high": adsk.fusion.MeshRefinementSettings.MeshRefinementHigh}
level = levels[args.get("refinement", "high")]
only = set(args.get("only") or [])
want_world = args.get("frame", "world") == "world"


def read_stl(path):
    with open(path, "rb") as f:
        data = f.read()
    n = struct.unpack_from("<I", data, 80)[0]
    if 84 + 50 * n != len(data):
        raise ValueError("not a binary STL")
    return data, n


def stl_box(data, n):
    lo, hi = [float("inf")] * 3, [float("-inf")] * 3
    for i in range(n):
        vals = struct.unpack_from("<12f", data, 84 + 50 * i)
        for k in range(3, 12, 3):
            for a in range(3):
                lo[a] = min(lo[a], vals[k + a])
                hi[a] = max(hi[a], vals[k + a])
    return lo, hi


def vertex_gap_mm(data, n, body, samples=24):
    """Median distance (mm) from mesh vertices to the body. Tessellation vertices lie on the
    surface, so this is near 0 in the frame the file was written in, whatever the boxes say:
    a part flipped in place has the same box in both frames, not the same surface."""
    step = max(1, n // samples)
    gaps = []
    for i in range(0, n, step):
        x, y, z = struct.unpack_from("<3f", data, 84 + 50 * i + 12)
        p = adsk.core.Point3D.create(x / 10, y / 10, z / 10)
        g = try_value(lambda: app.measureManager.measureMinimumDistance(p, body).value * 10)
        if g is not None:
            gaps.append(g)
    gaps.sort()
    return gaps[len(gaps) // 2] if gaps else None


def box_mm(bb):
    return ([bb.minPoint.x * 10, bb.minPoint.y * 10, bb.minPoint.z * 10],
            [bb.maxPoint.x * 10, bb.maxPoint.y * 10, bb.maxPoint.z * 10])


def box_gap(a, b):
    return max(abs(x - y) for x, y in zip(a[0] + a[1], b[0] + b[1]))


def move_stl(path, data, n, m):
    """Rewrite the triangles with the 4x4 matrix m (rows, translation in cm): vertices move, normals turn."""
    r = [[m[4 * i + j] for j in range(3)] for i in range(3)]
    t = [m[4 * i + 3] * 10 for i in range(3)]          # cm -> mm
    out_ = bytearray(data[:84])
    for i in range(n):
        off = 84 + 50 * i
        vals = struct.unpack_from("<12f", data, off)
        new = []
        for k in range(0, 12, 3):
            v = vals[k:k + 3]
            p = [sum(r[a][b] * v[b] for b in range(3)) for a in range(3)]
            new += p if k == 0 else [p[a] + t[a] for a in range(3)]
        out_ += struct.pack("<12f", *new) + data[off + 48:off + 50]
    with open(path, "wb") as f:
        f.write(out_)
    return bytes(out_)


root = d.rootComponent
bodies = [(root.name, None, b) for b in root.bRepBodies]
for o in root.allOccurrences:
    bodies += [(o.fullPathName, o, b) for b in o.bRepBodies]

files = []
out["folder"] = folder
out["frame"] = "world" if want_world else "component"
out["files"] = files
for owner, occ, b in bodies:
    if not (b.isSolid and b.isVisible):
        continue
    if only and not ({owner.split(":")[0], owner, b.name, occ.component.name if occ else root.name} & only):
        continue
    path = os.path.join(folder, safe_name("%s - %s" % (owner, b.name)) + ".stl")
    rec = {"file": os.path.basename(path)}
    try:
        opts = em.createSTLExportOptions(b, path)
        opts.meshRefinement = level
        opts.isBinaryFormat = True
        opts.unitType = adsk.fusion.DistanceUnits.MillimeterDistanceUnits   # else the design's unit (inches...)
        em.execute(opts)
        data, n = read_stl(path)
        mesh = stl_box(data, n)
        world = box_mm(b.boundingBox)
        native = box_mm((b.nativeObject or b).boundingBox)
        # On the body as the assembly places it? Then the file is already in place. Fusion will not
        # measure against a component's own body, so this one distance decides; the check after the
        # move proves it. A box comparison is only the fallback for bodies Fusion cannot measure.
        gw = vertex_gap_mm(data, n, b)
        if gw is not None:
            written = "world" if gw < 0.01 else "component"
        else:
            written = "world" if box_gap(mesh, world) <= box_gap(mesh, native) else "component"
        rec.update(triangles=n, written_in=written, vertex_gap_mm=None if gw is None else round(gw, 4))
        if want_world and written == "component" and occ is not None:
            data = move_stl(path, data, n, occ.transform2.asArray())
            mesh = stl_box(data, n)
            rec["moved_into_assembly"] = True
        target = world if want_world else native
        rec["box_error_mm"] = round(box_gap(mesh, target), 4)
        if want_world:                       # the file must now sit on the body itself
            after = try_value(lambda: vertex_gap_mm(data, n, b))
            rec["vertex_gap_after_mm"] = None if after is None else round(after, 4)
            if after is not None and after > 0.01:
                rec["warning"] = "the mesh is not on the body after the move: check this file"
        rec["bytes"] = os.path.getsize(path)
    except Exception as e:
        rec["error"] = str(e)
    files.append(rec)
    flush()

failed = [f for f in files if not f.get("bytes")]
worst = max([f["box_error_mm"] for f in files if "box_error_mm" in f] or [0])
moved = sum(1 for f in files if f.get("moved_into_assembly"))
print("%d STL files in %s%s" % (len(files) - len(failed), folder, "; %d failed" % len(failed) if failed else ""))
print("frame: %s; %d moved from their component into the assembly; mesh box within %.4f mm of the body's"
      % (out["frame"], moved, worst))
