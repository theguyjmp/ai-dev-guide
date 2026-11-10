"""Pass C4 (item 8): per-phase plan gaps, probes and polish before the next phase starts, and visibility-only edits
in a function's own module outside its files cell."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import drive  # noqa: E402
from test_drive import CHAIN, Toy, git, put  # noqa: E402


class PhasePolish(Toy):
    """Default toy plan: F1 (phase 1, alpha) and F2 (phase 2, beta, deps F1)."""

    def driver(self, **kw) -> drive.Driver:
        repo = self.make_repo(phase_polish=True, **kw)
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        drv.polish_rounds = 1
        return drv

    def test_phase_1_polish_is_due_once_phase_1_is_proven_and_holds_phase_2(self):
        drv = self.driver()
        self.assertIsNone(drv.polish_phase())  # F1 not proven yet
        self.assertEqual([f.id for f in drv.ready()], ["F1"])
        drv.st["functions"]["F1"]["status"] = "proven"
        self.assertEqual(drv.polish_phase(), 1)
        self.assertEqual(drv.ready(), [])  # the barrier waits for phase 1's polish
        self.assertIs(drv.polish_state(), drv.st["phase_polish"]["1"])
        self.assertEqual(drv.polish_dir(), drv.ov / "polish" / "phase-1")
        self.assertIn("phase 1 polish", drv.polish_scope(1))
        self.assertIn("F1", drv.polish_scope(1))
        drv.st["phase_polish"]["1"]["status"] = "done"
        self.assertIsNone(drv.polish_phase())
        self.assertEqual([f.id for f in drv.ready()], ["F2"])
        self.assertIs(drv.polish_state(), drv.st["polish"])
        self.assertEqual(drv.polish_scope(1), "Round 1: the whole tree.")

    def test_no_phase_polish_without_the_barrier_or_once_the_final_polish_started(self):
        drv = self.driver()
        drv.st["functions"]["F1"]["status"] = "proven"
        drv.barrier = False
        self.assertIsNone(drv.polish_phase())
        drv.barrier = True
        drv.st["polish"] = {"round": 1, "status": "fix"}  # a resumed run past the final audit
        self.assertIsNone(drv.polish_phase())

    def test_a_failed_phase_polish_also_releases_the_next_phase(self):
        drv = self.driver()
        drv.st["functions"]["F1"]["status"] = "proven"
        drv.st["phase_polish"] = {"1": {"status": "failed"}}
        self.assertEqual([f.id for f in drv.ready()], ["F2"])

    def test_plan_gaps_split_by_phase(self):
        drv = self.driver()
        drv.gaps_path().parent.mkdir(parents=True, exist_ok=True)
        drv.gaps_path().write_text("\n".join(json.dumps(e) for e in [
            {"fn": "F1", "module": "alpha", "gap": "export LIMIT"},
            {"fn": "F2", "module": "beta", "gap": "export fmt"},
            {"fn": "polish-r1-x", "gap": "orphan"}]) + "\n", encoding="utf-8")
        drv.st["functions"]["F1"]["status"] = "proven"
        self.assertEqual([e["gap"] for e in drv.scope_gaps()], ["export LIMIT"])
        drv.polish_state().update(gaps="started", status="done")  # phase 1 state: its gap pass ran
        self.assertEqual([e["gap"] for e in drv.scope_gaps()], ["export fmt", "orphan"])

    def test_phase_polish_waits_only_for_the_probes_of_modules_that_phase_finished(self):
        drv = self.driver()
        drv.probe_on = True
        self.assertTrue(drv.probe_waiting(1))
        drv.probe_state()["alpha"] = {"status": next(iter(drive.PROBE_DONE))}
        self.assertFalse(drv.probe_waiting(1))  # beta has a phase 2 function: probed later
        self.assertTrue(drv.probe_waiting())

    def test_drive_polishes_phase_1_before_phase_2_starts_then_the_whole_tree(self):
        repo = self.make_repo({"audit": [[]]}, phase_polish=True)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = [(e["fn"], e["event"], e["detail"]) for e in self.events(repo)]
        done = [i for i, e in enumerate(ev) if e[1] == "polish_done"]
        self.assertEqual(len(done), 2, ev)  # phase 1, then the final whole-tree polish
        f2 = ev.index(next(e for e in ev if e[:2] == ("F2", "build_start")))
        self.assertLess(done[0], f2)
        self.assertGreater(done[1], f2)
        st = self.state(repo)
        self.assertEqual(st["phase_polish"]["1"]["status"], "done")
        self.assertEqual(st["polish"]["status"], "done")
        self.assertTrue((repo / ".overhaul" / "polish" / "phase-1" / "audit-1.json").is_file())
        phase_brief = (repo / ".overhaul" / "briefs" / "polish-audit-p1-1.md").read_text(encoding="utf-8")
        self.assertIn("phase 1 polish", phase_brief)
        self.assertIn("`alpha`", phase_brief)
        self.assertNotIn("`beta`", phase_brief)  # the phase audit lists phase 1's modules only
        self.assertIn("Round 1: the whole tree.", self.seen("polish-audit-1.md"))  # the final audit ran last


RS = "fn now_ms() -> u64 {\n    0\n}\n"


class VisibilityOnly(unittest.TestCase):
    def diff(self, old: str, new: str) -> str:
        import difflib
        body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "a/x", "b/x", n=0))
        return "diff --git a/x b/x\n" + body

    def test_visibility_and_reexports_pass(self):
        self.assertTrue(drive.visibility_only(self.diff(RS, "pub(crate) " + RS)))
        self.assertTrue(drive.visibility_only(self.diff(RS, "pub " + RS)))
        self.assertTrue(drive.visibility_only(self.diff("mod clock;\n", "pub(crate) mod clock;\npub use clock::now_ms;\n")))
        self.assertTrue(drive.visibility_only(self.diff("const LIMIT = 5;\n", "export const LIMIT = 5;\n")))
        self.assertTrue(drive.visibility_only(self.diff("a\n", "a\nexport { fmt } from './fmt';\n")))

    def test_any_other_edit_fails(self):
        self.assertFalse(drive.visibility_only(self.diff(RS, "pub(crate) " + RS.replace("0", "1"))))
        self.assertFalse(drive.visibility_only(self.diff(RS, RS + "fn more() {}\n")))
        self.assertFalse(drive.visibility_only(self.diff("const A: u8 = 1;\n", "pub const B: u8 = 1;\n")))
        self.assertFalse(drive.visibility_only("diff --git a/x b/x\nnew file mode 100644\n@@ -0,0 +1 @@\n+pub use a;\n"))
        self.assertFalse(drive.visibility_only(""))


class VisibilityInOwnModule(Toy):
    def setUp(self):
        repo = self.make_repo(functions=CHAIN)
        put(repo / "src" / "alpha" / "clock.rs", RS)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "clock")
        self.repo = repo
        self.drv = drive.Driver(repo, drive.load_config(repo))
        self.drv.load()

    def branch(self, text: str) -> list[str]:
        git(self.repo, "checkout", "-q", "-B", "fn/F1", "main")
        put(self.repo / "src" / "alpha" / "clock.rs", text)
        git(self.repo, "commit", "-q", "-am", "edit")
        git(self.repo, "checkout", "-q", "main")
        return git(self.repo, "diff", "--name-only", "main...fn/F1").split()

    def test_the_brief_offers_visibility_edits_in_the_rest_of_the_module(self):
        text = self.drv.allowed(self.drv.fns["F1"])
        self.assertIn("visibility-only edits in the rest of module `alpha`", text)
        self.assertIn("`src/alpha/`", text)

    def test_a_visibility_edit_passes_and_a_body_edit_is_refused_before_review(self):
        f = self.drv.fns["F1"]
        self.assertEqual(self.drv.visibility_problem(f, self.branch("pub(crate) " + RS), "fn/F1"), "")
        why = self.drv.visibility_problem(f, self.branch(RS.replace("0", "7")), "fn/F1")
        self.assertIn("src/alpha/clock.rs beyond visibility", why)

    def test_files_outside_the_module_stay_with_the_review(self):
        f = self.drv.fns["F1"]
        self.assertEqual(self.drv.visibility_problem(f, ["src/beta/x.rs"], "fn/F1"), "")


if __name__ == "__main__":
    unittest.main()
