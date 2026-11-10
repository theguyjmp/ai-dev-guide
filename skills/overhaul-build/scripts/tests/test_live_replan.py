"""Live re-plan (owner 2026-11-09): supervise.py fixes needs-plan functions while drive.py runs, and drive.py applies
the reset requests it writes at a cycle boundary, reloading the plan on main without a restart.

Measured 2026-11-09 15:07 CST on the pilot project: "rate 1.7/h of expected 1.8/h (slots 3, build 10.3m, 10 builds/land)" with
7 functions needs-plan that most of 79 planned rows depend on, and no plan fix until drive.py exited."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import drive  # noqa: E402
import supervise  # noqa: E402
import verdict  # noqa: E402
from tests import test_drive as td  # noqa: E402
from tests import test_supervise as ts  # noqa: E402

FAKE_VALIDATE = """import pathlib, sys
bad = pathlib.Path(sys.argv[sys.argv.index('--repo') + 1], 'BAD')
print('PLAN FAIL 1 problems' if bad.exists() else 'PLAN OK fake')
sys.exit(1 if bad.exists() else 0)
"""

ROWS = """# Functions

| id | phase | module | deps | entry | acceptance | status |
|----|-------|--------|------|-------|------------|--------|
| F1 | 1 | alpha | - | `toy add 1 2` | tests/acceptance/test_f1.py::test_add | planned |
{f2}
| F3 | 1 | gamma | - | `toy triple 4` | tests/acceptance/test_f3.py::test_triple | planned |
"""


class DriveRequests(td.Toy):
    def driver(self) -> tuple[Path, drive.Driver]:
        repo = self.make_repo()
        td.put(self.root / "scripts" / "plan_validate.py", FAKE_VALIDATE)
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        drv.park("F2", "stuck")
        return repo, drv

    def plan_commit(self, repo: Path, f2: str) -> None:
        td.put(repo / "spec" / "FUNCTIONS.md", ROWS.format(f2=f2))
        td.git(repo, "commit", "-q", "-am", "plan: F2 fix")

    def request(self, repo: Path, name: str) -> Path:
        path = repo / ".overhaul" / "requests" / name
        td.put(path, "L1\n")
        return path

    def kinds(self, repo: Path) -> list[str]:
        return [e["event"] for e in self.events(repo)]

    def test_reset_request_applies_like_reset_and_picks_up_new_and_changed_rows(self):
        repo, drv = self.driver()
        self.plan_commit(repo, "| F2 | 2 | beta | F1 | `toy double 5` | tests/acceptance/test_f2.py::test_double | planned |")
        req = self.request(repo, "reset-F2")
        self.assertTrue(drv.replan_step())
        rec = self.state(repo)["functions"]["F2"]
        self.assertEqual((rec["status"], rec["attempts"], rec["resets"]), ("planned", 0, 1))
        self.assertEqual(self.state(repo)["functions"]["F3"]["status"], "planned")
        self.assertIn("toy double 5", drv.fns["F2"].entry)
        self.assertIn("F3", [f.id for f in drv.order])
        self.assertFalse(req.exists())
        self.assertIn("reset_applied", self.kinds(repo))
        self.assertIn("new F3", next(e["detail"] for e in self.events(repo) if e["event"] == "plan_reload"))
        self.assertFalse(drv.replan_step())  # not looked at again before REQUEST_POLL_S

    def test_a_third_reset_is_refused_and_logged(self):
        repo, drv = self.driver()
        drv.set("F2", resets=2)
        req = self.request(repo, "reset-F2")
        self.assertTrue(drv.replan_step())
        self.assertEqual(self.state(repo)["functions"]["F2"]["status"], "needs-plan")
        self.assertIn("already had 2 plan fixes", next(e["detail"] for e in self.events(repo)
                                                       if e["event"] == "reset_refused"))
        self.assertFalse(req.exists())

    def test_reload_refused_when_plan_validate_fails_and_not_retried_on_the_same_plan(self):
        repo, drv = self.driver()
        (repo / "BAD").write_text("x")
        self.plan_commit(repo, "| F2 | 2 | beta | F1 | `toy double 5` | tests/acceptance/test_f2.py::test_double | planned |")
        req = self.request(repo, "reset-F2")
        self.assertFalse(drv.replan_step())
        self.assertIn("PLAN FAIL", next(e["detail"] for e in self.events(repo) if e["event"] == "reload_refused"))
        self.assertTrue(req.exists())
        self.assertNotIn("F3", drv.fns)
        self.assertEqual(self.state(repo)["functions"]["F2"]["status"], "needs-plan")
        drv.last_request_check = 0.0
        self.assertFalse(drv.replan_step())
        self.assertEqual(self.kinds(repo).count("reload_refused"), 1)
        (repo / "BAD").unlink()  # the next plan commit passes
        td.put(repo / "spec" / "NOTES.md", "fixed\n")
        td.git(repo, "add", "-A")
        td.git(repo, "commit", "-q", "-m", "plan: fixed")
        drv.last_request_check = 0.0
        self.assertTrue(drv.replan_step())
        self.assertEqual(self.state(repo)["functions"]["F2"]["status"], "planned")

    def test_a_split_reloads_on_a_reload_request(self):
        repo, drv = self.driver()
        self.plan_commit(repo, "| F2 | 2 | beta | F1 | `toy double 4` | tests/acceptance/test_f2.py::test_double | deferred |")
        req = self.request(repo, "reload-L1")
        self.assertTrue(drv.replan_step())
        self.assertNotIn("F2", drv.fns)
        self.assertIn("F2", drv.deferred)
        self.assertNotIn("F2", self.state(repo)["functions"])
        self.assertIn("F3", drv.fns)
        self.assertFalse(req.exists())

    def test_a_reload_request_rereads_the_slot_counts(self):
        repo, drv = self.driver()
        toml = repo / "overhaul.toml"
        text = toml.read_text(encoding="utf-8")
        self.assertIn("[harness]", text)
        td.put(toml, text.replace("[harness]", "[harness]\nmax_parallel = 3\nland_parallel = 5", 1))
        self.request(repo, "reload-slots")
        self.assertTrue(drv.replan_step())
        self.assertEqual((drv.max_parallel, drv.max_lands), (3, 5))
        self.assertIn("slots_reload", self.kinds(repo))

    def test_drive_applies_a_pending_request_at_start_and_builds_it(self):
        repo, drv = self.driver()
        self.request(repo, "reset-F2")
        code, out = self.drive_cli(repo)
        self.assertEqual(code, 0, out[-800:])
        self.assertEqual(self.state(repo)["functions"]["F2"]["status"], "proven")
        self.assertIn("reset_applied", self.kinds(repo))


LIVE_DRIVE = ts.COMMON + r'''
args = sys.argv[2:]
repo = Path(args[args.index("--repo") + 1])
if "--reset" in args:
    note("reset", id=args[args.index("--reset") + 1])
    sys.exit(0)
runs = sum(1 for l in (ROOT / "calls.jsonl").read_text().splitlines() if '"drive"' in l) if (ROOT / "calls.jsonl").exists() else 0
note("drive", start=time.time())
req = repo / ".overhaul" / "requests"
(repo / ".overhaul").mkdir(exist_ok=True)
def state(needs):
    fns = {}
    for line in rows((repo / "spec" / "FUNCTIONS.md").read_text(encoding="utf-8")):
        if line.split("|")[7].strip() != "deferred":
            fns[rid(line)] = {"status": "needs-plan" if rid(line) in needs else "proven", "resets": 0,
                              "last_reason": "fake"}
    (repo / ".overhaul" / "state.json").write_text(json.dumps({"functions": fns}))
def pending():
    return sorted(p.name for p in req.iterdir()) if req.is_dir() else []
if runs == 0:
    state(SC["needs"])
    if SC.get("land_during"):  # a land moves main after the plan-fix branch was cut
        end = time.time() + 30
        while time.time() < end and subprocess.run(["git", "rev-parse", "-q", "--verify", "plan/fix-S01"], cwd=repo,
                                                   capture_output=True).returncode:
            time.sleep(0.05)
        (repo / "landed.txt").write_text("S03 code")
        git(repo, "add", "landed.txt")
        git(repo, "commit", "-q", "-m", "S03: land")
    end = time.time() + SC.get("wait", 60)
    while time.time() < end and not pending():
        time.sleep(0.05)
got = pending()
note("requests", names=got)
for name in got:
    (req / name).unlink()
code = 0 if got else SC["exits"][min(runs, len(SC["exits"]) - 1)]
state(SC["needs"] if code == 3 else [])
print(f"DRIVE EXIT {code}")
sys.exit(code)
'''


@mock.patch.object(supervise, "LIVE_POLL_S", 0.2)
class Live(ts.Toy):
    def live(self, **scenario) -> supervise.Supervisor:
        self.make(**scenario)
        (self.root / "fake_drive.py").write_text(LIVE_DRIVE, encoding="utf-8")
        return self.sup()

    def fixes(self) -> list[dict]:
        return [c for c in self.calls("claude") if c["role"] == "fix"]

    def test_a_needs_plan_row_is_fixed_and_reset_while_drive_runs(self):
        sup = self.live(exits=[3], needs=["S01"], kinds={"S01": "acceptance"}, land_during=True)
        self.assertEqual(sup.supervise(), 0, self.log())
        self.assertEqual(len(self.calls("drive")), 1)  # one drive run: no exit, no Exit-3 round
        self.assertEqual(self.calls("requests")[0]["names"], ["reset-S01"])
        self.assertEqual(len(self.fixes()), 1)
        self.assertIn("round L1", self.fixes()[0]["brief"])
        subjects = ts.git(self.repo, "log", "--format=%s", "main").splitlines()
        self.assertEqual(subjects[:2], ["plan: S01 acceptance: fake", "S03: land"])  # rebased onto the land
        runs = self.calls("validate")
        self.assertEqual([(Path(r["repo"]).name, r["run"]) for r in runs], [("_merge", False)])
        self.assertIn("live round L1: landed; reset requests S01", self.log())
        self.assertFalse((self.repo / ".overhaul" / "supervise" / "round.json").exists())
        self.assertFalse((self.repo / ".overhaul" / "lock").exists())
        self.assertEqual(len(ts.git(self.repo, "worktree", "list").splitlines()), 1)

    def test_a_split_writes_a_reload_request(self):
        sup = self.live(exits=[3], needs=["S01"])
        self.assertEqual(sup.supervise(), 0, self.log())
        self.assertEqual(self.calls("requests")[0]["names"], ["reload-L1"])
        self.assertEqual(self.main_rows()["S01"], "deferred")
        self.assertIn("S01: replaced", self.log())

    def test_drive_exit_waits_for_the_live_round_then_drives_again_without_a_round(self):
        sup = self.live(exits=[3, 0], needs=["S01"], kinds={"S01": "acceptance"}, wait=0.3)
        self.assertEqual(sup.supervise(), 0, self.log())
        self.assertEqual([c["names"] for c in self.calls("requests")], [[], ["reset-S01"]])
        self.assertEqual(len(self.fixes()), 1)
        self.assertIn("no Exit-3 round used", self.log())

    def test_a_failed_live_round_leaves_the_function_to_one_exit3_round_never_both_at_once(self):
        sup = self.live(exits=[3], needs=["S01"], kinds={"S01": "none"}, wait=3)
        self.assertEqual(sup.supervise(), 1)
        a, b = self.fixes()
        self.assertGreaterEqual(b["start"], a["end"])  # never two rounds on one function
        self.assertIn("live round L1: round L1: no plan fix succeeded (S01); still needs-plan", self.log())
        self.assertIn("round 1: no plan fix succeeded (S01)", self.log())

    def test_a_red_plan_land_keeps_main_and_leaves_the_row_to_exit3(self):
        sup = self.live(exits=[3], needs=["S01"], kinds={"S01": "acceptance"})
        sup.plan_land_cmd = [sys.executable, "-c", ts.PlanLandGate.RED]
        head = ts.git(self.repo, "rev-parse", "main")
        sup.supervise()
        self.assertIn("live round L1: round L1: plan/fixes-rL1 fails proven tests", self.log())
        self.assertEqual(ts.git(self.repo, "rev-parse", "main"), head)
        self.assertEqual(self.calls("requests")[0]["names"], [])


class VerdictLine(unittest.TestCase):
    def test_needs_plan_names_the_round_in_flight(self):
        with tempfile.TemporaryDirectory() as odir:
            self.assertEqual(verdict.needs_plan_text({"proven": 2}, odir), "")
            self.assertEqual(verdict.needs_plan_text({"needs-plan": 7}, odir), " needs-plan 7 (no round in flight)")
            Path(odir, "supervise").mkdir()
            Path(odir, "supervise", "round.json").write_text(json.dumps({"round": "L1", "ids": ["R05", "R09"]}))
            self.assertEqual(verdict.needs_plan_text({"needs-plan": 7}, odir),
                             " needs-plan 7 (round in flight: R05 R09)")


if __name__ == "__main__":
    unittest.main()
