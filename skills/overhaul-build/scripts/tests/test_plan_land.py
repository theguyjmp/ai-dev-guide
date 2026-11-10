"""plan_land.py: a plan commit reruns the proven acceptance tests its files can break before main moves to it."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plan_land  # noqa: E402

ARCH = """# Architecture

## Module alpha
Paths: src/alpha
Depends on: none

## Module beta
Paths: src/beta
Depends on: alpha

## Module gamma
Paths: src/gamma
Depends on: none
"""

FUNCTIONS = """# Functions

| id | phase | module | deps | entry | acceptance | status |
|----|-------|--------|------|-------|------------|--------|
| P1 | 1 | alpha | - | `a` | tests/acceptance/test_p1.py::test_p1 | proven |
| P2 | 1 | beta | P1 | `b` | tests/acceptance/test_p2.py::test_p2 | proven |
| P3 | 1 | gamma | - | `c` | tests/acceptance/test_p3.py::test_p3 | proven |
| P4 | 2 | gamma | - | `d` | tests/acceptance/test_p4.py::test_p4 | planned |
"""

TEST = """import json
from pathlib import Path


def test_{id}():
    assert json.loads(Path("src/{mod}/schema.json").read_text())
"""


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


class PlanRows(unittest.TestCase):
    rows = {"P1": {"acceptance": ["tests/acceptance/test_p1.py::test_p1"], "status": "proven", "module": "alpha"},
            "P2": {"acceptance": ["tests/acceptance/test_p2.py::test_p2"], "status": "proven", "module": "beta"},
            "P3": {"acceptance": ["tests/acceptance/test_p3.py::test_p3"], "status": "proven", "module": "gamma"},
            "P4": {"acceptance": ["tests/acceptance/test_p4.py::test_p4"], "status": "planned", "module": "gamma"}}

    def pick(self, files):
        return plan_land.plan_rows(ARCH, self.rows, files, "spec/FUNCTIONS.md")[0]

    def test_a_spec_only_commit_reruns_nothing(self):
        self.assertEqual(self.pick(["spec/FUNCTIONS.md", "spec/DECISIONS.md"]), set())

    def test_a_new_planned_test_reruns_nothing(self):
        self.assertEqual(self.pick(["tests/acceptance/test_p4.py"]), set())

    def test_a_proven_test_file_reruns_its_row(self):
        self.assertEqual(self.pick(["tests/acceptance/test_p3.py"]), {"P3"})

    def test_a_stub_in_a_module_reruns_its_module_and_its_dependents(self):
        self.assertEqual(self.pick(["src/alpha/new_stub.py"]), {"P1", "P2"})
        self.assertEqual(self.pick(["src/beta/new_stub.py"]), {"P2"})

    def test_a_file_outside_every_module_reruns_every_proven_row(self):
        self.assertEqual(self.pick(["Cargo.toml"]), {"P1", "P2", "P3"})


class Cli(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="plan-land-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        repo = self.repo = self.root / "repo"
        py = sys.executable.replace("\\", "/")
        put(repo / "overhaul.toml", f'[limits]\nproc_mem_gb = 0\n\n[commands]\nacceptance = '
                                    f'\'"{py}" -m pytest -q -p no:cacheprovider {{node}}\'\n')
        put(repo / "spec" / "ARCHITECTURE.md", ARCH)
        put(repo / "spec" / "FUNCTIONS.md", FUNCTIONS)
        put(repo / ".gitignore", ".overhaul/\n")
        for fid, mod in (("p1", "alpha"), ("p2", "beta"), ("p3", "gamma")):
            put(repo / "src" / mod / "schema.json", '{"type": "object"}\n')
            put(repo / "tests" / "acceptance" / f"test_{fid}.py", TEST.format(id=fid, mod=mod))
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "m0")

    def branch(self, name: str, files: dict[str, str]) -> None:
        git(self.repo, "checkout", "-q", "-b", name)
        for rel, text in files.items():
            put(self.repo / rel, text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", f"plan: {name}")
        git(self.repo, "checkout", "-q", "main")

    def run_cli(self, branch: str) -> tuple[int, str]:
        p = subprocess.run([sys.executable, str(Path(plan_land.__file__)), "--repo", str(self.repo), "--branch",
                            branch, "--tests-only", "--timeout", "120"], capture_output=True, text=True)
        return p.returncode, p.stdout.strip().splitlines()[-1]

    def test_a_stub_that_breaks_a_proven_test_is_red_and_main_does_not_move(self):
        head = git(self.repo, "rev-parse", "main")
        self.branch("plan/bad", {"src/alpha/schema.json": "{not json\n"})
        code, line = self.run_cli("plan/bad")
        self.assertEqual(code, 1, line)
        self.assertIn("PLAN LAND RED", line)
        self.assertIn("proven P1", line)
        self.assertEqual(git(self.repo, "rev-parse", "main"), head)
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), 1)  # its worktree is gone

    def test_a_clean_plan_commit_is_green_on_main_merged_in(self):
        self.branch("plan/good", {"src/beta/stub.py": "def f():\n    raise NotImplementedError\n"})
        put(self.repo / "spec" / "DECISIONS.md", "D1: x\n")  # main moves after the branch: merged in the check
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "m1")
        code, line = self.run_cli("plan/good")
        self.assertEqual(code, 0, line)
        self.assertIn("PLAN LAND OK", line)
        self.assertIn("1 proven rows green: P2", line)

    def test_green_in_the_branch_worktree_commits_the_regenerated_index_on_the_branch(self):
        self.branch("plan/idx", {"src/beta/helpers.py": "def parse_query(text: str) -> dict:\n    return {}\n"})
        wt = self.root / "wt"
        git(self.repo, "worktree", "add", "-q", str(wt), "plan/idx")
        before = git(self.repo, "rev-parse", "plan/idx")
        p = subprocess.run([sys.executable, str(Path(plan_land.__file__)), "--repo", str(self.repo), "--branch",
                            "plan/idx", "--wt", str(wt), "--tests-only", "--timeout", "120"],
                           capture_output=True, text=True)
        line = p.stdout.strip().splitlines()[-1]
        self.assertEqual(p.returncode, 0, line)
        self.assertIn("INDEX regenerated", line)
        self.assertEqual(git(self.repo, "rev-parse", "plan/idx~1"), before)  # one commit on top, on the branch
        files = git(self.repo, "show", "--name-only", "--format=", "plan/idx").split()
        self.assertIn("spec/INDEX.md", files)
        self.assertTrue(all(f.startswith("spec/index/") or f == "spec/INDEX.md" for f in files), files)
        self.assertIn("parse_query", git(self.repo, "show", "plan/idx:spec/index/beta.md"))
        self.assertEqual(git(wt, "status", "--porcelain", "--untracked-files=no"), "")

    def test_a_branch_that_does_not_merge_with_main_is_red(self):
        self.branch("plan/clash", {"src/gamma/schema.json": '{"a": 1}\n'})
        put(self.repo / "src" / "gamma" / "schema.json", '{"b": 2}\n')
        git(self.repo, "commit", "-q", "-am", "m1")
        code, line = self.run_cli("plan/clash")
        self.assertEqual(code, 1, line)
        self.assertIn("does not merge with main", line)


if __name__ == "__main__":
    unittest.main()
