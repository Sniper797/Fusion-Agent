"""Offline tests for run_job.py's pre-flight check and helpers. No Fusion needed:

    python -m unittest discover -s tests
"""
import glob
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("run_job", os.path.join(ROOT, "run_job.py"))
run_job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_job)


def rules(code, pinned=False):
    refusals, warnings = run_job.preflight(code, pinned=pinned)
    return [r[0] for r in refusals], [w[0] for w in warnings]


class Refusals(unittest.TestCase):
    def test_iterating_cloud_folders_is_refused(self):
        self.assertEqual(rules("for f in app.data.activeProject.rootFolder.dataFolders:\n    pass\n")[0],
                         ["cloud-walk"])

    def test_comprehension_over_files_is_refused(self):
        self.assertEqual(rules("names = [f.name for f in folder.dataFiles]\n")[0], ["cloud-walk"])

    def test_recursive_walk_is_refused(self):
        code = ("def walk(folder):\n"
                "    sub = folder.dataFolders.item(0)\n"
                "    walk(sub)\n")
        self.assertEqual(rules(code)[0], ["cloud-walk"])

    def test_hubs_in_a_while_loop_are_refused(self):
        code = "i = 0\nwhile i < 3:\n    h = app.data.dataHubs.item(i)\n    i += 1\n"
        self.assertEqual(rules(code)[0], ["cloud-walk"])

    def test_level_by_level_itemByName_is_allowed(self):
        code = ("folder = app.data.activeProject.rootFolder\n"
                "for name in ['CAD', 'SDP']:\n"
                "    folder = folder.dataFolders.itemByName(name)\n")
        self.assertEqual(rules(code), ([], []))

    def test_single_lookup_outside_a_loop_is_allowed(self):
        self.assertEqual(rules("first = root_folder.dataFolders.item(0)\n"), ([], []))

    # The forms a review found slipping past the first version of the check.
    def test_alias_used_in_a_loop_is_refused(self):
        code = "files = root.dataFiles\nfor i in range(files.count):\n    f = files.item(i)\n"
        self.assertEqual(set(rules(code)[0]), {"cloud-walk"})

    def test_as_array_alias_is_refused(self):
        self.assertEqual(rules("fs = root.dataFolders.asArray()\nfor f in fs:\n    pass\n")[0], ["cloud-walk"])

    def test_helper_called_from_a_loop_is_refused(self):
        code = ("def subs(f):\n    return f.dataFolders.asArray()\n"
                "stack = [root]\nwhile stack:\n    stack.extend(subs(stack.pop()))\n")
        self.assertIn("cloud-walk", rules(code)[0])

    def test_mutual_recursion_is_refused(self):
        code = ("def a(f):\n    b(f.dataFolders.item(0))\n"
                "def b(f):\n    a(f)\n")
        self.assertIn("cloud-walk", rules(code)[0])

    def test_method_recursion_is_refused(self):
        code = ("class W:\n    def walk(self, f):\n        self.walk(f.dataFolders.item(0))\n")
        self.assertIn("cloud-walk", rules(code)[0])

    def test_iterating_builtins_are_refused(self):
        self.assertEqual(rules("names = sorted(folder.dataFiles)\n")[0], ["cloud-walk"])
        self.assertEqual(rules("xs = list(map(str, folder.dataFiles))\n")[0], ["cloud-walk"])

    def test_select_entity_is_refused(self):
        self.assertEqual(rules("s = ui.selectEntity('pick', 'Bodies')\n")[0], ["modal-ui"])

    def test_compute_all_is_refused(self):
        self.assertEqual(rules("design().computeAll()\n")[0], ["compute-all"])

    def test_modal_dialogs_are_refused(self):
        self.assertEqual(rules("ui.messageBox('hi')\n")[0], ["modal-ui"])
        self.assertEqual(rules("dlg = ui.createFileDialog()\n")[0], ["modal-ui"])


