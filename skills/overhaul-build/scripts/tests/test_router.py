"""router.py: failure classes from real the pilot project event texts, routing through drive.py's hooks, the route round in
supervise.py (fake claude/drive/validate from test_supervise), and the verdict's failures line.

Run from the scripts dir: py -3 -m unittest tests.test_router
"""
import datetime as dt
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import router  # noqa: E402
from drive import in_paths, module_paths, module_section, paths_overlap  # noqa: E402,F401  the hooks read them here
from tests import test_supervise as ts  # noqa: E402
from tests import test_verdict as tv  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "router_app.json").read_text(encoding="utf-8"))
UTC = dt.timezone.utc


class Classify(unittest.TestCase):
    def test_twenty_pilot_failures_get_their_class(self):
        self.assertEqual(len(FIXTURE), 20)
        for case in FIXTURE:
            with self.subTest(fn=case["fn"], event=case["event"]):
                self.assertEqual(router.classify(case["detail"], case["builder"]), case["class"])

    def test_review_fail_naming_a_stub_or_a_stray_path_is_review_quality(self):
        for nos in ("[K2 P1] `src/alpha/f1.py:1` stub left in f1.py", "src/beta/f1_stray.py is outside the allowed paths"):
            self.assertEqual(router.classify(f"review FAIL: 1 verified blocking NOs; fix every one:\n- {nos}"),
                             "review_quality")

    def test_thin_reason_without_builder_text_is_other(self):
        self.assertEqual(router.classify("builder left no committed change on fn/S45"), "other")

    def test_blocked_by_outside_the_cell_that_needs_the_file_is_a_files_gap(self):
        b = "BLOCKED-BY: crates/jobs/src/worker.rs:176 my files column needs worker.rs to call the payload"
        self.assertEqual(router.classify("acceptance red in the worktree after merging main", b,
                                         ["crates/jobs/src/payload.rs"]), "files_gap")
        b2 = "BLOCKED-BY: crates/kernel/src/engine.rs:53 the engine has no revolve kind (todo!())"
        self.assertEqual(router.classify("acceptance red in the worktree after merging main", b2,
                                         ["crates/kernel/src/solid_revolve.rs"]), "missing_dep")

    def test_regression_in_a_1m_scale_test_is_a_retry_not_oversized(self):
        node = "tests/acceptance/s110_cull_budget.rs::test_s110_cull_1m_under_budget_p19"
        self.assertEqual(router.classify(f"land: regression: proven S110 {node} fails (exit 101)"), "other")
        self.assertEqual(router.classify(f"regression before review: proven S110 {node} (exit 101) is red"), "other")
        self.assertEqual(router.classify(f"acceptance still red after merge: {node} (exit 101)"), "oversized")

    def test_load_flake_and_reviewer_timeout(self):
        self.assertEqual(router.classify("land: regression: proven S02 bench failed in the batch, passed alone on "
                                         "rerun 1/2"), "load_flake")
        self.assertEqual(router.classify("reviewer timed out"), "other")


def ev(minutes_ago, event, fn, detail, now):
    return {"t": (now - dt.timedelta(minutes=minutes_ago)).isoformat(), "event": event, "fn": fn, "detail": detail}


class Window(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 11, 9, 22, 0, tzinfo=UTC)

    def routed(self, classes, minutes_ago=5):
        return [ev(minutes_ago, "routed", f"F{i}", f"class={c} -> retry: x", self.now) for i, c in enumerate(classes)]

    def test_one_class_over_30_percent_is_red_in_words(self):
        f = router.failures(self.routed(["files_gap"] * 5 + ["review_quality"] * 2 + ["other"] * 2), self.now)
        self.assertTrue(f["red"])
        self.assertEqual(f["words"], "most failures: builders cannot edit a needed file (5 of 9)")
        self.assertEqual(f["line"], "failures 60m: 9 (files_gap 5, review_quality 2, other 2); top files_gap")

    def test_spread_failures_and_small_counts_are_not_red(self):
        spread = ["files_gap", "missing_dep", "contract_defect", "main_flake", "load_flake", "oversized",
                  "review_quality", "other", "files_gap", "oversized"]
        self.assertFalse(router.failures(self.routed(spread), self.now)["red"])
        self.assertFalse(router.failures(self.routed(["oversized"] * 2), self.now)["red"])

    def test_older_than_60_min_drops_out(self):
        f = router.failures(self.routed(["files_gap"] * 4, minutes_ago=61) + self.routed(["other"], 1), self.now)
        self.assertEqual(f["counts"], {"other": 1})

    def test_raw_pilot_log_collapses_one_attempt_per_function(self):
        events = [dict(c, t=c["t"]) for c in FIXTURE]
        builders = {c["fn"]: c["builder"] for c in FIXTURE if c["builder"]}
        end = router.parse_t("2026-11-09T16:00:00-06:00")
        f = router.failures(events, end, 240, builders.get)
        # R0524 blocked_by+needs_plan, R0501 review_fail+needs_plan and S140 build_end+needs_plan are one each
        self.assertEqual(f["total"], 17)
        self.assertEqual(f["counts"]["files_gap"], 3)
        self.assertEqual(f["counts"]["oversized"], 3)
        self.assertEqual(f["counts"]["main_flake"], 3)


