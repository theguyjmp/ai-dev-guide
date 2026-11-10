"""Phase audit (owner 2026-11-09): each phase's polish runs beside the builds without the barrier, the modules it
covers take no new build until it ends, an arch audit joins its round 1, a sweep audits the whole tree every N lands,
and the verdict holds a phase until its audit has 0 verified P0/P1.

Run from the scripts dir:  py -3 -m unittest tests.test_phase_audit
"""
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import drive  # noqa: E402
import verdict as vd  # noqa: E402
from test_drive import FAKE_WORKER, THREE, Toy, git, put  # noqa: E402

# F1 (phase 1, alpha); F2 (phase 2, beta) and F3 (phase 2, alpha), both after F1.
LOCKED = """# Functions
| id | phase | module | deps | entry | acceptance | status |
|----|-------|--------|------|-------|------------|--------|
| F1 | 1 | alpha | - | `toy add 1 2` | tests/acceptance/test_f1.py::test_add | planned |
| F2 | 2 | beta | F1 | `toy double 4` | tests/acceptance/test_f2.py::test_double | planned |
| F3 | 2 | alpha | F1 | `toy triple 4` | tests/acceptance/test_f3.py::test_triple | planned |
"""
ARCH_DEPS = """# Architecture
## alpha
paths: `src/alpha/`
Adds numbers.
## beta
paths: `src/beta/`
Depends on: alpha
Doubles numbers through alpha.
"""
# Answers an arch-audit brief from scenario "arch" (one list of findings per call, the last repeats); "arch_sleep"
# delays it so the checklist audit ends first. Any other brief falls through to the fake worker of test_drive.
ARCH_WORKER = '''
import json as _json, sys as _sys, time as _time
from pathlib import Path as _Path
_brief = _Path(_sys.argv[-1].split("Read and do ", 1)[1]).read_text(encoding="utf-8")
if _brief.startswith("# Architecture audit "):
    _root = _Path(__file__).resolve().parent
    _scen = _json.loads((_root / "scenario.json").read_text(encoding="utf-8"))
    (_root / "seen").mkdir(exist_ok=True)
    _k = len(list((_root / "seen").glob("arch-*.md"))) + 1
    (_root / "seen" / f"arch-{_k}.md").write_text(_brief, encoding="utf-8")
    _time.sleep(_scen.get("arch_sleep", 0))
    _runs = _scen.get("arch") or [[]]
    _found = _runs[min(_k, len(_runs)) - 1]
    _out = next(ln.split("`")[1] for ln in _brief.splitlines() if ln.startswith("- Findings file to write:"))
    _Path(_out).write_text(_json.dumps(_found), encoding="utf-8")
    print(_json.dumps({"type": "result", "result": str(len(_found)), "num_turns": 2,
                       "usage": {"input_tokens": 50, "output_tokens": 5}}))
    _sys.exit(0)
'''
DUP = {"module": "beta", "id": "A01", "answer": "no", "file": "src/beta/parse.py", "line": 3,
       "other": "src/alpha/parse.py:7", "evidence": "both parse the toy header: same bytes in, same tuple out"}
BUILD = SimpleNamespace(kind="build", host="")  # a local job (remote-builders: local_jobs counts host == "")


class Base(Toy):
    def repo(self, scenario=None, functions=LOCKED, barrier=False, extra="", **kw) -> Path:
        repo = self.make_repo(scenario, functions=functions, phase_audit=True,
                              harness_extra=f"phase_barrier = {str(barrier).lower()}\n{extra}", **kw)
        put(self.root / "fake_worker.py", ARCH_WORKER + FAKE_WORKER)
        put(repo / "spec" / "ARCHITECTURE.md", ARCH_DEPS)
        git(repo, "commit", "-q", "-am", "deps")
        return repo

    def driver(self, **kw) -> drive.Driver:
        repo = self.repo(**kw)
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        drv.polish_rounds, drv.probe_on = 1, False
        return drv


