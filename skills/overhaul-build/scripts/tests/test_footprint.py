"""Pass B (2026-11-09): builders prove the proven tests of their footprint before DONE, and the pre-review gate
runs them, so a regression costs a retry with the test named instead of a review and a land slot.

Run from the scripts dir:  py -3 -m pytest -q tests/test_footprint.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import drive  # noqa: E402
import land  # noqa: E402

# alpha <- beta (beta depends on alpha); gamma stands alone. F5 is a [tiers] bench of beta.
ARCH = """# Architecture

## Module alpha
Paths: src/alpha/
Depends on: -

## Module beta
Paths: src/beta/
Depends on: alpha

## Module gamma
Paths: src/gamma/
Depends on: -
"""

FUNCTIONS = """# Functions

| id | phase | module | deps | entry | acceptance | status |
|----|-------|--------|------|-------|------------|--------|
| F1 | 1 | alpha | - | `toy a` | tests/acceptance/test_f1.py::test_f1 | proven |
| F2 | 1 | beta | F1 | `toy b` | tests/acceptance/test_f2.py::test_f2 | proven |
| F3 | 1 | gamma | - | `toy c` | tests/acceptance/test_f3.py::test_f3 | proven |
| F4 | 2 | alpha | F1 | `toy d` | tests/acceptance/test_f4.py::test_f4 | planned |
| F5 | 1 | beta | F1 | `toy e` | tests/acceptance/scale_f5.py::test_f5 | proven |
"""

# Each proven test reads a value from its module's source: a branch that changes that value breaks it.
CHECK = """from pathlib import Path


def test_{name}():
    assert "{want}" in Path("{src}").read_text(encoding="utf-8")
"""


def put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL)
    if r.returncode:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


class TestFootprintRows(unittest.TestCase):
    def test_module_dependents_and_unlisted_rows_but_not_fid_or_unrelated(self):
        rows = {"A": {"status": "proven", "module": "alpha"}, "B": {"status": "proven", "module": "beta"},
                "C": {"status": "proven", "module": "gamma"}, "D": {"status": "planned", "module": "alpha"},
                "U": {"status": "proven", "module": "unlisted"}, "F": {"status": "proven", "module": "alpha"}}
        self.assertEqual(land.footprint_rows(ARCH, rows, {"alpha"}, "F"), {"A", "B", "U"})
        self.assertEqual(land.footprint_rows(ARCH, rows, {"beta"}), {"B", "U"})


class Repo(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="fp-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, onexc=drive.force_remove)
        repo = self.repo = self.root / "repo"
        put(repo / ".gitignore", ".overhaul/\n__pycache__/\n")
        put(repo / "spec" / "FUNCTIONS.md", FUNCTIONS)
        put(repo / "spec" / "ARCHITECTURE.md", ARCH)
        for name, src, want, file in (("f1", "src/alpha/a.py", "A = 1", "test_f1.py"),
                                      ("f2", "src/beta/b.py", "B = 2", "test_f2.py"),
                                      ("f3", "src/gamma/c.py", "C = 3", "test_f3.py"),
                                      ("f4", "src/alpha/d.py", "D = 4", "test_f4.py"),
                                      ("f5", "src/beta/b.py", "B = 2", "scale_f5.py")):
            put(repo / "tests" / "acceptance" / file, CHECK.format(name=name, want=want, src=src))
        for src, text in (("src/alpha/a.py", "A = 1\n"), ("src/beta/b.py", "B = 2\n"), ("src/gamma/c.py", "C = 3\n")):
            put(repo / src, text)
        py = Path(sys.executable).as_posix()
        put(repo / "overhaul.toml", f"""[commands]
acceptance = "{py} -m pytest -q -p no:cacheprovider {{node}}"
dup = "{py} dup.py"
literals = "{py} literals.py"

[tiers]
scale = ["tests/acceptance/scale_*.py"]

