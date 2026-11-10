"""Main red, systemic failures, dead depfixes (drive.py via mainred.py) and the throughput tripwire (verdict.py).

Measured on the pilot project 2026-11-09: a plan commit left main failing dup_blocks, every build failed the pre-review dup
gate as its own attempt and nothing landed for hours; depfix passes kept ending "builder left no committed change".
"""
import contextlib
import datetime as dt
import io
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import drive  # noqa: E402
import mainred  # noqa: E402
import verdict as vd  # noqa: E402
from test_drive import CHAIN, Toy, git, put  # noqa: E402

DUPCHECK = """import pathlib, sys
p = pathlib.Path("src/alpha/stub.py")
if p.is_file() and "BAD" in p.read_text():
    print("src/alpha/stub.py:1: 6 lines repeat at src/alpha/other.py:1")
    sys.exit(1)
print("DUP OK")
"""
WT_DUP = ("dup failed (exit 1) in the worktree after merging main, before review: `py -3 tools/dupcheck.py`: "
          "src/alpha/f1.py:3: 6 lines repeat")


class Base(Toy):
    def setUp(self):
        self.repo = self.make_repo(functions=CHAIN, commands_extra='dup = "py -3 tools/dupcheck.py"')
        put(self.repo / "tools" / "dupcheck.py", DUPCHECK)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "plan: dup check")
        self.drv = drive.Driver(self.repo, drive.load_config(self.repo))
        self.drv.load()
        self.fixes = []
        self.drv.start_polish_fix = lambda module, items, fid="": self.fixes.append((fid, module, items))

    def commit(self, text: str, subject: str) -> None:
        put(self.repo / "src" / "alpha" / "stub.py", text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", subject)

    def events(self, kind: str) -> list[dict]:
        lines = (self.repo / ".overhaul" / "log.jsonl").read_text(encoding="utf-8").splitlines()
        return [e for e in map(json.loads, lines) if e.get("event") == kind]


class MainRed(Base):
    def test_plan_stub_red_on_main_stops_with_needs_plan_and_counts_no_attempt(self):
        self.commit("BAD\n", "plan: phase 4-5 stubs")
        self.drv.fail("F1", WT_DUP)
        rec = self.drv.st["functions"]["F1"]
        self.assertEqual((rec["attempts"], rec["blocked_on"], rec["status"]), (0, mainred.MAIN_RED, "planned"))
        self.assertEqual(self.drv.red_stop_reason, "main red in plan stubs: dup src/alpha/stub.py")
        self.assertEqual(self.fixes, [])
        self.assertEqual(self.events("main_red")[0]["fn"], "F1")
        self.assertTrue(self.drv.red_hold() and not self.drv.launch())
        self.assertTrue(self.drv.red_done())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.drv.finish(), 3)
        self.assertIn("needs-plan: main red in plan stubs: dup src/alpha/stub.py", out.getvalue())

    def test_landed_code_red_on_main_sends_one_depfix_then_resumes_on_green(self):
        self.commit("BAD\n", "F0: landed code")
        self.drv.fail("F1", WT_DUP)
        self.drv.fail("F4", WT_DUP)  # a second build hitting it: cached, waits, no second fix
        self.assertEqual([(f, m) for f, m, _ in self.fixes], [("depfix-main-dup", "alpha")])
        self.assertEqual(self.drv.st["main_red"]["fix"], "depfix-main-dup")
        self.assertEqual(self.drv.red_stop_reason, "")
        self.assertIn("F1", self.drv.busy())
        reviews = []
        self.drv.sync_problem = lambda fid: ""
        self.drv.start_review = lambda fid, attempt: reviews.append((fid, attempt))
        self.commit("fine\n", "depfix-main-dup: fix")
        self.assertTrue(self.drv.red_depfix("depfix-main-dup", True, ""))
        self.assertTrue(self.drv.red_step())
        self.assertNotIn("main_red", self.drv.st)
        self.assertEqual(sorted(reviews), [("F1", 1), ("F4", 1)])
        self.assertEqual(self.drv.st["functions"]["F1"]["attempts"], 0)
        self.assertEqual(len(self.events("main_green")), 1)

    def test_a_failed_main_fix_stops_the_build(self):
        self.commit("BAD\n", "F0: landed code")
        self.drv.fail("F1", WT_DUP)
        self.drv.depfix_settled("depfix-main-dup", False, "review NOs")
        self.assertIn("main red: dup: its fix depfix-main-dup failed", self.drv.red_stop_reason)

    def test_red_only_in_the_worktree_is_the_functions_own_attempt(self):
        self.drv.fail("F1", WT_DUP)
        rec = self.drv.st["functions"]["F1"]
        self.assertEqual((rec["attempts"], rec.get("blocked_on")), (1, None))
        self.assertNotIn("main_red", self.drv.st)

    def test_a_land_failing_on_red_main_waits_at_landing_and_lands_after_green(self):
        self.commit("BAD\n", "F0: landed code")
        self.drv.fail("F1", "land: dup failed (exit 1) on merged tree: src/alpha/stub.py:1")
        rec = self.drv.st["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"]), ("landing", 0))
        self.drv.land_queue.append("F4")
        self.assertFalse(self.drv.pump_land())  # no plain land on a red main
        self.commit("fine\n", "depfix-main-dup: fix")
        self.drv.red_depfix("depfix-main-dup", True, "")
        self.drv.red_step()
        self.assertEqual(self.drv.land_queue, ["F4", "F1"])