class Units(Base):
    def test_phase_audit_is_due_without_the_barrier_and_holds_only_its_modules(self):
        drv = self.driver()
        self.assertEqual((drv.phase_audit, drv.arch_every), (True, 25))
        drv.st["functions"]["F1"]["status"] = "proven"
        self.assertEqual(drv.polish_phase(), 1)
        self.assertEqual(drv.audit_locked(), {"alpha"})  # from the moment phase 1 is proven
        self.assertEqual([f.id for f in drv.ready()], ["F2"])  # F3 is alpha: it waits for the phase 1 polish
        drv.st["phase_polish"]["1"]["status"] = "done"
        self.assertEqual(drv.audit_locked(), set())
        self.assertEqual([f.id for f in drv.ready()], ["F2", "F3"])

    def test_with_phase_audit_off_and_no_barrier_no_phase_polish(self):
        drv = self.driver()
        drv.phase_audit = False
        drv.st["functions"]["F1"]["status"] = "proven"
        self.assertIsNone(drv.polish_phase())
        self.assertEqual(drv.audit_locked(), set())

    def test_the_barrier_with_phase_polish_keeps_its_old_hold(self):
        drv = self.driver(barrier=True, phase_polish=True)
        drv.st["functions"]["F1"]["status"] = "proven"
        self.assertEqual(drv.ready(), [])
        self.assertFalse(drv.beside_builds())

    def test_polish_waits_only_for_builds_in_its_modules(self):
        drv = self.driver()
        drv.st["functions"]["F1"]["status"] = "proven"
        drv.jobs["F2"] = BUILD  # beta: the phase 1 polish starts beside it
        self.assertFalse(drv.polish_busy())
        drv.jobs["F3"] = BUILD  # alpha (launched before the lock): the polish waits for it
        self.assertTrue(drv.polish_busy())
        del drv.jobs["F3"]
        drv.jobs["depfix-F9"] = BUILD  # another fix pass in flight: it waits too
        self.assertTrue(drv.polish_busy())

    def test_a_fix_waits_for_a_build_in_its_module_and_locks_it(self):
        drv = self.driver()
        item = ("beta", [DUP])
        drv.polish_queue = [item]
        self.assertIn("beta", drv.audit_locked())
        drv.jobs["F2"] = BUILD
        self.assertEqual(drv.polish_ready(), [])
        del drv.jobs["F2"]
        self.assertEqual(drv.polish_ready(), [item])
        drv.polish_queue = []
        drv.polish_meta["arch-r25-beta"] = ("beta", "x")
        drv.polish_lands.append("arch-r25-beta")  # passed review, waiting to land: still locked
        drv.polish_meta["depfix-F2"] = ("alpha", "x")
        drv.jobs["depfix-F2"] = BUILD  # a dependency fix keeps its old behaviour: no lock
        self.assertEqual(drv.audit_locked(), {"beta"})

    def test_arch_items_carry_their_severity_and_a01_routes_to_the_caller(self):
        nos = drive.checklist_nos([{"id": "A03", "answer": "no"}, {"id": "A05"}, {"id": "A02", "answer": "yes"}])
        self.assertEqual([(n["id"], n["severity"]) for n in nos], [("A03", "P0"), ("A05", "P1")])
        drv = self.driver()
        groups = drv.route_polish([{**DUP, "module": "alpha", "file": "src/alpha/parse.py", "other": "src/beta/p.py:3"}])
        self.assertEqual(list(groups), ["beta"])
        self.assertEqual(groups["beta"][0]["owners"], ["alpha"])

    def test_the_arch_brief_lists_every_module_and_marks_the_phase_scope(self):
        drv = self.driver()
        drv.st["functions"]["F1"]["status"] = "proven"
        with mock.patch.object(drv, "start_polish_job") as job, mock.patch.object(drv, "prepare_worktree"):
            drv.jobs[drive.ARCH_FID] = SimpleNamespace(model="opus", host="")
            drv.start_arch_audit(drive.ARCH_FID, 1, drv.polish_dir() / "arch-1.json")
        brief = Path(job.call_args.args[3]).read_text(encoding="utf-8")
        self.assertTrue(brief.startswith("# Architecture audit arch-audit arch-1"))
        self.assertIn("- `alpha` (in scope): src/alpha/", brief)
        self.assertIn("- `beta`: src/beta/", brief)
        self.assertIn("A04 [P1]", brief)
        self.assertEqual(job.call_args.args[1], "arch_audit")
        self.assertEqual(drv.polish_cmd("arch_audit")[1], "custom")  # the toy's worker_cmd; else audit_model

    def test_the_sweep_starts_every_n_lands_never_beside_a_phase_polish(self):
        drv = self.driver(functions=THREE)
        drv.arch_every = 2
        with mock.patch.object(drv, "start_arch_audit") as start:
            drv.st["functions"]["F1"]["status"] = "proven"
            self.assertFalse(drv.arch_step())  # 1 land since 0
            drv.st["functions"]["F2"]["status"] = "proven"
            self.assertTrue(drv.arch_step())
            self.assertEqual(start.call_args.args[0], drive.SWEEP_FID)
            self.assertEqual(drv.sweep_state()["at"], 2)
            self.assertEqual(drv.sweep_state()["status"], "audit")
            self.assertFalse(drv.arch_step())  # its job is gone (a mock): back to idle, at stays
            self.assertEqual(drv.sweep_state(), {"at": 2, "status": "idle", "history": []})
        drv2 = self.driver()
        drv2.arch_every = 1
        drv2.st["functions"]["F1"]["status"] = "proven"  # phase 1 polish due: the sweep waits for it
        with mock.patch.object(drv2, "start_arch_audit") as start:
            self.assertFalse(drv2.arch_step())
            start.assert_not_called()

    def test_the_sweep_verifies_then_routes_fixes_and_ends_idle(self):
        drv = self.driver(functions=THREE)
        drv.sweep_state().update(at=25, status="audit")
        with mock.patch.object(drv, "start_polish_job") as job:
            drv.sweep_audited([{**DUP, "severity": "P1"}], "")
        self.assertEqual(job.call_args.args[:2], (drive.SWEEP_VERIFY, "polish_verify"))
        self.assertIn("[A01 P1]", Path(job.call_args.args[3]).read_text(encoding="utf-8"))
        drv.sweep_checked(drv.verify_meta.pop(drive.SWEEP_VERIFY), [{**DUP, "severity": "P1", "how": "t"}])
        self.assertEqual(drv.sweep_state()["status"], "fix")
        self.assertEqual([m for m, _ in drv.polish_queue], ["beta"])
        self.assertEqual(drv.sweep_state()["history"], [[25, 1, 1]])
        with mock.patch.object(drv, "start_polish_fix") as fix:
            drv.arch_step()
        self.assertEqual(fix.call_args.kwargs["fid"], "arch-r25-beta")
        drv.arch_step()  # nothing queued or in flight: the sweep is over
        self.assertEqual(drv.sweep_state()["status"], "idle")


