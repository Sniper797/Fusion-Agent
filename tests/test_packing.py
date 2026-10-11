"""Offline tests for the laser sheet packers in examples/laser_dxf.py (plain Python, no Fusion):

    python -m unittest discover -s tests
"""
import ast
import os
import random
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WANTED = {"pack_rows", "pack_maxrects", "split_free", "contains"}


def load_packers():
    with open(os.path.join(ROOT, "examples", "laser_dxf.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    module = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in WANTED],
                        type_ignores=[])
    scope = {}
    exec(compile(module, "laser_dxf.py", "exec"), scope)
    return scope


P = load_packers()


def check(test, sizes, places, W, H, gap):
    """Every placed part inside the bed with `gap` all round, and no two closer than `gap`."""
    rects = {}
    for (w, h), place in zip(sizes, places):
        if place is None:
            continue
        pg, x, y, turn = place
        if turn:
            w, h = h, w
        test.assertGreaterEqual(x, gap - 1e-6)
        test.assertGreaterEqual(y, gap - 1e-6)
        test.assertLessEqual(x + w, W - gap + 1e-6)
        test.assertLessEqual(y + h, H - gap + 1e-6)
        for (ox, oy, ow, oh) in rects.get(pg, []):
            apart = x >= ox + ow + gap - 1e-6 or ox >= x + w + gap - 1e-6 or \
                    y >= oy + oh + gap - 1e-6 or oy >= y + h + gap - 1e-6
            test.assertTrue(apart, "parts closer than the gap on sheet %d" % pg)
        rects.setdefault(pg, []).append((x, y, w, h))
    return 1 + max((p[0] for p in places if p), default=-1)


class Packing(unittest.TestCase):
    def test_too_big_is_left_out(self):
        self.assertEqual(P["pack_maxrects"]([(2000, 10)], 1300, 900, 5), [None])
        self.assertEqual(P["pack_rows"]([(2000, 10)], 1300, 900, 5), [None])

    def test_turned_when_only_that_fits(self):
        place = P["pack_maxrects"]([(850, 1250)], 1300, 900, 5)[0]
        self.assertTrue(place[3])

    def test_no_overlap_and_never_more_sheets_than_rows(self):
        rng = random.Random(7)
        for trial in range(25):
            sizes = [(rng.uniform(20, 400), rng.uniform(20, 300)) for _ in range(rng.randint(5, 60))]
            W, H, gap = 1300, 900, 5
            m = P["pack_maxrects"](sizes, W, H, gap)
            r = P["pack_rows"](sizes, W, H, gap)
            self.assertTrue(all(p is not None for p in m))
            sm, sr = check(self, sizes, m, W, H, gap), check(self, sizes, r, W, H, gap)
            self.assertLessEqual(sm, sr, "trial %d: maxrects %d sheets, rows %d" % (trial, sm, sr))

    def test_needs_clearly_fewer_sheets_overall(self):
        # Measured: 200 random sets took 317 sheets with maxrects and 445 in rows (29 % fewer).
        rng = random.Random(1)
        total_m = total_r = 0
        for _ in range(40):
            sizes = [(rng.uniform(20, 400), rng.uniform(20, 300)) for _ in range(rng.randint(5, 60))]
            total_m += 1 + max(p[0] for p in P["pack_maxrects"](sizes, 1300, 900, 5))
            total_r += 1 + max(p[0] for p in P["pack_rows"](sizes, 1300, 900, 5))
        self.assertLessEqual(total_m, 0.85 * total_r, "maxrects %d sheets, rows %d" % (total_m, total_r))


if __name__ == "__main__":
    unittest.main()