class Warnings(unittest.TestCase):
    def test_all_joints(self):
        self.assertEqual(rules("n = root.allJoints.count\n")[1], ["all-joints"])

    def test_visible_open_warns_and_hidden_open_does_not(self):
        self.assertEqual(rules("d = app.documents.open(f)\n")[1], ["visible-open"])
        self.assertEqual(rules("d = app.documents.open(f, True)\n")[1], ["visible-open"])
        self.assertEqual(rules("d = app.documents.open(f, False)\n")[1], [])
        self.assertEqual(rules("d = app.documents.open(f, visible=False)\n")[1], [])

    def test_document_close_warns_but_file_close_does_not(self):
        self.assertEqual(rules("d.close(False)\n")[1], ["close"])
        self.assertEqual(rules("d.close(True)\n")[1], ["close"])
        self.assertEqual(rules("fh.close()\n")[1], [])

    def test_save(self):
        self.assertEqual(rules("app.activeDocument.save('x')\n")[1], ["save"])

    def test_active_names_warn_only_when_pinned(self):
        code = "p = app.activeProduct\n"
        self.assertEqual(rules(code, pinned=True)[1], ["active-with-doc"])
        self.assertEqual(rules(code, pinned=False)[1], [])

    def test_active_component_of_the_pinned_design_is_fine(self):
        self.assertEqual(rules("comp = design().activeComponent\n", pinned=True)[1], [])

    def test_new_document_under_a_pin_warns(self):
        self.assertIn("active-with-doc", rules("d = app.documents.add(t)\n", pinned=True)[1])

    def test_eval_with_a_trailing_comment_still_parses(self):
        expr = "design().timeline.count  # how many"
        run_job.preflight("out['value'] = (\n%s\n)\n" % expr)          # the form main() builds for -e

    def test_line_numbers(self):
        refusals, _ = run_job.preflight("x = 1\n\nui.messageBox('a')\n")
        self.assertEqual(refusals[0][1], 3)


class OwnExamples(unittest.TestCase):
    def test_no_example_is_refused(self):
        for path in glob.glob(os.path.join(ROOT, "examples", "*.py")):
            with open(path, encoding="utf-8-sig") as f:
                refusals, _ = run_job.preflight(f.read(), path)
            self.assertEqual(refusals, [], path)

    def test_demo_scene_builds_in_its_own_document(self):
        # Under --doc, design() is the pinned (user's) document; the demo must never use it.
        import ast
        with open(os.path.join(ROOT, "examples", "demo_scene.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "design"]
        self.assertEqual(calls, [])

    def test_examples_that_read_a_design_follow_the_pin(self):
        # screenshot.py captures the active window and demo_scene.py works in the new, active
        # document it makes, so only those two may use the active-document names.
        for path in glob.glob(os.path.join(ROOT, "examples", "*.py")):
            if os.path.basename(path) in ("screenshot.py", "demo_scene.py", "selection.py"):
                continue
            with open(path, encoding="utf-8-sig") as f:
                _, warnings = run_job.preflight(f.read(), path, pinned=True)
            self.assertNotIn("active-with-doc", [w[0] for w in warnings], path)

    def test_inspect_design_no_longer_uses_all_joints(self):
        with open(os.path.join(ROOT, "examples", "inspect_design.py"), encoding="utf-8") as f:
            _, warnings = run_job.preflight(f.read())
        self.assertNotIn("all-joints", [w[0] for w in warnings])


class Helpers(unittest.TestCase):
    def test_bash_paths(self):
        notes = []
        fixed = run_job.fix_bash_paths({"out": "/c/Users/me/a.stl", "keep": "C:/x", "list": ["/d/y"], "n": 3},
                                       notes)
        self.assertEqual(fixed, {"out": "C:/Users/me/a.stl", "keep": "C:/x", "list": ["D:/y"], "n": 3})
        self.assertEqual(len(notes), 2)

    def test_hints(self):
        self.assertTrue(run_job.hints_for("RuntimeError: 3 : invalid argument value"))
        self.assertTrue(run_job.hints_for("NameError: name 'doc' is not defined"))
        self.assertEqual(run_job.hints_for("ZeroDivisionError: division by zero"), [])

    def test_version_tuple(self):
        self.assertLess(run_job.version_tuple("1.0.0"), run_job.NEEDS_FOR_DOC)
        self.assertEqual(run_job.version_tuple("1.1.0"), (1, 1, 0))
        self.assertEqual(run_job.version_tuple("junk"), (0, 0, 0))

    def test_describe(self):
        self.assertEqual(run_job.describe({"a": [1, 2], "b": 3}), {"a": "list of 2", "b": "int"})


if __name__ == "__main__":
    unittest.main()