class Runs(Base):
    def test_phase_1_polish_runs_beside_phase_2_builds_and_holds_only_alpha(self):
        repo = self.repo({"audit": [[]], "arch": [[]]})
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = [(e["fn"], e["event"], e["detail"]) for e in self.events(repo)]
        done = [i for i, e in enumerate(ev) if e[1] == "polish_done"]
        self.assertEqual(len(done), 2, ev)  # phase 1, then the final whole-tree polish
        f2 = ev.index(next(e for e in ev if e[:2] == ("F2", "build_start")))
        f3 = ev.index(next(e for e in ev if e[:2] == ("F3", "build_start")))
        self.assertLess(f2, done[0])  # beta builds while phase 1 is polished
        self.assertGreater(f3, done[0])  # alpha waits for it
        arch = [e[2] for e in ev if e[1] == "arch_audit"]
        self.assertTrue(any(d.startswith("start arch-audit: phase 1") for d in arch), arch)
        st = self.state(repo)
        self.assertEqual((st["phase_polish"]["1"]["status"], st["phase_polish"]["1"]["arch"]), ("done", []))
        self.assertEqual(st["phase_polish"]["1"]["locked"], ["alpha"])
        self.assertTrue((repo / ".overhaul" / "briefs" / "arch-audit-phase-1-arch-1.md").is_file())
        phase_brief = (repo / ".overhaul" / "briefs" / "polish-audit-p1-1.md").read_text(encoding="utf-8")
        self.assertIn("later phases build meanwhile", phase_brief)

    def test_arch_nos_join_round_1_and_are_fixed_in_the_caller(self):
        repo = self.repo({"audit": [[], []], "arch": [[DUP]], "arch_sleep": 2}, functions=THREE, barrier=True)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        verify = self.seen("polish-verify-verify-1.md")
        self.assertIn("[A01 P1] `src/beta/parse.py:3` (module beta)", verify)
        self.assertIn("(other: src/alpha/parse.py:7)", verify)
        fix = self.seen("polish-r1-beta-fix.md")
        self.assertIn("[A01]", fix)
        self.assertIn("`beta` depends on `alpha`", fix)
        st = self.state(repo)
        self.assertEqual([n["id"] for n in st["polish"]["arch"]], ["A01"])
        self.assertEqual((st["polish"]["status"], st["polish"]["issues"]), ("done", 0))
        landed = [e["fn"] for e in self.events(repo) if e["event"] == "polish_fix" and e["detail"].startswith("landed")]
        self.assertEqual(landed, ["polish-r1-beta"])

    def test_the_sweep_fixes_a_duplicate_mid_build(self):
        repo = self.repo({"audit": [[]], "arch": [[DUP], []]}, functions=THREE, barrier=True,
                         extra="arch_audit_every_lands = 1")
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = self.events(repo)
        sweeps = [e["detail"] for e in ev if e["event"] == "arch_sweep"]
        self.assertTrue(any("1 of 1 blocking NOs verified" in d for d in sweeps), sweeps)
        landed = [e["fn"] for e in ev if e["event"] == "polish_fix" and e["detail"].startswith("landed")]
        self.assertTrue(any(re.fullmatch(r"arch-r\d-beta", f) for f in landed), landed)


