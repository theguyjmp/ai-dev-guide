"""Land throughput (pilot2026-11-09): parallel lands with a partial re-gate, heavy tests through a remote runner,
the land time budget, and drive.py's side of each (land_parallel, module footprints, land_timeout requeue).

Run from the scripts dir: py -3 -m unittest tests.test_land_throughput
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS)
import drive  # noqa: E402
import land  # noqa: E402
from tests.test_drive import FILES4, Toy  # noqa: E402
from tests.test_land import ACCEPTANCE, FUNCTIONS, LAND, PY, TOML, LandTest, git, write  # noqa: E402

# F0 (module solo) is proven and counts its runs in .overhaul/solo-runs.txt (every land worktree is .overhaul/wt/<x>).
SOLO = 'def test_solo():\n    with open("../../solo-runs.txt", "a") as f:\n        f.write("x\\n")\n'
ARCH = ("# Architecture\n\n## Module toy\nPaths: toy.py\nDepends on: -\n\n## Module extra\nPaths: extra.py\n"
        "Depends on: toy\n\n## Module solo\nPaths: solo.py\nDepends on: -\n")
EXTRA = ("import sys\n\nop, a, b = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])\n"
         "print(a - b if op == 'sub' else '?')\n")


class ParallelLandTest(LandTest):
    """--parallel: each land gates on main alone outside the lock; one whose base moved re-merges main and reruns
    only the proven rows the newly landed footprints can affect."""

    def make_par(self):
        repo = self.make_repo()
        rows = (FUNCTIONS + "| F3 | 1 | extra | F1 | extra.py | `tests/acceptance/test_sub.py::test_sub` | planned |\n"
                + "| F0 | 1 | solo | - | solo.py | `tests/acceptance/test_solo.py::test_solo` | proven |\n")
        write(repo, {"spec/FUNCTIONS.md": rows, "spec/ARCHITECTURE.md": ARCH, "tests/acceptance/test_solo.py": SOLO,
                     "tests/acceptance/test_sub.py": ACCEPTANCE.format(op="sub", want=-1).replace('"toy.py"',
                                                                                                  '"extra.py"')})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "plan F3, F0")
        git(repo, "checkout", "-q", "-b", "fn/F3")
        write(repo, {"extra.py": EXTRA})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "build F3")
        git(repo, "checkout", "-q", "main")
        return repo

    def solo_runs(self, repo):
        try:
            with open(os.path.join(repo, ".overhaul", "solo-runs.txt"), encoding="utf-8") as f:
                return len(f.read().split())
        except OSError:
            return 0

    def test_base_moved_reruns_only_the_rows_the_new_landing_affects(self):
        repo = self.make_par()
        ctx = land.gate_branch(repo, "F3", "main", 120, own=True)  # F3 gated on main as it was
        self.assertEqual(self.solo_runs(repo), 1)
        code, out, err = self.land(repo)  # F2 (module toy) lands meanwhile
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.solo_runs(repo), 2)
        self.assertTrue(land.acquire_lock(repo, 5))
        try:
            land.commit_land(repo, ctx, "main", 120)
        finally:
            land.release_lock(repo)
        self.assertTrue(ctx["regated"])
        self.assertEqual(ctx["regate"], "partial regate: 2 of 3 proven rows (landed modules: toy)")  # F1, F2
        self.assertEqual(self.solo_runs(repo), 2)  # F0's module is untouched: not rerun
        rows = git(repo, "show", "main:spec/FUNCTIONS.md")
        self.assertRegex(rows, r"(?m)^\| F2 \|.*\| proven *\|$")
        self.assertRegex(rows, r"(?m)^\| F3 \|.*\| proven *\|$")
        self.assertIn("sub", git(repo, "show", "main:extra.py"))

    def test_a_regate_still_fails_a_newly_landed_row_the_merge_breaks(self):
        repo = self.make_par()
        git(repo, "checkout", "-q", "fn/F3")  # F3's extra.py gains a line that F2's mul (below) reacts to
        write(repo, {"extra.py": EXTRA + "import pathlib\n"})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "F3 more")
        git(repo, "checkout", "-q", "main")
        ctx = land.gate_branch(repo, "F3", "main", 120, own=True)
        git(repo, "checkout", "-q", "fn/F2")  # F2's mul reads extra.py when present: only the merge breaks it
        write(repo, {"toy.py": land_toy_reading_extra()})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "F2 reads extra")
        git(repo, "checkout", "-q", "main")
        code, out, err = self.land(repo)
        self.assertEqual(code, 0, out + err)
        before = git(repo, "rev-parse", "main")
        self.assertTrue(land.acquire_lock(repo, 5))
        try:
            with self.assertRaises(land.LandFail) as cm:
                land.commit_land(repo, ctx, "main", 120)
        finally:
            land.release_lock(repo)
        self.assertIn("regression: proven F2", str(cm.exception))
        self.assertEqual(git(repo, "rev-parse", "main"), before)

    def test_two_parallel_lands_from_the_cli_both_land(self):
        repo = self.make_par()
        p = subprocess.Popen([PY, LAND, "--repo", repo, "--fn", "F3", "--parallel", "--timeout", "120"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        code, out, err = self.land(repo, "--parallel", "--wait", "60")
        out3, err3 = p.communicate(timeout=300)
        self.assertEqual((code, p.returncode), (0, 0), out + err + out3 + err3)
        self.assertTrue("(parallel, " in out and "(parallel, " in out3, out + out3)
        self.assertTrue("partial regate" in out + out3 or "no second gate" in out + out3, out + out3)
        self.assertEqual([e["event"] for e in self.log(repo)], ["land_ok", "land_ok"])
        rows = git(repo, "show", "main:spec/FUNCTIONS.md")
        self.assertRegex(rows, r"(?m)^\| F2 \|.*\| proven *\|$")
        self.assertRegex(rows, r"(?m)^\| F3 \|.*\| proven *\|$")

    def test_a_landed_file_outside_every_module_means_every_row(self):
        repo = self.make_par()
        old = git(repo, "rev-parse", "main")
        write(repo, {"spec/NOTES.md": "notes\n"})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "docs")
        rows = land.parse_functions(git(repo, "show", "main:spec/FUNCTIONS.md"))
        mid = git(repo, "rev-parse", "main")
        self.assertEqual(land.regate_rows(repo, old, mid, "spec/FUNCTIONS.md", "spec/ARCHITECTURE.md", rows, "F3"),
                         (set(), set(), set()))  # spec prose touches no test
        write(repo, {"Cargo.lock": "lock\n"})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "lock")
        new = git(repo, "rev-parse", "main")
        self.assertIsNone(land.regate_rows(repo, old, new, "spec/FUNCTIONS.md", "spec/ARCHITECTURE.md", rows, "F3"))


def land_toy_reading_extra():
    """toy.py whose mul result depends on extra.py: F3's change there breaks proven F2 only on the merged tree."""
    from tests.test_land import toy
    return toy(mul="a * b + (__import__('os').path.exists('extra.py') and 'pathlib' in open('extra.py').read())")


