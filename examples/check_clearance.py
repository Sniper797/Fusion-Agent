"""Interference and minimum gap between every pair of visible solid bodies (read-only).

    python run_job.py examples/check_clearance.py
    python run_job.py examples/check_clearance.py --args "{\"only\": [\"Gear\", \"Mount\"]}"

args:
    only       list of name fragments; a body is checked when its occurrence or body
               name contains one of them. Default: every visible solid body.
    max_pairs  refuse above this many pairs (default 1500); a large assembly would
               keep Fusion busy for minutes, so narrow it with `only` instead.
Output, worst first: interference_mm3 = shared volume (0 = none), min_gap_mm.
"""
tbm = adsk.fusion.TemporaryBRepManager.get()
root = design().rootComponent
only = args.get("only")

items = [(root.name + " / " + b.name, b) for b in root.bRepBodies]
for o in root.allOccurrences:
    items += [(o.fullPathName + " / " + b.name, b) for b in o.bRepBodies]
items = [(label, b) for label, b in items
         if b.isSolid and b.isVisible and (not only or any(s in label for s in only))]

n_pairs = len(items) * (len(items) - 1) // 2
if n_pairs > int(args.get("max_pairs", 1500)):
    raise RuntimeError("%d bodies make %d pairs; narrow it with only=[...] or raise max_pairs"
                       % (len(items), n_pairs))

pairs = []
out["bodies"] = [label for label, _ in items]
out["pairs"] = pairs
for i in range(len(items)):
    for j in range(i + 1, len(items)):
        (la, a), (lb, b) = items[i], items[j]
        gap = try_value(lambda: mm(app.measureManager.measureMinimumDistance(a, b).value))
        inter = 0.0 if gap is not None else None
        if gap is not None and gap < 1e-4:   # touching or overlapping: measure the overlap
            def overlap():
                t = tbm.copy(a)
                tbm.booleanOperation(t, tbm.copy(b), adsk.fusion.BooleanTypes.IntersectionBooleanType)
                return t.volume * 1000.0
            inter = try_value(overlap)       # None: the kernel could not intersect them
        pairs.append({"a": la, "b": lb,
                      "min_gap_mm": None if gap is None else round(gap, 4),
                      "interference_mm3": None if inter is None else round(inter, 3)})
        flush()

pairs.sort(key=lambda p: (-(p["interference_mm3"] or 0), p["min_gap_mm"] is not None,
                          p["min_gap_mm"] or 0))
clashes = [p for p in pairs if p["interference_mm3"]]
unknown = [p for p in pairs if p["interference_mm3"] is None]
print("%d bodies, %d pairs, %d clashing, %d not measurable"
      % (len(items), len(pairs), len(clashes), len(unknown)))
for p in clashes:
    print("  CLASH %.3f mm3: %s <-> %s" % (p["interference_mm3"], p["a"], p["b"]))
