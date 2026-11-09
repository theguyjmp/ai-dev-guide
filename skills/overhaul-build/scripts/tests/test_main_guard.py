"""The main guard: outside the harness, main moves only forward and only with plan data and configuration."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import drive  # noqa: E402


def run(cwd, *args, env=None):
    e = {k: v for k, v in os.environ.items() if k != "OVERHAUL_HARNESS"}
    e.update(env or {})
    return subprocess.run(["git", *args], cwd=cwd, env=e, capture_output=True, text=True)


def subject(cwd):
    return run(cwd, "log", "-1", "--format=%s", "main").stdout.strip()


class MainGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Path(self.tmp.name) / "r"
        self.r.mkdir()
        for a in (("init", "-q", "-b", "main"), ("config", "user.email", "t@t"), ("config", "user.name", "t"),
                  ("config", "core.autocrlf", "false")):
            run(self.r, *a)
        (self.r / "spec").mkdir()
        (self.r / "crates").mkdir()
        (self.r / "spec" / "a.md").write_text("a\n")
        (self.r / "crates" / "x.rs").write_text("x\n")
        run(self.r, "add", ".")
        run(self.r, "commit", "-qm", "init")
        drive.install_guard(self.r)
        run(self.r, "checkout", "-q", "-b", "fn/X")
        (self.r / "crates" / "x.rs").write_text("x\nz\n")
        run(self.r, "commit", "-qam", "fn code")
        run(self.r, "checkout", "-q", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_installed_once_and_idempotent(self):
        hook = Path(run(self.r, "rev-parse", "--git-common-dir").stdout.strip())
        hook = (hook if hook.is_absolute() else self.r / hook) / "hooks" / "reference-transaction"
        self.assertTrue(hook.is_file())
        drive.install_guard(self.r)
        self.assertEqual(hook.read_bytes(), (drive.HERE / "guard" / "reference-transaction").read_bytes())

    def test_plan_commit_on_main_allowed(self):
        (self.r / "spec" / "a.md").write_text("a\nb\n")
        self.assertEqual(run(self.r, "commit", "-qam", "plan edit").returncode, 0)
        self.assertEqual(subject(self.r), "plan edit")

    def test_plan_commit_may_add_stubs_but_never_change_code(self):
        (self.r / "crates" / "new.rs").write_text("pub fn f() { todo!() }\n")
        with open(self.r / "crates" / "x.rs", "a") as fh:
            fh.write("mod new;\n")
        run(self.r, "add", ".")
        self.assertEqual(run(self.r, "commit", "-qm", "plan: S99 stub").returncode, 0)
        self.assertEqual(subject(self.r), "plan: S99 stub")
        (self.r / "crates" / "x.rs").write_text("changed\n")
        self.assertNotEqual(run(self.r, "commit", "-qam", "plan: S99 rewrite").returncode, 0)
        self.assertEqual(subject(self.r), "plan: S99 stub")
        run(self.r, "checkout", "-q", "--", ".")
        (self.r / "crates" / "other.rs").write_text("pub fn g() {}\n")
        run(self.r, "add", ".")
        self.assertNotEqual(run(self.r, "commit", "-qm", "wire it").returncode, 0)

    def test_plan_may_reshape_a_pure_plan_stub_only(self):
        (self.r / "crates" / "stub.rs").write_text("pub struct P { a: u8 }\npub fn f() { todo!() }\n")
        run(self.r, "add", ".")
        self.assertEqual(run(self.r, "commit", "-qm", "plan: S98 stub").returncode, 0)
        (self.r / "crates" / "stub.rs").write_text("pub use crate::shared::P;\npub fn f() { todo!() }\n")
        self.assertEqual(run(self.r, "commit", "-qam", "plan: dedupe stub params").returncode, 0)
        self.assertEqual(subject(self.r), "plan: dedupe stub params")

    def test_code_reaches_main_only_through_the_harness(self):
        (self.r / "crates" / "x.rs").write_text("x\ny\n")
        self.assertNotEqual(run(self.r, "commit", "-qam", "code").returncode, 0)
        run(self.r, "checkout", "-q", "--", ".")
        self.assertNotEqual(run(self.r, "merge", "-q", "--ff-only", "fn/X").returncode, 0)
        self.assertNotEqual(run(self.r, "update-ref", "refs/heads/main", "fn/X").returncode, 0)
        self.assertEqual(subject(self.r), "init")
        ok = run(self.r, "update-ref", "refs/heads/main", "fn/X", env={"OVERHAUL_HARNESS": "land"})
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(subject(self.r), "fn code")

    def test_main_never_rewinds_or_disappears(self):
        run(self.r, "update-ref", "refs/heads/main", "fn/X", env={"OVERHAUL_HARNESS": "land"})
        self.assertNotEqual(run(self.r, "reset", "-q", "--hard", "HEAD~1").returncode, 0)
        run(self.r, "checkout", "-q", "fn/X")
        self.assertNotEqual(run(self.r, "branch", "-f", "main", "HEAD~1").returncode, 0)
        self.assertEqual(subject(self.r), "fn code")
        self.assertNotEqual(run(self.r, "branch", "-D", "main").returncode, 0)

    def test_builders_do_not_inherit_the_marker(self):
        os.environ["OVERHAUL_HARNESS"] = "drive"
        drv = drive.Driver.__new__(drive.Driver)
        drv.ov = self.r / ".overhaul"
        drv.slots, drv.jobs = {}, {}
        env = drv.slot_env("F01")
        self.assertNotIn("OVERHAUL_HARNESS", env)


if __name__ == "__main__":
    unittest.main()
