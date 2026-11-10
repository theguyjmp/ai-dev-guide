"""Pass C3: deferred rows in drive, verdict and policies; anchors from every plan commit; the small plan checks
(ui -> verb, float_roundtrip, fault hook, [plan] policy_lists, [plan] traces) and trimmed policies in briefs."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import anchors  # noqa: E402
import drive  # noqa: E402
import plan_validate as pv  # noqa: E402
from test_drive import Toy  # noqa: E402
from test_plan_validate import ARCH, CALC, CALC_T, FAST, R1, R2, Base, r3, row, table, toml  # noqa: E402
from test_verdict import VerdictTest  # noqa: E402  (its helpers only: a subclass would rerun its tests)

DEFERRED3 = """# Functions

| id | phase | module | deps | entry | acceptance | status |
|----|-------|--------|------|-------|------------|--------|
| F1 | 1 | alpha | - | `toy add 1 2` | tests/acceptance/test_f1.py::test_add | planned |
| F2 | 2 | beta | F3 | `toy double 4` | tests/acceptance/test_f2.py::test_double | planned |
| F3 | 2 | beta | F1 | `toy triple 4` | | deferred |
"""

POLICY_ARCH = """# Architecture

## alpha
paths: `src/alpha/`

## beta
paths: `src/beta/`
Depends on: alpha

