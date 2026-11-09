"""Process priority (builders BELOW_NORMAL, gates NORMAL, land's acceptance ABOVE_NORMAL) and the [tiers] rerun
that keeps a timing bench starved by builders from failing a land (pilotS123 on S02, 2026-11-09)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drive  # noqa: E402
import land  # noqa: E402
import priority  # noqa: E402
import test_land as tl  # noqa: E402  its toy repo; not its classes, so its tests are not collected twice

BELOW, NORMAL, ABOVE = 0x4000, 0x20, 0x8000


class FakePopen:
    """Records creationflags; exits 0 at once."""
    calls = []

    def __init__(self, *args, **kw):
        FakePopen.calls.append(kw.get("creationflags", 0))
        self.pid = 1

    def wait(self, timeout=None):
        return 0


class PriorityFlagsTest(unittest.TestCase):
    def test_windows_classes(self):
        self.assertEqual(priority.flag(priority.BELOW_NORMAL, nt=True), BELOW)
        self.assertEqual(priority.flag(priority.NORMAL, nt=True), NORMAL)
        self.assertEqual(priority.popen_kwargs(priority.ABOVE_NORMAL, nt=True), {"creationflags": ABOVE})

    def test_no_op_elsewhere(self):
        self.assertEqual(priority.flag(priority.ABOVE_NORMAL, nt=False), 0)
        self.assertEqual(priority.popen_kwargs(priority.BELOW_NORMAL, nt=False), {})

    def test_set_self_off_windows_is_false(self):
        with mock.patch.object(priority.os, "name", "posix"):
            self.assertFalse(priority.set_self(priority.NORMAL))

    def test_spawn_priority_slot_work_below_land_normal(self):
        with mock.patch.object(priority.os, "name", "nt"):
            self.assertEqual(drive.spawn_priority({"CARGO_TARGET_DIR": "x"}), BELOW)  # builder, reviewer, probe
            self.assertEqual(drive.spawn_priority(None), NORMAL)  # land.py

    @unittest.skipUnless(os.name == "nt", "creationflags are Windows only")
    def test_spawn_passes_below_normal_with_new_group(self):
        FakePopen.calls = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(drive.subprocess, "Popen", FakePopen):
            drive.spawn(["x"], Path(d), Path(d) / "w.json", {"A": "1"})
            drive.spawn(["x"], Path(d), Path(d) / "l.out")
        group = subprocess.CREATE_NEW_PROCESS_GROUP
        self.assertEqual(FakePopen.calls, [group | BELOW, group | NORMAL])

    @unittest.skipUnless(os.name == "nt", "creationflags are Windows only")
    def test_pre_review_gate_runs_normal(self):
        self.assertEqual(drive.GATE_PRIORITY, {"creationflags": NORMAL})

    @unittest.skipUnless(os.name == "nt", "creationflags are Windows only")
    def test_land_acceptance_runs_above_normal(self):
        FakePopen.calls = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(land.subprocess, "Popen", FakePopen):
            out = os.path.join(d, "land.out")
            land.run("gate", d, out, 5)
            land.red_nodes("t {node}", ["tests/acceptance/a.py::t"], d, out, 5)
        self.assertEqual(FakePopen.calls, [NORMAL, ABOVE])


class TierRecoveredTest(unittest.TestCase):
    """A failed [tiers] test reruns alone up to TIER_RERUNS times; a non-tier one keeps the batch verdict."""
    TIERS = ["tests/acceptance/s02_*.rs"]
    BENCH = "tests/acceptance/s02_bench.rs::test_s02_stats_and_bench_record"
    OTHER = "tests/acceptance/s03_io.rs::test_io"

    def recover(self, bad, results):
        calls = []

        def fake_run(cmd, cwd, out_path, timeout, level=priority.NORMAL, label=""):
            calls.append((cmd, level))
            return results.pop(0), "tail"
        with tempfile.TemporaryDirectory() as d, mock.patch.object(land, "run", fake_run), \
                mock.patch("builtins.print"):
            out = os.path.join(d, "land.out")
            kept = land.tier_recovered(bad, "t {node}", self.TIERS, {self.BENCH: "S02"}, d, out, 5)
            text = Path(out).read_text(encoding="utf-8") if os.path.exists(out) else ""
        return kept, calls, text

    def test_flaky_tier_test_recovers_on_second_rerun(self):
        kept, calls, text = self.recover([(self.BENCH, 101, "panicked")], [1, 0])
        self.assertEqual(kept, [])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(lv == priority.ABOVE_NORMAL for _, lv in calls))
        self.assertIn("tier rerun passed: proven S02 " + self.BENCH, text)
        self.assertIn("rerun 2/2", text)

    def test_tier_test_red_every_time_still_counts(self):
        kept, calls, text = self.recover([(self.BENCH, 101, "panicked")], [1, 3])
        self.assertEqual(kept, [(self.BENCH, 3, "tail")])
        self.assertEqual(len(calls), land.TIER_RERUNS)
        self.assertEqual(text, "")

    def test_non_tier_test_keeps_todays_rule(self):
        kept, calls, _ = self.recover([(self.OTHER, 1, "x"), (self.BENCH, 1, "y")], [0])
        self.assertEqual(kept, [(self.OTHER, 1, "x")])
        self.assertEqual(len(calls), 1)  # only the tier test reran

    def test_no_tiers_no_reruns(self):
        with mock.patch.object(land, "run", side_effect=AssertionError("no rerun")):
            self.assertEqual(land.tier_recovered([(self.BENCH, 1, "y")], "t {node}", (), {}, ".", "o", 5),
                             [(self.BENCH, 1, "y")])


FLAKY = '''import pathlib, subprocess, sys

COUNT = pathlib.Path(r"{count}")


def test_add():
    n = int(COUNT.read_text()) + 1 if COUNT.exists() else 1
    COUNT.write_text(str(n))
    assert n > {fails}, f"starved run {{n}}"
    out = subprocess.run([sys.executable, "toy.py", "add", "2", "3"], capture_output=True, text=True).stdout
    assert out.strip() == "5", out
'''


class FlakyTierLandTest(unittest.TestCase):
    """End to end: F1's proven test is in the scale tier, inside the footprint, and fails its first `fails` runs."""
    make_repo, land, assert_failed = tl.LandTest.make_repo, tl.LandTest.land, tl.LandTest.assert_failed
    log, state = tl.LandTest.log, tl.LandTest.state

    def flaky_repo(self, fails):
        tmp = tempfile.mkdtemp(prefix="tiercount-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        count = os.path.join(tmp, "n")
        repo = self.make_repo(branch={"toy.py": tl.toy(mul="a * b")})
        arch = "# Architecture\n\n## Module core\n\nPaths: `core/`, `toy.py`\n\n## Module toy\n\nPaths: toy.py\n"
        tl.write(repo, {"spec/ARCHITECTURE.md": arch,
                        "spec/FUNCTIONS.md": tl.FUNCTIONS.replace("| F1 | 1 | toy |", "| F1 | 1 | core |"),
                        "overhaul.toml": tl.TOML + '[tiers]\nscale = ["tests/acceptance/test_add*.py"]\n',
                        "tests/acceptance/test_add.py": FLAKY.format(count=count, fails=fails)})
        tl.git(repo, "add", "-A")
        tl.git(repo, "commit", "-q", "-m", "flaky tier")
        return repo

    def test_flaky_tier_test_recovers_and_lands(self):
        repo = self.flaky_repo(fails=2)  # the batch run and the first rerun fail, the second rerun passes
        code, out, err = self.land(repo)
        self.assertEqual(code, 0, out + err)
        self.assertIn("tier rerun passed: proven F1 tests/acceptance/test_add.py::test_add", out)
        self.assertIn("rerun 2/2", out)

    def test_tier_test_red_three_times_is_a_regression(self):
        repo = self.flaky_repo(fails=3)
        before = tl.git(repo, "rev-parse", "main")
        code, out, _ = self.land(repo)
        self.assert_failed(repo, before, code, out, "regression: proven F1 tests/acceptance/test_add.py::test_add")


if __name__ == "__main__":
    unittest.main()