class ModuleDepsTest(unittest.TestCase):
    def test_dependents_follow_depends_on_transitively(self):
        arch = ("## Module core\nPaths: crates/core/\nDepends on: -\n## Module store\nPaths: crates/store/\n"
                "Depends on: core\n## Module doc\nPaths: crates/doc/\nDepends on: core, store\n"
                "## Module ui\nPaths: crates/ui/\nDepends on: doc\n## Module cli\nPaths: src/\nDepends on: none\n")
        self.assertEqual(land.module_deps(arch)["doc"], {"core", "store"})
        self.assertEqual(land.dependents(arch, {"store"}), {"store", "doc", "ui"})
        self.assertEqual(land.dependents(arch, {"core"}), {"core", "store", "doc", "ui"})
        self.assertEqual(land.dependents(arch, {"cli"}), {"cli"})


# A fake heavy runner: args sha stem test slot go to CALLS; the exit code is the next line of MODES (the last repeats).
RUNNER = """import json, sys
calls, modes = sys.argv[1], sys.argv[2]
with open(calls, "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[3:]) + chr(10))
seq = open(modes, encoding="utf-8").read().split()
n = sum(1 for _ in open(calls, encoding="utf-8"))
code = int(seq[min(n, len(seq)) - 1])
print("ssh: connect to host remote-host port 22: Connection timed out" if code == 255 else f"runner exit {code}")
sys.exit(code)
"""


