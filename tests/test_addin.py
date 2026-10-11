"""Offline tests for the add-in's file protocol and receipts. Fusion's adsk modules are
stubbed, so only the parts that never touch the Fusion API are tested here:

    python -m unittest discover -s tests
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_addin(agent_dir):
    adsk = types.ModuleType("adsk")
    core = types.ModuleType("adsk.core")
    fusion = types.ModuleType("adsk.fusion")
    core.CustomEventHandler = type("CustomEventHandler", (), {"__init__": lambda self: None})
    fusion.DesignTypes = types.SimpleNamespace(ParametricDesignType=1, DirectDesignType=0)
    adsk.core, adsk.fusion = core, fusion
    sys.modules.update({"adsk": adsk, "adsk.core": core, "adsk.fusion": fusion})
    os.environ["FUSION_AGENT_DIR"] = agent_dir
    try:
        spec = importlib.util.spec_from_file_location("fusion_agent_under_test",
                                                      os.path.join(ROOT, "FusionAgent", "FusionAgent.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        del os.environ["FUSION_AGENT_DIR"]
    return module


class AddinFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fa_test_")
        self.fa = load_addin(self.dir)
        os.makedirs(self.fa.QUEUE)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def queue(self, name, **job):
        path = os.path.join(self.fa.QUEUE, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(job, f)
        return path

    def test_empty_queue(self):
        self.assertIsNone(self.fa._next_job(time.time()))

    def test_oldest_queued_job_first(self):
        self.queue("00000000000000000002-b.json", id="b")
        first = self.queue("00000000000000000001-a.json", id="a")
        self.assertEqual(self.fa._next_job(time.time()), first)

    def test_legacy_slot_goes_before_the_queue(self):
        self.queue("00000000000000000001-a.json", id="a")
        with open(self.fa.JOB, "w") as f:
            f.write("{}")
        self.assertEqual(self.fa._next_job(time.time()), self.fa.JOB)

    def test_half_written_files_are_ignored(self):
        with open(os.path.join(self.fa.QUEUE, "00000000000000000001-a.json.tmp"), "w") as f:
            f.write("{")
        self.assertIsNone(self.fa._next_job(time.time()))

    def test_expired_job_is_dropped_unrun_with_a_result(self):
        old = self.queue("00000000000000000001-old.json", id="old", expires=time.time() - 1)
        fresh = self.queue("00000000000000000002-new.json", id="new", expires=time.time() + 60)
        self.assertEqual(self.fa._next_job(time.time()), fresh)
        self.assertFalse(os.path.exists(old))
        with open(os.path.join(self.fa.RESULTS, "old.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["error"]["type"], "JobExpired")

    def test_cancelled_queued_job_is_dropped_unrun(self):
        gone = self.queue("00000000000000000001-c.json", id="c", expires=time.time() + 60)
        os.makedirs(self.fa.CANCEL, exist_ok=True)
        open(os.path.join(self.fa.CANCEL, "c"), "w").close()
        self.assertIsNone(self.fa._next_job(time.time()))
        self.assertFalse(os.path.exists(gone))
        self.assertFalse(os.path.exists(os.path.join(self.fa.CANCEL, "c")))     # cleaned up
        with open(os.path.join(self.fa.RESULTS, "c.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["error"]["type"], "JobCancelled")

    def test_journal_appends_and_rotates(self):
        self.fa.JOURNAL_MAX = 200
        for i in range(5):
            self.fa._journal({"doc": "Car", "caller": {"cwd": "x"}},
                             {"id": str(i), "status": "done", "ran_on": {"name": "Car v3"}}, time.time())
        self.assertTrue(os.path.exists(os.path.join(self.dir, "journal.1.jsonl")))
        with open(self.fa.JOURNAL, encoding="utf-8") as f:
            line = json.loads(f.readlines()[-1])
        self.assertEqual((line["id"], line["ran_on"], line["doc"]), ("4", "Car v3", "Car"))


class Receipts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="fa_test_")
        cls.fa = load_addin(cls.dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def snap(self, open_docs, active, docs):
        """open_docs: {key: name}; active: key; docs: {key: fingerprint with a name}."""
        return {"open": open_docs, "active": [active, open_docs.get(active)] if active else None, "docs": docs}

    def test_nothing_changed(self):
        s = self.snap({"k1": "Car v3"}, "k1", {"k1": {"name": "Car v3", "isModified": False, "bodies": 4}})
        self.assertEqual(self.fa._diff(s, json.loads(json.dumps(s))), {})

    def test_counts_opened_closed_active(self):
        before = self.snap({"k1": "Car v3", "k2": "Old v1"}, "k1",
                           {"k1": {"name": "Car v3", "isModified": False, "bodies": 4, "timeline": [10, 10]}})
        after = self.snap({"k1": "Car v3", "k3": "New"}, "k3",
                          {"k1": {"name": "Car v3", "isModified": True, "bodies": 5, "timeline": [12, 12]},
                           "k3": {"name": "New"}})
        d = self.fa._diff(before, after)
        self.assertEqual(d["opened"], ["New"])
        self.assertEqual(d["closed"], ["Old v1"])
        self.assertEqual(d["active"], ["Car v3", "New"])
        self.assertEqual(d["documents"]["Car v3"],
                         {"bodies": [4, 5], "isModified": [False, True], "timeline": [[10, 10], [12, 12]]})

    def test_parameters_say_which(self):
        before = self.snap({"k": "A"}, "k", {"k": {"name": "A", "parameters": ["d=10 mm", "w=5 mm"]}})
        after = self.snap({"k": "A"}, "k", {"k": {"name": "A", "parameters": ["d=12 mm", "w=5 mm"]}})
        self.assertEqual(self.fa._diff(before, after)["documents"]["A"]["parameters"],
                         {"removed": ["d=10 mm"], "added": ["d=12 mm"]})

    # Both failed live when documents were keyed by name: two new documents are both "Untitled".
    def test_a_second_document_with_the_same_name_is_seen(self):
        before = self.snap({"k1": "Untitled"}, "k1", {"k1": {"name": "Untitled", "isModified": False}})
        after = self.snap({"k1": "Untitled", "k2": "Untitled"}, "k2",
                          {"k1": {"name": "Untitled", "isModified": False}, "k2": {"name": "Untitled"}})
        d = self.fa._diff(before, after)
        self.assertEqual(d["opened"], ["Untitled"])
        self.assertEqual(d["active"], ["Untitled", "Untitled (another document)"])
        self.assertNotIn("documents", d)

    def test_closing_one_of_two_same_named_documents_is_not_compared_with_the_other(self):
        before = self.snap({"k1": "Untitled", "k2": "Untitled"}, "k2",
                           {"k2": {"name": "Untitled", "isModified": True, "parameters": ["a=1 mm"]}})
        after = self.snap({"k1": "Untitled"}, "k1", {"k1": {"name": "Untitled", "isModified": False}})
        d = self.fa._diff(before, after)
        self.assertEqual(d["closed"], ["Untitled"])
        self.assertNotIn("documents", d)


class GeometryReceipt(unittest.TestCase):
    """Body entries are [volume mm3, area mm2, centre of mass mm, [faces, edges]]."""

    @classmethod
    def setUpClass(cls):
        cls.fa = load_addin(tempfile.mkdtemp(prefix="fa_test_"))

    def g(self, bodies=None, placements=None):
        return {"bodies": bodies or {}, "placements": placements or {}}

    def test_nothing_changed(self):
        g = self.g({"Plate/Body1": [1000.0, 700.0, [5, 5, 1], [6, 12]]}, {"Plate:1": [1] * 9 + [0, 0, 0]})
        self.assertEqual(self.fa._geometry_diff(g, json.loads(json.dumps(g))), {})

    def test_cut_added_body_moved_part(self):
        before = self.g({"Plate/Body1": [1000.0, 700.0, [5, 5, 1], [6, 12]], "Old/Body1": [5.0, 3.0, [1, 1, 1], [6, 12]]},
                        {"Ear:1": [1, 0, 0, 0, 1, 0, 0, 0, 1, 10.0, 0.0, 0.0]})
        after = self.g({"Plate/Body1": [990.0, 710.0, [5.1, 5, 1], [7, 15]], "New/Body1": [2.0, 1.0, [9, 9, 9], [6, 12]]},
                       {"Ear:1": [1, 0, 0, 0, 1, 0, 0, 0, 1, 15.0, 0.0, 0.0]})
        d = self.fa._geometry_diff(before, after)
        self.assertEqual(d["bodies_added"], ["New/Body1"])
        self.assertEqual(d["bodies_removed"], ["Old/Body1"])
        self.assertEqual(d["bodies_changed"], ["Plate/Body1: volume 1000.0 -> 990.0 mm3; area 700.0 -> 710.0 mm2; "
                                               "faces/edges [6, 12] -> [7, 15]; centre moved [0.1, 0, 0] mm"])
        self.assertEqual(d["parts_moved"], ["Ear:1: moved [5.0, 0.0, 0.0] mm"])

    def test_hole_slid_same_volume_and_topology(self):
        # A review found this missed by the first version: only the centre of mass moves.
        before = self.g({"Plate/Body1": [1000.0, 700.0, [50.0, 30.0, 1.35], [7, 15]]})
        after = self.g({"Plate/Body1": [1000.0, 700.0, [49.8, 30.0, 1.35], [7, 15]]})
        self.assertEqual(self.fa._geometry_diff(before, after)["bodies_changed"],
                         ["Plate/Body1: moved [-0.2, 0.0, 0.0] mm in its component"])

    def test_rename_is_not_a_loss(self):
        body = [1000.0, 700.0, [5, 5, 1], [6, 12]]
        d = self.fa._geometry_diff(self.g({"Old/Body1": body}), self.g({"New/Body1": list(body)}))
        self.assertEqual(d, {"bodies_renamed": ["Old/Body1 -> New/Body1"]})

    def test_parts_added_and_removed(self):
        before = self.g(placements={"Bolt:1": [1] * 9 + [0, 0, 0], "Bolt:3": [1] * 9 + [50, 0, 0]})
        after = self.g(placements={"Bolt:1": [1] * 9 + [0, 0, 0], "Bolt:4": [1] * 9 + [80, 0, 0]})
        d = self.fa._geometry_diff(before, after)
        self.assertEqual(d["parts_added"], ["Bolt:4 at [80, 0, 0] mm"])
        self.assertEqual(d["parts_removed"], ["Bolt:3 (was at [50, 0, 0] mm)"])

    def test_not_measured_is_said_not_turned_into_removals(self):
        g = self.g({"P/B": [1.0, 1.0, [0, 0, 0], [6, 12]]})
        self.assertEqual(list(self.fa._geometry_diff(g, None)), ["not_measured"])

    def test_placement_noise_is_not_a_move_but_a_turn_is(self):
        before = self.g(placements={"P:1": [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]})
        noise = self.g(placements={"P:1": [0.999999, 0, 0, 0, 1, 0, 0, 0, 1, 0.0004, 0, 0]})
        turn = self.g(placements={"P:1": [0, -1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0]})
        self.assertEqual(self.fa._geometry_diff(before, noise), {})
        self.assertEqual(self.fa._geometry_diff(before, turn)["parts_moved"], ["P:1: moved [0, 0, 0] mm, turned"])


class Counted(list):
    @property
    def count(self):
        return len(self)


class ReceiptLimit(unittest.TestCase):
    """The full receipt is the default from 1.5.0, so a design over the limit must stay cheap, and
    a document is measured after the job exactly when it was measured before."""

    def setUp(self):
        self.fa = load_addin(tempfile.mkdtemp(prefix="fa_test_"))
        self.fa.adsk.fusion.Design = types.SimpleNamespace(cast=lambda x: x)
        self.measured = []
        self.fa._geometry = lambda d: self.measured.append(d.tag) or {"bodies": {"C/B": [1.0, 1.0, [0, 0, 0], [6, 12]]},
                                                                      "placements": {}}
        self.small, self.big = self.doc("small", 5), self.doc("big", 50)
        docs = [self.small, self.big]
        self.fa._app = types.SimpleNamespace(activeDocument=self.small,
                                             documents=types.SimpleNamespace(count=2, item=lambda i: docs[i]))
        self.fa._target = self.big

    def doc(self, key, bodies):
        comp = types.SimpleNamespace(bRepBodies=Counted([object()] * bodies))
        design = types.SimpleNamespace(tag=key, allComponents=Counted([comp]), designType=0,
                                       rootComponent=types.SimpleNamespace(allOccurrences=Counted()))
        return types.SimpleNamespace(creationId=key, name=key.title(), isModified=False,
                                     products=types.SimpleNamespace(itemByProductType=lambda t: design))

    def test_a_design_over_the_limit_is_counted_not_measured(self):
        s = self.fa._snapshot(True, 10)
        self.assertIn("geometry", s["docs"]["small"])
        self.assertEqual(s["docs"]["big"]["geometry_skipped"], 50)
        self.assertNotIn("geometry", s["docs"]["big"])
        self.assertEqual(self.measured, ["small"])

    def test_no_limit_measures_both(self):
        self.fa._snapshot(True)
        self.assertEqual(sorted(self.measured), ["big", "small"])

    def test_after_the_job_only_what_was_measured_before(self):
        s = self.fa._snapshot(True, only={"small"})
        self.assertEqual(self.measured, ["small"])
        self.assertNotIn("geometry", s["docs"]["big"])
        self.assertNotIn("geometry_skipped", s["docs"]["big"])

    def test_skipped_before_is_not_a_change_after(self):
        before, after = self.fa._snapshot(True, 10), self.fa._snapshot(True, only={"small"})
        notes = {}
        self.assertEqual(self.fa._diff(before, after, notes), {})
        self.assertEqual(self.fa._geometry_summary(before, after, notes),
                         {"compared": ["Small"], "skipped": {"Big": 50}, "not_measured": []})

    def test_unmeasured_is_a_note_not_a_change(self):
        # Before 1.5.0 this was a change, so a --read-only job failed on a design it could not measure.
        g = {"bodies": {}, "placements": {}}
        snap = lambda geo: {"open": {"k": "A"}, "active": ["k", "A"],
                            "docs": {"k": {"name": "A", "isModified": False, "geometry": geo}}}
        notes = {}
        self.assertEqual(self.fa._diff(snap(None), snap(g), notes), {})
        self.assertEqual(notes, {"not_measured": ["A (before the job)"]})

    def tearDown(self):
        self.fa._target = None


class Json(unittest.TestCase):
    def test_tuple_keys_survive(self):
        fa = load_addin(tempfile.mkdtemp(prefix="fa_test_"))
        self.assertEqual(json.loads(fa._dumps({"gaps": {("a", "b"): 0.2}})), {"gaps": {"('a', 'b')": 0.2}})


if __name__ == "__main__":
    unittest.main()
