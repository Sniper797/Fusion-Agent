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


class Json(unittest.TestCase):
    def test_tuple_keys_survive(self):
        fa = load_addin(tempfile.mkdtemp(prefix="fa_test_"))
        self.assertEqual(json.loads(fa._dumps({"gaps": {("a", "b"): 0.2}})), {"gaps": {"('a', 'b')": 0.2}})


if __name__ == "__main__":
    unittest.main()