## Policies
Concurrency: alpha - one writer at a time
Concurrent writers: F1, F2
Boundaries: F1
Domain rules: sum -> alpha.add; draw -> gamma.draw
"""


class DriveDeferred(Toy):
    def driver(self, functions):
        drv = drive.Driver(self.make_repo(functions=functions), drive.load_config(self.root / "repo"))
        drv.load()
        return drv

    def test_a_deferred_row_is_never_started_and_counted_apart(self):
        drv = self.driver(DEFERRED3)
        self.assertEqual(sorted(drv.fns), ["F1", "F2"])
        self.assertEqual(drv.deferred, {"F3"})
        self.assertNotIn("F3", drv.st["functions"])
        self.assertIn("deferred 1", drv.summary())
        self.assertIn("F3", drv.plan_problem(drv.fns["F2"]))
        self.assertIn("deferred", drv.plan_problem(drv.fns["F2"]))
        self.assertEqual(drv.plan_problem(drv.fns["F1"]), "")

    def test_polish_does_not_wait_for_the_deferred_rows(self):  # C4 ruling: deferred rows are out of the plan
        drv = self.driver(DEFERRED3)
        for fid in drv.fns:
            drv.st["functions"][fid]["status"] = "proven"
        drv.polish_rounds, drv.probe_on, drv.phase_polish = 1, False, False
        calls = []
        drv.start_gap_pass = lambda: False
        drv.start_polish_audit = lambda rnd: calls.append(rnd) or True
        self.assertTrue(drv.polish_step())
        self.assertEqual(calls, [1])

    def test_only_deferred_rows_is_a_plan_error(self):
        only = DEFERRED3.replace("| planned |", "| deferred |")
        with self.assertRaises(drive.DriveError):
            self.driver(only)

    def test_trimmed_policies_keep_the_rows_id_and_its_modules_rules(self):
        drv = self.driver(DEFERRED3)
        drv.arch = POLICY_ARCH
        f2 = drv.fns["F2"]
        self.assertIn("Concurrent writers: F1, F2", drv.policies(f2))  # off by default
        drv.cfg["harness"]["trim_policies"] = True
        text = drv.policies(f2)
        self.assertIn("Concurrent writers: F2", text)
        self.assertNotIn("F1, F2", text)
        self.assertNotIn("Boundaries", text)
        self.assertIn("sum -> alpha.add", text)
        self.assertNotIn("gamma.draw", text)
        self.assertIn("Concurrency: alpha - one writer at a time", text)
        self.assertIn("Trimmed", text)
        self.assertIn("Boundaries: F1", drv.policies(drv.fns["F1"]))
        self.assertEqual(drv.policies(), drive.module_section(POLICY_ARCH, "Policies"))  # polish: the whole section


class VerdictDeferred(unittest.TestCase):
    repo, verdict = VerdictTest.repo, VerdictTest.verdict

    def test_deferred_rows_are_neither_proven_nor_missing(self):
        state = {"functions": {"F2": {"status": "planned"}}}
        d = self.repo(["proven", "deferred", "proven"], state=state)
        _, line, doc = self.verdict(d)
        self.assertIn("proven 2/2 deferred 1", line)
        self.assertEqual((doc["total"], doc["deferred"]), (2, 1))
        self.assertNotIn("deferred", self.verdict(self.repo(["proven", "planned"]))[1])


class AnchorsEveryPlanCommit(unittest.TestCase):
    def test_tests_named_on_base_or_touched_by_a_plan_commit_are_anchors(self):
        root = Path(tempfile.mkdtemp(prefix="anc3_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)

        def g(*a):
            return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, check=True).stdout

        def put(files, msg):
            for rel, text in files.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text, encoding="utf-8")
            g("add", "-A")
            g("commit", "-qm", msg)

        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@e.x")
        g("config", "user.name", "t")
        fn = "| F1 | tests/acceptance/test_a.py::test_a |\n"
        put({"spec/FUNCTIONS.md": fn, "tests/acceptance/test_a.py": "a = 1\n"}, "first plan")
        put({"spec/FUNCTIONS.md": fn + "| F2 | tests/acceptance/test_b.py::test_b |\n",
             "tests/acceptance/test_b.py": "b = 1\n"}, "plan: phase 2")
        put({"tests/acceptance/test_d.py": "d = 1\n"}, "plan: shared fixtures")
        put({"spec/FUNCTIONS.md": fn + "| F2 | tests/acceptance/test_b.py::test_b |\n"
             "| F3 | tests/acceptance/test_c.py::test_c |\n",
             "tests/acceptance/test_c.py": "c = 1\n", "tests/unit/test_free.py": "f = 1\n"}, "gate: merge F3 plan")
        base = g("rev-parse", "HEAD").strip()
        self.assertEqual(sorted(anchors.planned_tests(str(root), base)),
                         ["tests/acceptance/test_a.py", "tests/acceptance/test_b.py", "tests/acceptance/test_c.py",
                          "tests/acceptance/test_d.py"])
        g("checkout", "-qb", "fn/x")
        put({"tests/acceptance/test_b.py": "b = 2\n", "tests/acceptance/test_c.py": "c = 2\n",
             "tests/acceptance/test_d.py": "d = 2\n", "tests/unit/test_free.py": "f = 2\n"}, "weaken")
        self.assertEqual(anchors.touched_anchors(str(root), base, g("rev-parse", "HEAD").strip()),
                         ["tests/acceptance/test_b.py", "tests/acceptance/test_c.py", "tests/acceptance/test_d.py"])


LATER = row("F004", phase="2", module="report", deps="F001", entry="`tiny later`", acc="", status="deferred",
            files="src/nowhere.py")


class PolicyChecks(Base):
    def test_deferred_ids_on_policy_lists_are_skipped(self):
        arch = (ARCH.replace("Concurrent writers: -", "Concurrent writers: F004")
                .replace("Multi-store writes: -", "Multi-store writes: F004").replace("Boundaries: -", "Boundaries: F004"))
        self.assertEqual(self.problems(self.make(table(R1, R2, r3(), LATER), arch=arch)), [])
        p = self.problems(self.make(table(R1, R2, r3()), arch=arch))
        self.assertTrue(self.has(p, "unknown function F004"), p)

    def test_each_crash_test_names_the_fault_hook(self):
        arch = ARCH.replace("Multi-store writes: -", "Multi-store writes: F001\nFault hook: TINY_FAULT")
        acc = f"{CALC_T}::test_add; {CALC_T}::test_add_crash_between_saves"
        root = self.make(functions=table(row(acc=acc), R2, r3()), arch=arch)
        crash = "\n\ndef test_add_crash_between_saves():\n    assert tiny('add', '1', '1') == '2'\n"
        (root / CALC_T).write_text(CALC + crash, encoding="utf-8")
        self.only(self.problems(root), "crash test", "does not name the fault hook TINY_FAULT")
        (root / CALC_T).write_text(CALC + crash.replace("    assert", "    env = {'TINY_FAULT': 'after_chunk'}\n    assert"),
                                   encoding="utf-8")
        self.assertEqual(self.problems(root), [])

    def plan_toml(self, plan):
        return toml(cycles=FAST, entry_lint=FAST) + "[plan]\n" + plan + "\n"

    def test_generic_policy_lists(self):
        t = self.plan_toml('policy_lists = { cancellable = "cancel" }')
        p = self.problems(self.make(toml_text=t))
        self.only(p, "no `Cancellable:` line")
        self.assertEqual(self.problems(self.make(toml_text=t, arch=ARCH + "Cancellable: -\n")), [])
        p = self.problems(self.make(toml_text=t, arch=ARCH + "Cancellable: F001, F009\n"))
        self.assertTrue(self.has(p, "F001 is listed in Cancellable", "`cancel`"), p)
        self.assertTrue(self.has(p, "unknown function F009"), p)
        self.assertEqual(len(p), 2, p)
        acc = f"{CALC_T}::test_add; {CALC_T}::test_add_cancel"
        root = self.make(functions=table(row(acc=acc), R2, r3(), LATER), toml_text=t,
                         arch=ARCH + "Cancellable: F001, F004\n")
        (root / CALC_T).write_text(CALC + "\n\ndef test_add_cancel():\n    assert tiny('add', '1', '1') == '2'\n",
                                   encoding="utf-8")
        self.assertEqual(self.problems(root), [])
        p = self.problems(self.make(toml_text=self.plan_toml('policy_lists = { cancellable = "(" }')))
        self.assertTrue(self.has(p, "policy_lists 'cancellable': bad regex"), p)

    def test_traces(self):
        perf = ("# Performance\n\n| technique | owner | test |\n|---|---|---|\n"
                f"| memo | calc | {CALC_T}::test_add |\n| cull | nope | {CALC_T}::test_gone |\n| none | calc | - |\n")
        root = self.make(toml_text=self.plan_toml('traces = ["spec/PERFORMANCE.md"]'))
        p = self.problems(root)
        self.only(p, "spec/PERFORMANCE.md: missing")
        (root / "spec" / "PERFORMANCE.md").write_text(perf, encoding="utf-8")
        p = self.problems(root)
        self.assertTrue(self.has(p, "PERFORMANCE.md:6: module 'nope' is no module"), p)
        self.assertTrue(self.has(p, "PERFORMANCE.md:6: test tests/acceptance/test_calc.py::test_gone is in no"), p)
        self.assertTrue(self.has(p, "PERFORMANCE.md:7: names no test"), p)
        self.assertEqual(len(p), 3, p)
        (root / "spec" / "PERFORMANCE.md").write_text("# Performance\n\n| a | b |\n|---|---|\n| x | y |\n",
                                                      encoding="utf-8")
        self.only(self.problems(root), "no table with a module (or owner) and a test column")

    def test_unknown_plan_check(self):
        p = self.problems(self.make(toml_text=self.plan_toml('checks = ["ui_verb", "nope"]')))
        self.only(p, "[plan] checks 'nope' is unknown")


class SmallChecks(unittest.TestCase):
    def test_ui_row_depends_on_a_verb_row(self):
        rows = [{"rid": "F1", "line": 3, "entry": "`cli: vc part add`", "deps": "-"},
                {"rid": "F2", "line": 4, "entry": "`ui: /parts, add`", "deps": "F1"},
                {"rid": "F3", "line": 5, "entry": "`ui: /parts, drop`", "deps": "F2"},
                {"rid": "F4", "line": 6, "entry": "`http: POST /parts`", "deps": "-"},
                {"rid": "F5", "line": 7, "entry": "ui: /parts, list", "deps": "F4"}]
        problems = []
        pv.check_ui_verb(rows, problems, "spec/FUNCTIONS.md")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("spec/FUNCTIONS.md:5 F3: ui row depends on no cli: or http: row", problems[0])

    def test_float_roundtrip(self):
        root = Path(tempfile.mkdtemp(prefix="fr_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)

        def put(rel, text):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")

        put("Cargo.toml", '[workspace]\nmembers = ["crates/a", "crates/b"]\n[workspace.dependencies]\nserde_json = "1"\n')
        put("crates/a/Cargo.toml", '[package]\nname = "a"\n[dependencies]\nserde_json = { workspace = true }\n')
        put("crates/b/Cargo.toml", '[package]\nname = "b"\n[dev-dependencies]\nserde_json = { version = "1" }\n')
        put("target/x/Cargo.toml", '[package]\nname = "x"\n[dependencies]\nserde_json = "1"\n')
        problems = []
        pv.check_float_roundtrip(root, problems)
        self.assertEqual(sorted(p.split(":")[0] for p in problems), ["Cargo.toml", "crates/b/Cargo.toml"], problems)
        put("Cargo.toml", '[workspace]\n[workspace.dependencies]\nserde_json = { version = "1", features = ["float_roundtrip"] }\n')
        put("crates/b/Cargo.toml", '[package]\nname = "b"\n[target.\'cfg(unix)\'.dependencies]\nserde_json = "1"\n')
        problems = []
        pv.check_float_roundtrip(root, problems)
        self.assertEqual([p.split(":")[0] for p in problems], ["crates/b/Cargo.toml"], problems)


if __name__ == "__main__":
    unittest.main()