class Fn:
    def __init__(self, fid, module, files):
        self.id, self.module, self.files = fid, module, files


class FakeDrive:
    """The attributes and methods of drive.Drive the router hooks use."""

    def __init__(self, root: Path):
        self.ov = root / ".overhaul"
        self.ov.mkdir(parents=True)
        self.fns = {"S39": Fn("S39", "jobs", ["crates/jobs/src/payload.rs"]),
                    "S43": Fn("S43", "store", ["crates/store/src/a.rs"]),
                    "S50": Fn("S50", "view", ["crates/view/src/b.rs"])}
        self.st = {"functions": {k: {"status": "planned", "attempts": 0} for k in self.fns}}
        self.land_queue, self.events, self.arch = [], [], "## Module store\nPaths: crates/store/\n"

    def log(self, fn, event, detail="", *a):
        self.events.append((fn, event, detail))

    def set(self, fid, **fields):
        self.st["functions"][fid].update(fields)

    def park(self, fid, reason):
        reason = router.on_park(self, fid, reason)
        if reason:  # '' when the router widened the claim instead
            self.set(fid, status="needs-plan", last_reason=reason)

    def say(self, msg):
        pass

    def claims(self, f):
        return f.files + list(self.st["functions"][f.id].get("files_extra") or [])

    def allowed_lists(self, f):
        return self.claims(f), ["tests/unit/"]

    def widen_scope(self, f, deps=True):  # module jobs depends on store; view on nothing
        return [f"crates/{f.module}/"] + (["crates/store/"] if deps and f.module == "jobs" else [])

    def footprint(self, fid):
        return self.fns[fid].files

    def fail(self, fid, reason):  # drive.fail with its hook
        if router.on_failure(self, fid, reason):
            return
        n = self.st["functions"][fid]["attempts"] + 1
        self.set(fid, status="planned", attempts=n, last_reason=reason)
        if n > 1:
            self.park(fid, reason)


