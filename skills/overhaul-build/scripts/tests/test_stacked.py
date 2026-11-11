"""[harness] stacked_starts: a row builds on its dep's reviewed, unlanded branch, lands after it, is reset when that
branch changes, and its review sees only its own diff (pilot rate proof 2026-11-09: 7.5/h -> 14.5-15.8/h)."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_drive as td  # noqa: E402  its toy repo and fake worker; Toy has no tests of its own
from test_drive import drive, git, put  # noqa: E402
import stack  # noqa: E402

CHAIN3 = td.CHAIN.replace(
    "| F4 | 1 | alpha | F1 | `toy quad 4` | tests/acceptance/test_f4.py::test_quad | src/alpha/f4.py | planned |\n", "")
STACKED = "stacked_starts = true"


class StackedToy(td.Toy):
    def driver(self, functions=td.CHAIN, extra=STACKED):
        self.repo = self.make_repo(functions=functions, harness_extra=extra)
        drv = drive.Driver(self.repo, drive.load_config(self.repo))
        drv.load()
        return drv

    def branch(self, fid: str, path: str, text: str) -> str:
        """fn/<fid> off main with one commit writing path; returns its sha."""
        git(self.repo, "checkout", "-q", "-b", f"fn/{fid}")
        put(self.repo / path, text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", f"{fid}: work")
        git(self.repo, "checkout", "-q", "main")
        return git(self.repo, "rev-parse", f"fn/{fid}").strip()

    @staticmethod
    def no_spawn(drv):
        def fake(fid, kind, attempt, brief, cwd, tag="", rslot=None, max_calls=None):
            drv.jobs[fid] = SimpleNamespace(host="", model="fake", head="", proc=None, fn=fid)
        return mock.patch.object(drv, "start_job", side_effect=fake)

    def started(self, drv, fid: str) -> Path:
        with self.no_spawn(drv):
            drv.start_build(drv.fns[fid])
        drv.jobs.pop(fid, None)
        return self.repo / ".overhaul" / "wt" / fid

    def reviewed(self, drv, fid: str) -> None:
        """fid's own work committed in its worktree; it passed review and waits in the land queue."""
        wt = self.repo / ".overhaul" / "wt" / fid
        put(wt / "src" / "alpha" / f"{fid.lower()}.py", f"def run():\n    return '{fid}'\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", f"{fid}: own work")
        drv.st["functions"][fid]["status"] = "landing"
        drv.land_queue.append(fid)


class TestStackedStart(StackedToy):
    def test_starts_on_a_reviewed_dep_branch(self):
        drv = self.driver()
        sha = self.branch("F1", "src/alpha/f1.py", "def run():\n    return 'F1'\n")
        drv.st["functions"]["F1"]["status"] = "landing"
        self.assertEqual({f.id for f in drv.ready()}, {"F2", "F4"})  # both wait only on F1
        drv.stacked = False
        self.assertEqual(drv.ready(), [])  # off: deps must be proven
        drv.stacked = True
        wt = self.started(drv, "F2")
        self.assertTrue((wt / "src" / "alpha" / "f1.py").is_file())  # F1's reviewed code is in the worktree
        rec = drv.st["functions"]["F2"]
        self.assertEqual((rec["status"], rec["stacked_on"]), ("building", {"F1": sha}))
        brief = (self.repo / ".overhaul" / "briefs" / "F2.md").read_text(encoding="utf-8")
        self.assertIn("Built on unlanded deps: F1", brief)
        self.assertIn("do not edit them", brief)
        ev = [e for e in self.events(self.repo) if e["event"] == "stacked"]
        self.assertEqual([(e["fn"], e["detail"]) for e in ev], [("F2", "built on unlanded F1")])

    def test_dep_still_building_does_not_stack(self):
        drv = self.driver()
        drv.st["functions"]["F1"]["status"] = "review"
        self.assertEqual(drv.ready(), [])

    def test_review_and_checks_see_only_the_rows_own_diff(self):
        drv = self.driver()
        self.branch("F1", "src/alpha/f1.py", "def run():\n    return 'F1'\n")
        drv.st["functions"]["F1"]["status"] = "landing"
        wt = self.started(drv, "F2")
        self.reviewed(drv, "F2")
        put(self.repo / "README.md", "main moved\n")  # main moves; the pre-review sync merges it
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "main moves")
        git(wt, "merge", "-q", "--no-edit", "main")
        own = git(self.repo, "diff", "--name-only", *drv.span("fn/F2")).split()
        self.assertEqual(own, ["src/alpha/f2.py"])
        self.assertIn("src/alpha/f1.py", git(self.repo, "diff", "--name-only", "main...fn/F2"))  # what it was
        with self.no_spawn(drv):
            drv.start_review("F2", 1)
        brief = (self.repo / ".overhaul" / "briefs" / "review-F2.md").read_text(encoding="utf-8")
        self.assertIn("diff --git a/src/alpha/f2.py", brief)
        self.assertNotIn("src/alpha/f1.py", brief)
        self.assertNotIn("README.md", brief)