class Systemic(Base):
    def test_three_functions_with_one_reason_stop_the_build_when_main_is_green(self):
        self.drv.red_init({"systemic_window_min": 30})
        for fid, n in (("F1", 11), ("F2", 12)):
            self.drv.fail(fid, f"builder exited rc=1: panic in fn/{fid} at C:/x/{fid}.rs:{n} then /y/z.rs")
            self.assertFalse(self.drv.red_step())
        self.drv.fail("F4", "builder exited rc=1: panic in fn/F4 at C:/x/F4.rs:99 then /q/r.rs")
        self.assertEqual(len(self.drv.st["systemic"]), 1)
        self.assertEqual(self.drv.st["systemic"][0]["count"], 3)
        self.assertTrue(self.drv.red_step())
        self.assertTrue(self.drv.red_stop_reason.startswith("systemic builder exited rc=#: panic in <id>"))
        self.assertEqual(len(self.events("systemic")), 1)

    def test_systemic_whole_tree_failure_on_green_main_names_the_reason(self):
        for fid in ("F1", "F2", "F4"):
            self.drv.fail(fid, WT_DUP)
        self.drv.red_step()
        self.assertTrue(self.drv.red_stop_reason.startswith("systemic dup failed"))
        self.assertNotIn("main_red", self.drv.st)

    def test_different_reasons_or_one_function_repeating_never_trip(self):
        self.drv.fail("F1", "builder exited rc=1: a")
        self.drv.st["functions"]["F1"]["attempts"] = 0
        self.drv.fail("F1", "builder exited rc=1: a")
        self.drv.fail("F2", "builder exited rc=1: b")
        self.assertNotIn("systemic", self.drv.st)

    def test_normalize_and_window(self):
        a = mainred.normalize("dup failed (exit 1): `py -3 C:/s/dup.py crates`: crates/k/a.rs:14: 6 lines at crates/k/b.rs:14")
        b = mainred.normalize("dup failed (exit 1): `py -3 D:/t/dup.py crates`: crates/k/a.rs:20: 7 lines at crates/k/c.rs:3")
        self.assertEqual(a, b)
        self.assertNotEqual(a, mainred.normalize("dup failed (exit 1): crates/k/other.rs:1: 6 lines"))
        t = [0.0]
        w = mainred.Window(30, clock=lambda: t[0])
        self.assertEqual((w.add("k", "F1"), w.add("k", "F2")), (1, 2))
        t[0] = 31 * 60
        self.assertEqual(w.add("k", "F3"), 1)
        self.assertEqual(mainred.check_name("land: test failed (exit 1) on merged tree: x", {"test"}), "test")
        self.assertEqual(mainred.check_name("acceptance red in the worktree", {"test"}), "")


class DeadDepfix(Base):
    def wait_on(self, fid: str, dfid: str, text: str) -> None:
        self.drv.polish_meta[dfid] = ("alpha", "")
        self.drv.set(fid, blocked_on=dfid, last_reason=f"waiting on {dfid}: {text}")

    def test_second_dead_depfix_in_one_module_is_a_plan_gap(self):
        self.wait_on("F2", "depfix-F2", "src/alpha/f1.py:3 add() returns None")
        self.drv.depfix_settled("depfix-F2", False, "builder left no committed change on fn/depfix-F2")
        self.assertIn("dependency fix depfix-F2 failed", self.drv.st["functions"]["F2"]["last_reason"])
        self.wait_on("F3", "depfix-F3", "src/alpha/f2.py:9 double() is wrong")
        self.drv.depfix_settled("depfix-F3", False, "builder left no committed change on fn/depfix-F3")
        rec = self.drv.st["functions"]["F3"]
        self.assertEqual((rec["status"], rec["attempts"]), ("needs-plan", 0))
        self.assertIn("plan gap: src/alpha/f2.py:9 double() is wrong", rec["last_reason"])  # router prefixes its class
        self.assertIn("dead depfix in alpha", self.drv.st["systemic"][0]["reason"])
        self.assertTrue(self.drv.red_dead_block("F4", "alpha", "src/alpha/f1.py:1 x"))
        self.assertIn("plan gap: src/alpha/f1.py:1 x", self.drv.st["functions"]["F4"]["last_reason"])


