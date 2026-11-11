"""plan_land.py: a plan commit reruns the proven acceptance tests its files can break before main moves to it."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_a_second_run_waits_for_the_first_instead_of_running_in_parallel(self):
        self.branch("plan/one", {"src/beta/stub.py": "def f():\n    raise NotImplementedError\n"})
        import land
        import time
        self.assertTrue(land.acquire_lock(str(self.repo), 5, "plan_land.py branch plan/first", plan_land.LOCK_NAME))
        p = subprocess.Popen([sys.executable, str(Path(plan_land.__file__)), "--repo", str(self.repo), "--branch",
                              "plan/one", "--tests-only", "--timeout", "120"], stdout=subprocess.PIPE, text=True)
        try:
            time.sleep(2)
            self.assertIsNone(p.poll(), "ran while the lock was held")
        finally:
            land.release_lock(str(self.repo), plan_land.LOCK_NAME)
        out = p.communicate(timeout=120)[0]
        self.assertEqual(p.returncode, 0, out)
        self.assertIn(f"waiting for plan_land of plan/first (pid {os.getpid()})", out)
        self.assertIn("PLAN LAND OK", out)
        self.assertFalse(os.path.exists(land.lock_path(str(self.repo), plan_land.LOCK_NAME)))

    def test_a_dead_holders_lock_is_broken(self):
        self.branch("plan/two", {"src/beta/stub.py": "def f():\n    raise NotImplementedError\n"})
        import land
        lp = land.lock_path(str(self.repo), plan_land.LOCK_NAME)
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        Path(lp).write_text("plan_land.py branch plan/x pid 999999 since now\n")
        code, line = self.run_cli("plan/two")
        self.assertEqual(code, 0, line)

    def test_a_branch_that_does_not_merge_with_main_is_red(self):
        self.branch("plan/clash", {"src/gamma/schema.json": '{"a": 1}\n'})
        put(self.repo / "src" / "gamma" / "schema.json", '{"b": 2}\n')
        git(self.repo, "commit", "-q", "-am", "m1")
        code, line = self.run_cli("plan/clash")
        self.assertEqual(code, 1, line)
        self.assertIn("does not merge with main", line)


REFUSE = """#!/bin/sh
# fake main guard: refuses every move of main, like guard/reference-transaction refusing a code change
[ "$1" = "prepared" ] || exit 0
while read -r old new ref; do
  [ "$ref" = "refs/heads/main" ] && { echo "fake guard: main refused" >&2; exit 1; }
done
exit 0
"""


class FastForward(unittest.TestCase):
    """main moves only through fast_forward: a guard refusal leaves main and the checkout exactly as they were
    (pilot2026-11-10 07:35-08:10: `git merge --ff-only` wrote the files, the guard refused, the repo stayed dirty)."""

    def setUp(self):
        self.r = Path(tempfile.mkdtemp(prefix="plan-ff-"))
        self.addCleanup(shutil.rmtree, self.r, True)
        for args in (("init", "-q", "-b", "main"), ("config", "user.name", "t"), ("config", "user.email", "t@x"),
                     ("config", "core.autocrlf", "false"), ("config", "commit.gpgsign", "false")):
            self.git(*args)
        self.put("src/a.py", "a = 1\n")
        self.put("notes.txt", "n\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.git("checkout", "-qb", "plan/x")
        self.put("src/a.py", "a = 2\n")
        self.put("src/new.py", "b = 1\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "plan: x")
        self.git("checkout", "-q", "main")
        self.put("notes.txt", "local edit\n")  # an unrelated local change the owner made
        self.put("scratch.txt", "untracked\n")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.r), *args], capture_output=True, text=True, check=True).stdout

    def put(self, rel: str, text: str) -> None:
        p = self.r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    def guard(self, body: str) -> None:
        hooks = self.r / self.git("rev-parse", "--git-path", "hooks").strip()
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "reference-transaction").write_text(body, encoding="utf-8", newline="\n")
        (hooks / "reference-transaction").chmod(0o755)
        self.git("config", "core.hooksPath", hooks.as_posix())

    def test_a_refused_fast_forward_leaves_main_and_the_checkout_as_they_were(self):
        self.guard(REFUSE)
        head, status = self.git("rev-parse", "main"), self.git("status", "--porcelain")
        ok, why = plan_land.fast_forward(self.r, "plan/x", harness=False)
        self.assertFalse(ok)
        self.assertIn("fake guard: main refused", why)
        self.assertNotIn("CHECKOUT CHANGED", why)
        self.assertEqual(self.git("rev-parse", "main"), head)
        self.assertEqual(self.git("status", "--porcelain"), status)
        self.assertEqual((self.r / "src" / "a.py").read_text(encoding="utf-8"), "a = 1\n")
        self.assertFalse((self.r / "src" / "new.py").exists())

    def test_an_allowed_fast_forward_moves_main_and_keeps_local_changes(self):
        self.guard(REFUSE.replace("exit 1;", "exit 0;"))
        status = self.git("status", "--porcelain")
        ok, why = plan_land.fast_forward(self.r, "plan/x", harness=False)
        self.assertTrue(ok, why)
        self.assertEqual(self.git("rev-parse", "main"), self.git("rev-parse", "plan/x"))
        self.assertEqual(self.git("status", "--porcelain"), status)  # only the owner's own changes, as before
        self.assertEqual((self.r / "src" / "a.py").read_text(encoding="utf-8"), "a = 2\n")
        self.assertEqual((self.r / "notes.txt").read_text(encoding="utf-8"), "local edit\n")

    def test_a_local_change_the_fast_forward_would_overwrite_refuses_first(self):
        self.put("src/a.py", "a = 9\n")
        head, status = self.git("rev-parse", "main"), self.git("status", "--porcelain")
        ok, why = plan_land.fast_forward(self.r, "plan/x", harness=False)
        self.assertFalse(ok)
        self.assertIn("local changes", why)
        self.assertEqual((self.git("rev-parse", "main"), self.git("status", "--porcelain")), (head, status))


if __name__ == "__main__":
    unittest.main()


class StaticCheckReadsStdout(unittest.TestCase):
    """plan_validate prints notes (warnings) to stderr after its PLAN line on stdout; the PLAN line decides
    (pilot2026-11-10 16:13-17:00: every plan branch refused as 'not PLAN OK: PLAN FAIL' from a stderr note)."""

    def test_plan_ok_on_stdout_with_stderr_notes_passes(self):
        done = subprocess.CompletedProcess([], 0, "notes\nPLAN OK 3 functions\n", "warning: row X has no packet\n")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(plan_land.subprocess, "run", return_value=done):
            self.assertEqual(plan_land.static_check(Path(d), {"commands": {}}, Path(d) / "out"), "")