class HeavyRunnerTest(LandTest):
    def setUp(self):
        self.repo = self.make_repo()
        self.tmp = tempfile.mkdtemp(prefix="heavy-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.calls, self.modes = os.path.join(self.tmp, "calls.jsonl"), os.path.join(self.tmp, "modes.txt")
        runner = os.path.join(self.tmp, "runner.py")
        write(self.tmp, {"runner.py": RUNNER})
        conf = {"commands": {"acceptance": f'"{PY}" pytest_lite.py {{file}} {{test}}'},
                "harness": {"heavy_tests": ["tests/acceptance/test_mul*.py"],
                            "heavy_runner": f'"{PY}" "{runner}" "{self.calls}" "{self.modes}" {{sha}} {{stem}} '
                                            f'{{test}} {{slot}}'}}
        self.t = land.acceptance_template(land.load_commands(conf))
        self.nodes = ["tests/acceptance/test_add.py::test_add", "tests/acceptance/test_mul.py::test_mul"]
        self.out = os.path.join(self.tmp, "land.out")

    def red(self, modes):
        write(self.tmp, {"modes.txt": " ".join(map(str, modes))})
        return land.red_nodes(self.t, self.nodes, self.repo, self.out, 60)  # main: add passes, mul is missing

    def calls_made(self):
        with open(self.calls, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def test_a_heavy_test_runs_through_the_runner_and_its_pass_counts(self):
        self.assertEqual(self.red([0]), [])  # mul would fail locally: the runner's verdict is the one used
        self.assertEqual(self.calls_made(), [[git(self.repo, "rev-parse", "HEAD"), "test_mul", "test_mul",
                                              os.path.basename(os.environ.get("CARGO_TARGET_DIR", "") or "land")]])
        self.assertIsNone(land.heavy_of(self.t, self.nodes[0]))

    def test_a_heavy_runner_failure_fails_the_test(self):
        bad = self.red([1])
        self.assertEqual([(n, c) for n, c, _ in bad], [("tests/acceptance/test_mul.py::test_mul", 1)])
        self.assertIn("runner exit 1", land.read_out(self.out))  # land.out holds its output

    def test_an_unreachable_host_retries_once_then_runs_locally(self):
        bad = self.red([255, 255])
        self.assertEqual(len(self.calls_made()), 2)
        self.assertEqual([(n, c) for n, c, _ in bad], [("tests/acceptance/test_mul.py::test_mul", 1)])  # local
        write(self.tmp, {"calls.jsonl": ""})
        self.assertEqual(self.red([255, 0]), [])  # the retry reached it
        self.assertEqual(len(self.calls_made()), 2)

    def test_no_heavy_keys_keep_the_plain_template(self):
        self.assertEqual(land.acceptance_template(land.load_commands({"commands": {"acceptance": "a {test}"},
                                                                       "harness": {"heavy_tests": ["x*"]}})),
                         "a {test}")


class LandTimeoutTest(LandTest):
    def test_a_land_over_its_budget_stops_and_names_the_slowest_test(self):
        repo = self.make_repo()
        slow = f'"{PY}" -c "import time; time.sleep(8)" && "{PY}" pytest_lite.py {{file}} {{test}}'
        write(repo, {"overhaul.toml": TOML.replace("[paths]", f"[commands.acceptance_by]\n"
                                                   f"\"tests/acceptance/test_mul*.py\" = '{slow}'\n[paths]")
                     + "[harness]\nland_timeout_min = 0.04\n"})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "plan: slow test, short budget")
        before = git(repo, "rev-parse", "main")
        code, out, _ = self.land(repo)
        self.assertEqual(code, 1, out)
        self.assertTrue(out.startswith("land timeout: over 0.04 min"), out)
        last = self.log(repo)[-1]
        self.assertEqual((last["event"], last["slowest"]), ("land_timeout", "tests/acceptance/test_mul.py::test_mul"))
        self.assertLess(last["seconds"], 8)
        self.assertEqual(git(repo, "rev-parse", "main"), before)
        self.assertEqual(self.state(repo)["functions"]["F2"]["status"], "planned")


class DriveThroughputTest(Toy):
    def test_land_parallel_key_and_parallel_flag(self):
        repo = self.make_repo(harness_extra="land_parallel = 5\nland_timeout_min = 7")
        drv = drive.Driver(repo, drive.load_config(repo))
        self.assertEqual((drv.max_lands, drv.land_timeout_s), (5, 420.0))
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        argv = json.loads((repo / ".overhaul" / "land-argv.json").read_text(encoding="utf-8"))
        self.assertIn("--parallel", argv)
        self.assertNotIn("--stack", argv)

    def test_one_module_is_one_footprint_even_with_disjoint_files(self):
        drv = drive.Driver(self.make_repo(functions=FILES4.format(f4="src/alpha/f4.py")),
                           drive.load_config(self.root / "repo"))
        drv.load()
        self.assertEqual(drv.footprint_modules("F1", ["src/alpha/f1.py"]), {"alpha"})
        self.assertEqual(drv.footprint_modules("F3", ["src/alpha/f3.py"]), {"alpha"})  # disjoint files, one module
        repo = self.make_repo()  # F1 alpha, F2 beta
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        self.assertEqual(drv.footprint_modules("F1", ["src/beta/x.py"]), {"alpha", "beta"})
        self.assertFalse(drive.paths_overlap(["src/alpha/f1.py"], ["src/alpha/f3.py"]))  # only the module joins them

    def test_first_land_timeout_is_requeued_uncharged(self):
        repo = self.make_repo({"F1": {"land": ["timeout", "ok"]}}, retries=0)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"], rec["land_timeouts"]), ("proven", 0, 1))
        ev = [(e["event"], e["fn"]) for e in self.events(repo)]
        self.assertEqual(ev.count(("land_timeout", "F1")), 1)
        self.assertEqual(ev.count(("land_fail", "F1")), 0)  # land.py's own land_timeout line is the record
        self.assertEqual(ev.count(("build_start", "F1")), 1)

    def test_second_land_timeout_is_charged(self):
        repo = self.make_repo({"F1": {"land": ["timeout", "timeout", "ok"]}}, retries=1)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"]), ("proven", 1))
        self.assertEqual([(e["event"], e["fn"]) for e in self.events(repo)].count(("build_start", "F1")), 2)


if __name__ == "__main__":
    unittest.main()