class Verdict(unittest.TestCase):
    def judge(self, phase_polish):
        state = {"phase_polish": phase_polish}
        p = {"view": [], "proven": 2, "total": 3, "state": state, "fpath": "f", "cfg": {}, "deferred": 0}
        return vd.judge(p, {"proven": 1}, vd.now(), None, [])

    def test_a_phase_audit_with_open_findings_is_bad_and_shown(self):
        doc = self.judge({"1": {"status": "done", "issues": 2}, "2": {"status": "audit", "issues": None}})
        self.assertIn("phase 1 audit: 2 open, phase 2 audit: ? open", doc["line"])
        self.assertEqual([r["kind"] for r in doc["reds"]], ["phase-audit"])
        self.assertEqual(doc["verdict"], "BAD")

    def test_a_clean_phase_audit_is_good(self):
        doc = self.judge({"1": {"status": "done", "issues": 0}})
        self.assertEqual(doc["verdict"], "GOOD", doc["reds"])
        self.assertIn("phase 1 audit: 0 open reds: none", doc["line"])

    def test_a_failed_arch_audit_holds_the_phase(self):
        doc = self.judge({"1": {"status": "done", "issues": 0, "arch_failed": "auditor timed out"}})
        self.assertIn("arch audit failed: auditor timed out", doc["reds"][0]["what"])


if __name__ == "__main__":
    unittest.main()