class Hooks(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="router-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.d = FakeDrive(self.root)

    def routed(self):
        return [e for e in self.d.events if e[1] == "routed"]

    def test_files_gap_in_another_module_parks_at_once_and_asks_for_a_plan_fix(self):
        self.d.fail("S39", "changed crates/view/src/worker.rs beyond visibility: outside its files cell")
        rec = self.d.st["functions"]["S39"]
        self.assertEqual((rec["status"], rec["attempts"]), ("needs-plan", 0))
        self.assertTrue(rec["last_reason"].startswith("router files_gap: builders cannot edit a needed file"))
        route = json.loads(router.route_file(self.d.ov, "S39").read_text())
        self.assertEqual(route["class"], "files_gap")
        self.assertIn("worker.rs", route["evidence"])
        self.assertEqual(len(self.routed()), 1)  # the park did not count it twice
        self.assertIn("class=files_gap -> plan fix", self.routed()[0][2])
        self.assertEqual(json.loads(router.state_path(self.d.ov).read_text())["counts"], {"files_gap": 1})

    def test_one_route_per_function_at_a_time(self):
        self.d.fail("S39", "builder timed out after 30 min and was killed")
        self.d.set("S39", status="planned")
        self.d.fail("S39", "builder timed out after 30 min and was killed")
        self.assertIn("plan fix already in flight", self.routed()[-1][2])

    def test_load_flake_on_land_requeues_without_an_attempt_then_charges(self):
        why = "land: regression: S02 bench failed in the batch, passed alone on rerun 1/2"
        for _ in range(router.FREE_REQUEUES):
            self.d.land_queue.clear()
            self.d.fail("S50", why)
            self.assertEqual(self.d.land_queue, ["S50"])
            self.assertEqual(self.d.st["functions"]["S50"]["attempts"], 0)
        self.d.fail("S50", why)
        self.assertEqual(self.d.st["functions"]["S50"]["attempts"], 1)

    def test_review_quality_is_a_normal_retry(self):
        self.d.fail("S50", "review FAIL: 1 verified blocking NOs; fix every one")
        self.assertEqual(self.d.st["functions"]["S50"]["attempts"], 1)
        self.assertFalse(router.route_file(self.d.ov, "S50").exists())
        self.d.fail("S50", "review FAIL: 1 verified blocking NOs; fix every one")  # parks after the budget
        self.assertEqual(len(self.routed()), 2)

    def test_a_direct_contract_defect_park_is_routed(self):
        self.d.park("S43", "contract defect reported by the builder in tests/acceptance/s43.rs:9: wrong value")
        self.assertTrue(self.d.st["functions"]["S43"]["last_reason"].startswith("router contract_defect"))
        self.assertTrue(router.route_file(self.d.ov, "S43").exists())

    def test_main_flake_holds_lands_that_rerun_the_test_until_its_fix_settles(self):
        test = "tests/acceptance/s06_snapshot_reads.rs::test_s06_concurrent_readers_never_torn"
        router.on_main_flake(self.d, "S13", test, "tests/acceptance/s06_snapshot_reads.rs", "store",
                             "depfix-flake-S13")
        for fid in ("S43", "S50"):
            self.d.set(fid, status="landing")
        self.d.land_queue[:] = ["S43", "S50"]
        router.hold_lands(self.d)
        self.assertEqual(self.d.land_queue, ["S50"])  # S43 (module store) reruns it; S50 (view) does not
        router.on_depfix_settled(self.d, "depfix-flake-S13", True)
        router.hold_lands(self.d)
        self.assertEqual(self.d.land_queue, ["S50", "S43"])


# real the pilot project BLOCKED-BY lines (2026-11-09 overnight): four missing capabilities, three wrong results or stubs
CAPS = ["crates/core/src/telemetry.rs:10 input `do variable.set --stats`; wrong result: the stats line can only hold "
        "u64 counters, so it cannot carry write_mode",
        "crates/doc/src/sheet/set.rs:40 `Cell` (cells.rs:135, no formula kind) can't store a formula cell",
        "crates/store/src/element_set.rs:74 refuses not-found; right result: a journal-append store edit that adds "
        "a new record that readers and compaction see",
        "crates/store/src/paged/journal.rs:122 a journal op for a new id is dropped, so a new part record cannot be "
        "created"]
BUGS = ["crates/kernel/src/solid_box.rs:54 `solid.box` prints `{solid, volume}` with no `body` key; it should "
        "also print `body`",
        "crates/sketch/src/sketch_create.rs:210 input with no plane, wrong result: exit 3, right result: exit 0 "
        "with a default plane",
        "crates/kernel/src/engine.rs:53 the engine has no revolve kind (todo!())"]


class PermanentRoutes(unittest.TestCase):
    """Pass B 2026-11-10: a files_gap the row can fix itself widens its claim; a missing capability and a second
    BLOCKED-BY go to a plan fix at once (pilotovernight: 16 files_gap, 13 BLOCKED/no-change builds lost)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="router-pf-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.d = FakeDrive(self.root)

    def rec(self, fid):
        return self.d.st["functions"][fid]

    def test_capability_words_are_told_from_wrong_results_and_stubs(self):
        for text in CAPS:
            self.assertTrue(router.capability(text), text)
        for text in BUGS:
            self.assertFalse(router.capability(text), text)
        b = f"BLOCKED S43 abc\nBLOCKED-BY: {CAPS[3]}"
        self.assertEqual(router.classify("builder left no committed change on fn/S43", b, ["crates/x/"]),
                         "missing_capability")

    def test_files_gap_in_its_own_or_a_dep_module_widens_the_claim_and_resumes_free(self):
        self.d.fail("S39", "changed crates/jobs/src/worker.rs, crates/store/src/c.rs beyond visibility: outside "
                           "its files cell (module jobs and the modules it depends on) a function may only ...")
        rec = self.rec("S39")
        self.assertEqual((rec["status"], rec["attempts"], rec["resume"]), ("planned", 0, "building"))
        self.assertEqual(rec["files_extra"], ["crates/jobs/src/worker.rs", "crates/store/src/c.rs"])
        self.assertIn("crates/jobs/src/worker.rs", rec["widened"])
        self.assertFalse(router.route_file(self.d.ov, "S39").exists())
        self.assertTrue(any(e[1] == "files_widened" for e in self.d.events))
        self.assertIn("claim widened", [e for e in self.d.events if e[1] == "routed"][-1][2])

    def test_a_review_plan_gap_widens_once_per_file_then_goes_to_a_plan_fix(self):
        gap = ("review asks for a change outside the allowed paths (plan gap): PLAN-GAP: C26: "
               "[C26 crates/jobs/src/rename.rs:83] delete_in must also remove the enum")
        self.d.park("S39", gap)
        self.assertEqual((self.rec("S39")["status"], self.rec("S39")["files_extra"]),
                         ("planned", ["crates/jobs/src/rename.rs"]))
        self.d.park("S39", gap)  # the same file again: already in the claim, so the plan has to change
        self.assertEqual(self.rec("S39")["status"], "needs-plan")
        self.assertEqual(json.loads(router.route_file(self.d.ov, "S39").read_text())["class"], "files_gap")

    def test_a_gap_naming_a_file_of_a_module_it_may_not_use_still_goes_to_a_plan_fix(self):
        self.d.fail("S50", "changed crates/store/src/c.rs beyond visibility: outside its files cell")
        self.assertEqual(self.rec("S50")["status"], "needs-plan")
        self.assertNotIn("files_extra", self.rec("S50"))

    def test_blocked_by_a_missing_capability_is_a_plan_fix_never_a_depfix(self):
        self.assertTrue(router.on_blocked(self.d, "S39", "crates/store/src/paged/journal.rs",
                                          CAPS[3].replace(":122", ":122 in store:", 1), spent=False))
        rec = self.rec("S39")
        self.assertEqual(rec["status"], "needs-plan")
        self.assertTrue(rec["last_reason"].startswith("router missing_capability: builders need a capability"))
        route = json.loads(router.route_file(self.d.ov, "S39").read_text())
        self.assertEqual(route["class"], "missing_capability")
        self.assertIn("owning module", router.HINT["missing_capability"])

    def test_blocked_by_its_own_module_widens_a_dep_module_bug_goes_to_its_depfix(self):
        self.assertTrue(router.on_blocked(self.d, "S39", "crates/jobs/src/info.rs", "crates/jobs/src/info.rs:43 "
                                          "in jobs: prints no locale; it should print `locale`", spent=False))
        self.assertEqual(self.rec("S39")["files_extra"], ["crates/jobs/src/info.rs"])
        self.assertFalse(router.on_blocked(self.d, "S39", "crates/store/src/open.rs", f"crates/store/src/open.rs:22"
                                           f" in store: {BUGS[0]}", spent=False))  # drive's depfix route
        self.assertNotIn("crates/store/src/open.rs", self.rec("S39")["files_extra"])

    def test_a_second_blocked_by_is_a_plan_fix_not_no_committed_change(self):
        self.assertTrue(router.on_blocked(self.d, "S43", "crates/kernel/src/solid_box.rs",
                                          f"crates/kernel/src/solid_box.rs:54 in kernel: {BUGS[0]}", spent=True))
        rec = self.rec("S43")
        self.assertEqual((rec["status"], rec["attempts"]), ("needs-plan", 0))
        self.assertIn("again after its dependency fix", rec["last_reason"])
        self.assertEqual(json.loads(router.route_file(self.d.ov, "S43").read_text())["class"], "missing_dep")

    def test_builder_text_reads_a_stream_json_run(self):
        runs = self.d.ov / "runs"
        runs.mkdir()
        lines = [{"type": "system"}, {"type": "assistant", "message": {}},
                 {"type": "result", "result": f"BLOCKED S43 abc\nBLOCKED-BY: {CAPS[0]}"}, {"type": "rate_limit"}]
        (runs / "S43-a1-build.json").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        self.assertIn("can only hold", router.builder_text(self.d.ov, "S43"))
        self.d.fail("S43", "builder left no committed change on fn/S43")
        self.assertEqual(json.loads(router.route_file(self.d.ov, "S43").read_text())["class"], "missing_capability")


class Round(ts.Toy):
    def needs(self, fid, cls, evidence):
        ov = self.repo / ".overhaul"
        ov.mkdir(exist_ok=True)
        (ov / "state.json").write_text(json.dumps({"functions": {
            "S01": {"status": "needs-plan", "resets": 0, "last_reason": f"router {cls}: {evidence}"},
            "S02": {"status": "planned", "resets": 0}, "S03": {"status": "proven", "resets": 0}}}))
        route = router.route_file(ov, fid)
        route.parent.mkdir(parents=True, exist_ok=True)
        route.write_text(json.dumps({"id": fid, "class": cls, "evidence": evidence}))

    def test_a_routed_function_gets_a_plan_fix_with_its_class_then_a_reset_request(self):
        self.make(kinds={"S01": "acceptance"})
        sup = self.sup()
        self.needs("S01", "files_gap", "changed crates/a/x.rs beyond visibility: outside its files cell")
        before = ts.git(self.repo, "rev-parse", "main")
        self.assertTrue(router.Watch(sup).step())
        fix = self.calls("claude")
        self.assertEqual([c["id"] for c in fix], ["S01"])
        self.assertIn("classed this failure `files_gap`", fix[0]["brief"])
        self.assertIn("outside its files cell", fix[0]["brief"])
        self.assertNotEqual(ts.git(self.repo, "rev-parse", "main"), before)
        self.assertTrue((self.repo / ".overhaul" / "requests" / "reset-S01").is_file())
        self.assertIn("landed", json.loads(router.route_file(self.repo / ".overhaul", "S01").read_text())["done"])
        self.assertFalse(router.Watch(sup).step())  # done: never routed twice
        self.assertEqual(router.apply_requests(sup), ["S01"])
        self.assertEqual([c["id"] for c in self.calls("reset")], ["S01"])

    def test_exit3_after_a_routed_land_resets_and_drives_again_without_a_round(self):
        self.make(exits=[3, 0], needs=["S01"])
        req = self.repo / ".overhaul" / "requests"
        req.mkdir(parents=True)
        (req / "reset-S01").write_text("X1\n")
        self.assertEqual(self.sup().supervise(), 0)
        self.assertEqual([c["id"] for c in self.calls("reset")], ["S01"])
        self.assertEqual([c for c in self.calls("claude") if c.get("role") == "fix"], [])
        self.assertIn("after routed plan fixes (S01): drive again", self.log())

    def test_live_rounds_own_plan_fixes_when_present(self):
        self.make()
        sup = self.sup()
        sup.live_step = lambda: None
        with router.watching(sup) as w:
            self.assertIsNone(w)


class VerdictLine(unittest.TestCase):
    repo, verdict, kinds = tv.VerdictTest.repo, tv.VerdictTest.verdict, tv.VerdictTest.kinds

    def test_failures_line_and_red_when_one_class_dominates(self):
        events = [{"t": tv.ago(minutes=5), "fn": f"F{i}", "event": "routed", "detail": f"class={c} -> plan fix: x"}
                  for i, c in enumerate(["files_gap"] * 5 + ["review_quality"] * 4)]
        repo = self.repo(["planned", "proven"], events=events)
        _, line, doc = self.verdict(repo, "--no-checks")
        self.assertEqual(doc["failures"]["counts"], {"files_gap": 5, "review_quality": 4})
        self.assertIn("failures 9 top files_gap", line)
        self.assertIn("failures", self.kinds(doc))
        red = next(r for r in doc["reds"] if r["kind"] == "failures")
        self.assertEqual(red["what"], "most failures: builders cannot edit a needed file (5 of 9)")


if __name__ == "__main__":
    unittest.main()