[harness]
worker_cmd = ["{py}", "-c", "pass"]
""")
        git(repo, "init", "-q", "-b", "main")
        for key, value in (("user.name", "toy"), ("user.email", "toy@example.com"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            git(repo, "config", key, value)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "toy plan")
        self.drv = drive.Driver(repo, drive.load_config(repo))
        self.drv.load()

    def branch(self, fid: str, edits: dict[str, str]) -> Path:
        wt = self.drv.prepare_worktree(fid)
        for path, text in edits.items():
            put(wt / path, text)
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", f"{fid}: build")
        return wt

    def events(self, name: str) -> list[dict]:
        lines = (self.repo / ".overhaul" / "log.jsonl").read_text(encoding="utf-8").splitlines()
        return [e for e in (json.loads(ln) for ln in lines if ln.strip()) if e["event"] == name]


class TestBrief(Repo):
    def test_brief_names_the_footprint_commands_and_the_dup_check(self):
        f4 = self.drv.fns["F4"]
        text = self.drv.footprint_text(f4)
        self.assertIn("tests of proven rows F1, F2", text)
        self.assertIn("tests/acceptance/test_f1.py::test_f1 tests/acceptance/test_f2.py::test_f2", text)  # batched
        self.assertNotIn("test_f3", text)  # gamma: not alpha, not depending on it
        self.assertNotIn("scale_f5", text)  # a [tiers] bench of beta: only when beta itself is touched
        self.assertNotIn("test_f4", text)  # its own acceptance is listed apart
        dup = self.drv.dup_text()
        self.assertIn('git grep -n "<words>"', dup)
        self.assertIn("dup.py", dup)
        self.assertIn("literals.py", dup)
        brief = drive.render("build-brief.md", {"footprint": text, "dup_check": dup})
        self.assertIn(text, brief)
        self.assertIn("C05", brief)

    def test_no_proven_row_in_the_footprint(self):
        rows = FUNCTIONS.replace("| proven |", "| planned |")
        put(self.repo / "spec" / "FUNCTIONS.md", rows)
        git(self.repo, "commit", "-qam", "nothing proven")
        shutil.rmtree(self.repo / ".overhaul", onexc=drive.force_remove)  # a fresh state: nothing proven there
        self.drv = drive.Driver(self.repo, drive.load_config(self.repo))
        self.drv.load()
        self.assertIn("None yet", self.drv.footprint_text(self.drv.fns["F4"]))


class TestPreReviewGate(Repo):
    def test_gate_fails_on_a_footprint_regression_with_the_test_named(self):
        wt = self.branch("F4", {"src/alpha/d.py": "D = 4\n", "src/alpha/a.py": "A = 9\n"})  # breaks F1
        problem = self.drv.footprint_problem(wt, "F4")
        self.assertTrue(problem.startswith("regression before review: proven F1 "
                                           "tests/acceptance/test_f1.py::test_f1"), problem)
        self.assertTrue((self.repo / ".overhaul" / "runs" / "F4-footprint.out").is_file())
        self.assertIn("2 proven tests of 2 rows", self.events("footprint")[0]["detail"])

    def test_gate_passes_a_branch_that_keeps_its_footprint_green(self):
        wt = self.branch("F4", {"src/alpha/d.py": "D = 4\n"})
        self.assertEqual(self.drv.footprint_problem(wt, "F4"), "")

    def test_a_beta_change_reruns_the_beta_bench_and_fails_it(self):
        wt = self.branch("F4", {"src/beta/b.py": "B = 0\n"})  # touches beta: F2 and the tier bench F5
        problem = self.drv.footprint_problem(wt, "F4")
        self.assertIn("regression before review: proven F2 ", problem)
        self.assertIn("3 proven tests of 3 rows", self.events("footprint")[0]["detail"])

    def test_a_test_red_on_clean_main_too_does_not_fail_the_attempt(self):
        put(self.repo / "src" / "alpha" / "a.py", "A = 0\n")  # main itself breaks F1
        git(self.repo, "commit", "-qam", "main red")
        wt = self.branch("F4", {"src/alpha/d.py": "D = 4\n"})
        self.assertEqual(self.drv.footprint_problem(wt, "F4"), "")
        self.assertTrue(self.events("footprint_main_red"))

    def test_off_switch(self):
        self.drv.cfg["harness"]["pre_review_footprint"] = False
        wt = self.branch("F4", {"src/alpha/a.py": "A = 9\n"})
        self.assertEqual(self.drv.footprint_problem(wt, "F4"), "")


if __name__ == "__main__":
    unittest.main()
