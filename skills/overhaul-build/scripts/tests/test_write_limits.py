"""Per-worker write limits (pi-harness gap 23): write_guard.py, a PreToolUse hook drive.py installs in each builder
worktree, refuses an Edit/Write outside the pass's claim and visibility paths when it is made, not after the build."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_stacked as ts  # noqa: E402  its toy driver and a start_build without a worker
from test_drive import git  # noqa: E402
import remote  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1]
GUARD = SCRIPTS / "write_guard.py"


def hook(cwd: Path, path, tool: str = "Write") -> subprocess.CompletedProcess:
    """One fake hook call, the JSON Claude Code sends on stdin before the tool runs."""
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    event = {"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": str(cwd), "tool_input": {key: str(path)}}
    return subprocess.run([sys.executable, str(GUARD)], input=json.dumps(event), capture_output=True, text=True,
                          check=False, timeout=60)


class TestGuard(unittest.TestCase):
    def setUp(self):
        self.wt = Path(tempfile.mkdtemp(prefix="wg_", dir=Path(__file__).resolve().parent)).resolve()
        self.addCleanup(shutil.rmtree, self.wt, ignore_errors=True)
        (self.wt / ".claude").mkdir()
        (self.wt / ".claude" / "write-limits.json").write_text(json.dumps(
            {"job": "build F9", "allowed": ["src/a/f9.py", "tests/unit/", "src/a/*.toml"],
             "visibility": ["src/b/"]}), encoding="utf-8")

    def test_allowed_and_visibility_paths_pass(self):
        for rel in ("src/a/f9.py", "tests/unit/a/test_f9.py", "src/a/x.toml", "src/b/lib.rs"):
            p = hook(self.wt, self.wt / rel)
            self.assertEqual(p.returncode, 0, (rel, p.stderr))
        self.assertEqual(hook(self.wt, "src/a/f9.py", "Edit").returncode, 0)  # relative to cwd
        self.assertEqual(hook(self.wt / "src", "a/f9.py", "MultiEdit").returncode, 0)  # cwd below the root

    def test_outside_the_claim_is_refused_with_the_reason(self):
        for rel, tool in (("src/a/f8.py", "Write"), ("README.md", "Edit"), ("nb/x.ipynb", "NotebookEdit"),
                          (".claude/write-limits.json", "Write")):
            p = hook(self.wt, self.wt / rel, tool)
            self.assertEqual(p.returncode, 2, rel)
            self.assertIn(f"{rel} is outside the allowed paths of this build F9", p.stderr)
            self.assertIn("src/a/f9.py", p.stderr)
            self.assertIn("visibility-only edits in: src/b/", p.stderr)

    def test_outside_the_worktree_is_refused_but_temp_is_not(self):
        p = hook(self.wt, self.wt.parent / "main-checkout" / "src" / "a" / "f9.py")
        self.assertEqual(p.returncode, 2)
        self.assertIn("is outside your worktree", p.stderr)
        self.assertEqual(hook(self.wt, Path(tempfile.gettempdir()) / "scratch.txt").returncode, 0)

    def test_no_limits_or_a_bad_event_never_blocks(self):
        (self.wt / ".claude" / "write-limits.json").unlink()
        self.assertEqual(hook(self.wt, self.wt / "anything.py").returncode, 0)
        p = subprocess.run([sys.executable, str(GUARD)], input="not json", capture_output=True, text=True,
                           check=False, timeout=60)
        self.assertEqual(p.returncode, 0)


class TestDriveInstallsLimits(ts.StackedToy):
    def test_a_build_pass_runs_under_its_claim(self):
        drv = self.driver()
        shutil.copy(SCRIPTS / "lint_hook.py", drv.scripts / "lint_hook.py")
        shutil.copy(GUARD, drv.scripts / "write_guard.py")
        wt = self.started(drv, "F1")
        settings = json.loads((wt / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
        self.assertIn("write_guard.py", settings["PreToolUse"][0]["hooks"][0]["command"])
        self.assertIn("NotebookEdit", settings["PreToolUse"][0]["matcher"])
        self.assertIn("lint_hook.py", settings["PostToolUse"][0]["hooks"][0]["command"])
        limits = json.loads((wt / ".claude" / "write-limits.json").read_text(encoding="utf-8"))
        self.assertEqual(limits["job"], "build F1")
        self.assertIn("src/alpha/f1.py", limits["allowed"])
        self.assertIn("src/alpha/", limits["visibility"])  # the rest of its module: visibility edits only
        self.assertEqual(git(wt, "status", "--porcelain").strip(), "")  # both files stay out of git
        self.assertEqual(hook(wt, wt / "src" / "alpha" / "f1.py").returncode, 0)
        self.assertEqual(hook(wt, wt / "src" / "beta" / "b.py").returncode, 2)  # alpha depends on nothing
        drv.prepare_worktree("F1")  # a retry resets the worktree: the old pass's limits go with it
        self.assertFalse((wt / ".claude" / "write-limits.json").exists())

    def test_without_the_guard_no_limits_file_is_left_for_git_add(self):
        drv = self.driver()  # the toy scripts dir has neither hook
        wt = self.started(drv, "F1")
        self.assertFalse((wt / ".claude" / "write-limits.json").exists())


class TestRemoteHost(unittest.TestCase):
    def test_the_host_worktree_gets_the_guard_and_the_laptop_limits(self):
        root = Path(tempfile.mkdtemp(prefix="wgr_")).resolve()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        limits = {"job": "build F9", "allowed": ["src/a/"], "visibility": []}
        remote.lint_hook(root, SCRIPTS / "lint_hook.py", "")
        remote.set_limits(root, limits)  # no guard installed: nothing to enforce, nothing written
        self.assertIsNone(remote.read_limits(root))
        remote.lint_hook(root, SCRIPTS / "lint_hook.py", GUARD)
        remote.set_limits(root, limits)
        self.assertEqual(remote.read_limits(root), limits)
        self.assertIn(remote.LIMITS_FILE, (root / ".git" / "info" / "exclude").read_text(encoding="utf-8"))
        remote.lint_hook(root, SCRIPTS / "lint_hook.py", GUARD)  # a fresh worktree: no stale limits
        self.assertIsNone(remote.read_limits(root))


if __name__ == "__main__":
    unittest.main()