class TestStackedLand(StackedToy):
    def test_dep_lands_first_then_dependent(self):
        drv = self.driver()
        self.branch("F1", "src/alpha/f1.py", "def run():\n    return 'F1'\n")
        drv.st["functions"]["F1"]["status"] = "landing"
        self.started(drv, "F2")
        self.reviewed(drv, "F2")
        spawned = []
        fake = SimpleNamespace(poll=lambda: None)
        with mock.patch.object(drive, "spawn", side_effect=lambda cmd, *a, **k: spawned.append(cmd) or fake):
            self.assertFalse(drv.pump_land())  # F1 has not landed: F2 waits in the queue
            self.assertEqual((spawned, drv.land_queue), ([], ["F2"]))
            git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "fn/F1")  # F1 lands (a merge commit, as land.py)
            drv.st["functions"]["F1"]["status"] = "proven"
            stack.landed(drv, "F1")
            log = git(self.repo, "log", "--oneline", "-1", "fn/F2")
            self.assertIn("merge main after F1 landed", log)  # the dependent took the new main, no rebuild
            self.assertTrue(drv.pump_land())
        self.assertIn("F2", spawned[0])
        rec = drv.st["functions"]["F2"]
        self.assertEqual((rec.get("stacked_on"), rec.get("stacked")), (None, ["F1"]))
        self.assertEqual(git(self.repo, "diff", "--name-only", *drv.span("fn/F2")).split(), ["src/alpha/f2.py"])

    def test_drive_builds_on_a_landing_dep_and_lands_in_order(self):
        repo = self.make_repo(functions=CHAIN3, harness_extra=STACKED, scenario={"F1": {"land": ["slow"]}})
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertIn("proven 3/3", out)  # F3 stacks on F2 in turn
        seq = [(e["fn"], e["event"]) for e in self.events(repo)
               if e["event"] in ("build_start", "stacked", "land_ok", "review_pass")]
        self.assertLess(seq.index(("F2", "build_start")), seq.index(("F1", "land_ok")))  # started before F1 landed
        self.assertIn(("F2", "stacked"), seq)
        self.assertLess(seq.index(("F1", "land_ok")), seq.index(("F2", "land_ok")))
        files = git(repo, "ls-tree", "-r", "--name-only", "main")
        self.assertIn("src/alpha/f2.py", files)
        review = (repo / ".overhaul" / "briefs" / "review-F2.md").read_text(encoding="utf-8")
        self.assertNotIn("diff --git a/src/alpha/f1.py", review)

    def test_dep_land_fails_resets_the_stack(self):
        drv = self.driver()
        self.branch("F1", "src/alpha/f1.py", "def run():\n    return 'F1'\n")
        recs = drv.st["functions"]
        recs["F1"]["status"] = "landing"
        self.started(drv, "F2")
        self.reviewed(drv, "F2")  # F2 queued to land, stacked on F1
        self.started(drv, "F3")  # F3 stacked on F2 (and through it on F1), building
        self.assertEqual(list(recs["F3"]["stacked_on"]), ["F2"])
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        drv.jobs["F3"] = SimpleNamespace(proc=proc, host="", fn="F3", out=self.repo / "x.json")
        drv.fail("F1", "land: regression")  # what after_land does on a failed land
        self.assertTrue(stack.check(drv))
        self.assertEqual(proc.wait(timeout=10) is not None, True)
        for fid in ("F2", "F3"):
            self.assertEqual((recs[fid]["status"], recs[fid]["attempts"]), ("planned", 0), fid)
            self.assertNotIn("stacked_on", recs[fid])
        self.assertEqual((drv.land_queue, drv.jobs), ([], {}))
        resets = [(e["fn"], e["detail"].split(";")[0]) for e in self.events(self.repo) if e["event"] == "stack_reset"]
        self.assertEqual(resets, [("F2", "dep F1 is planned"), ("F3", "dep F2 is planned")])
        self.assertFalse(stack.check(drv))

    def test_dep_landed_another_branch_resets_at_land(self):
        drv = self.driver()
        self.branch("F1", "src/alpha/f1.py", "def run():\n    return 'F1'\n")
        drv.st["functions"]["F1"]["status"] = "landing"
        self.started(drv, "F2")
        self.reviewed(drv, "F2")
        put(self.repo / "src" / "alpha" / "f1.py", "def run():\n    return 'F1 rebuilt'\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "F1 landed from a rebuilt branch")
        drv.st["functions"]["F1"]["status"] = "proven"
        with mock.patch.object(drive, "spawn") as sp:
            self.assertFalse(drv.pump_land())
            sp.assert_not_called()
        self.assertEqual(drv.st["functions"]["F2"]["status"], "planned")
        self.assertIn("stack_reset", [e["event"] for e in self.events(self.repo)])

    def test_stack_conflict_waits_for_the_dep(self):
        drv = self.driver()
        self.branch("F1", "src/alpha/f1.py", "one\n")
        drv.st["functions"]["F1"]["status"] = "landing"
        put(self.repo / "src" / "alpha" / "f1.py", "two\n")  # main now clashes with fn/F1
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "clash")
        wt = self.started(drv, "F2")
        rec = drv.st["functions"]["F2"]
        self.assertEqual((rec["status"], rec.get("no_stack"), rec["attempts"]), ("planned", True, 0))
        self.assertEqual((wt / "src" / "alpha" / "f1.py").read_text(encoding="utf-8"), "two\n")
        self.assertNotIn("F2", {f.id for f in drv.ready()})
        self.assertIn("stack_conflict", [e["event"] for e in self.events(self.repo)])