class Throughput(unittest.TestCase):
    def project(self, statuses, events, harness=None, files=False):
        view = [{"id": f"F{i}", "module": f"m{i % 3}", "status": s, "deps": "-", "files": f"src/f{i}.py" if files else "",
                 "blocked_on": None, "attempts": 0, "last_reason": "", "updated": None, "cycle": None}
                for i, s in enumerate(statuses, 1)]
        return {"cfg": {"harness": {"max_parallel": 6, **(harness or {})}}, "view": view, "state": {},
                "total": len(view), "proven": statuses.count("proven"), "fpath": "spec/FUNCTIONS.md"}

    def ev(self, kind, minutes_ago, seconds=0.0, fn="F1"):
        return {"t": vd.iso(vd.now() - dt.timedelta(minutes=minutes_ago)), "fn": fn, "event": kind,
                "seconds": seconds}

    def test_expected_rate_from_slots_build_minutes_and_builds_per_land(self):
        evs = [self.ev("build_start", 90 - k) for k in range(6)] + [self.ev("build_end", 50, 600)] * 3
        evs += [self.ev("land_ok", 40, 120), self.ev("land_ok", 30, 120)]
        p = self.project(["planned"] * 6, evs, files=True)
        tp = vd.throughput(p, evs, vd.now())
        self.assertEqual((tp["slots"], tp["build_min"], tp["builds_per_land"]), (6, 10.0, 3.0))
        self.assertEqual(tp["expected"], 12.0)  # 6 x 60/10 / 3 = 12 < land ceiling 30
        self.assertEqual(tp["top_stage"], "idle slots")
        self.assertIn("of expected 12.0/h (slots 6, build 10m, 3 builds/land)", vd.expected_text(tp))

    def test_one_module_rule_caps_slots(self):
        p = self.project(["planned"] * 6, [self.ev("build_start", 1)])
        self.assertEqual(vd.throughput(p, [self.ev("build_start", 1)], vd.now())["slots"], 3)

    def test_rate_red_under_half_of_expected_while_builders_ran(self):
        evs = [self.ev("build_start", 120), self.ev("build_start", 10), self.ev("build_end", 5, 480)]
        p = self.project(["building", "review", "needs-plan", "needs-plan", "planned", "planned"], evs)
        doc = vd.judge(p, {}, vd.now(), None, evs)
        red = [r for r in doc["reds"] if r["kind"] == "rate"]
        self.assertEqual(len(red), 1)
        self.assertIn("rate red: 0/h < half of expected", red[0]["what"])
        self.assertIn("of expected", doc["line"])

    def test_floor_and_no_red_without_an_hour_of_builders(self):
        fresh = [self.ev("build_start", 10)]
        p = self.project(["planned"] * 3, fresh, {"min_rate_per_hour": 10})
        self.assertFalse([r for r in vd.judge(p, {}, vd.now(), None, fresh)["reds"] if r["kind"] == "rate"])
        evs = [self.ev("build_start", 120), self.ev("build_start", 10)] + [self.ev("land_ok", 20, 60)] * 4
        p = self.project(["planned"] * 3, evs, {"min_rate_per_hour": 10})
        red = [r for r in vd.judge(p, {}, vd.now(), None, evs)["reds"] if r["kind"] == "rate"]
        self.assertIn("rate red: 4/h < floor 10/h", red[0]["what"])

    def test_main_red_and_systemic_reds_from_state(self):
        p = self.project(["planned"], [])
        p["state"] = {"main_red": {"check": "dup", "sha": "abc", "files": ["a.rs"], "action": "plan fix"},
                      "systemic": [{"reason": "dup failed", "count": 4, "t": vd.iso(vd.now())},
                                   {"reason": "old", "count": 3, "t": vd.iso(vd.now() - dt.timedelta(hours=2))}]}
        doc = vd.judge(p, {}, vd.now(), None, [])
        self.assertEqual(doc["reds"][0], {"kind": "main", "what": "main red: dup at abc in a.rs", "action": "plan fix"})
        self.assertEqual([r["what"] for r in doc["reds"] if r["kind"] == "systemic"],
                         ["systemic: 4 functions fail the same way: dup failed"])


if __name__ == "__main__":
    unittest.main()
