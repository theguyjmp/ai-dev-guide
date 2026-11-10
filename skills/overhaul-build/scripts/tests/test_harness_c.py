"""Pass C: checklist v3 (C31-C37, `W` whole-tree items), drive.py --repolish, plan_validate `deferred` rows and
every FUNCTIONS table."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import drive  # noqa: E402
import plan_validate as pv  # noqa: E402
from test_drive import Toy  # noqa: E402
from test_plan_validate import FAST, OK, R1, R2, Base, r3, row, table  # noqa: E402

NEW = [f"C{n}" for n in range(31, 38)]


class ChecklistV3(unittest.TestCase):
    def test_c31_to_c37_parse_and_only_c37_is_whole_tree(self):
        version, items = drive.checklist()
        self.assertEqual(version, "v3")
        for cid in NEW:
            self.assertIn(cid, items)
            self.assertEqual(items[cid].whole, cid == "C37", cid)
            self.assertFalse(items[cid].project, cid)
        self.assertEqual(items["C37"].rules, (12, 16))
        self.assertEqual(items["C31"].severity, "P0")
        self.assertFalse(any(i.whole for c, i in items.items() if c != "C37"))

    def test_the_polish_audit_asks_every_new_item_and_marks_w_as_whole_tree(self):
        ids = [i.id for i in drive.applicable_items(None)]
        for cid in NEW:
            self.assertIn(cid, ids)
        text = drive.checklist_text(drive.applicable_items(None))
        c37 = next(ln for ln in text.splitlines() if ln.startswith("- C37 "))
        self.assertIn("whole tree", c37)
        c31 = next(ln for ln in text.splitlines() if ln.startswith("- C31 "))
        self.assertNotIn("whole tree", c31)

    def test_a_function_review_never_asks_a_w_item(self):
        row_text = "lint config clippy complexity function lines standard Q12 Q16 everything"
        self.assertTrue(drive.wanted_rules(row_text).intersection({12, 16}))  # its rules apply, still no W item
        self.assertNotIn("C37", [i.id for i in drive.applicable_items(row_text)])

    def test_the_review_of_a_polish_fix_of_a_w_finding_asks_it(self):
        issues = "module alpha\n- [C37] clippy.toml:1: cognitive-complexity-threshold 25, Q16 says 10 (lint)"
        self.assertIn("C37", [i.id for i in drive.applicable_items(issues)])
        self.assertNotIn("C37", [i.id for i in drive.applicable_items("module alpha\n- [C370] x lint")])

    def test_a_user_named_output_triggers_the_no_clobber_item(self):
        for text in ("convert IN --out FILE", "save as a new name", "write to the given output path"):
            self.assertIn(36, drive.wanted_rules(text), text)
            self.assertIn("C33", [i.id for i in drive.applicable_items(text)], text)
        self.assertNotIn("C33", [i.id for i in drive.applicable_items("add two numbers")])


class PolishBriefs(Toy):
    def test_the_audit_brief_carries_v3_and_a_function_review_omits_c37(self):
        repo = self.make_repo({"audit": [[]]})
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        audit = self.seen("polish-audit-1.md")
        self.assertIn("v3", audit)
        for cid in NEW:
            self.assertIn(f"- {cid} [", audit)
        self.assertIn("C37 [P1] (whole tree", audit)
        self.assertNotIn("- C37 [", self.seen("F1-review-1.md"))


class Repolish(Toy):
    def polish_audits(self, repo: Path) -> list[str]:
        return [e["detail"].split(" model=")[0] for e in self.events(repo) if e["event"] == "polish_audit"]

    def test_repolish_audits_again_keeps_history_and_is_bounded(self):
        repo = self.make_repo({"audit": [[]]})
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.polish_audits(repo), ["round 1: 0 issues"])
        first = self.state(repo)["polish"]
        self.assertEqual(first["status"], "done")
        rc, out = self.drive_cli(repo, "--repolish")
        self.assertEqual(rc, 0, out)
        self.assertIn("repolish", out)
        st = self.state(repo)["polish"]
        self.assertEqual((st["round"], st["status"], st["repolishes"], st["checklist"]), (0, "idle", 1, "v3"))
        self.assertEqual(st["history"], first.get("history") or [])
        self.assertTrue(any(e["event"] == "polish_repolish" for e in self.events(repo)))
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.polish_audits(repo), ["round 1: 0 issues", "round 1: 0 issues"])
        self.assertEqual(self.state(repo)["polish"]["repolishes"], 1)
        self.assertFalse([e for e in self.events(repo) if e["event"] == "plan_gaps"])  # gaps never rerun
        for _ in range(2):  # polish_rounds defaults to 3: a second and third repolish are allowed
            rc, out = self.drive_cli(repo, "--repolish")
            self.assertEqual(rc, 0, out)
        rc, out = self.drive_cli(repo, "--repolish")
        self.assertEqual(rc, 1, out)
        self.assertIn("already repolished 3 times", out)

    def driver(self, repo: Path) -> drive.Driver:
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        return drv

    def test_repolish_refuses_while_polish_is_in_flight_or_polish_rounds_is_0(self):
        repo = self.make_repo()
        drv = self.driver(repo)
        drv.st["polish"] = {"round": 1, "status": "auditing", "history": []}
        with self.assertRaisesRegex(drive.DriveError, "polish is auditing"):
            drv.repolish()
        drv.polish_rounds = 0
        with self.assertRaisesRegex(drive.DriveError, "polish_rounds is 0"):
            drv.repolish()

    def test_repolish_refuses_at_once_while_a_run_holds_the_lock_and_changes_nothing(self):
        repo = self.make_repo()
        drv = self.driver(repo)
        drv.st["polish"] = {"round": 2, "status": "done", "history": [{"round": 1}], "repolishes": 0}
        lock = drive.lock_path(repo)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(f"drive.py pid {os.getpid()} since now\n", encoding="utf-8")  # a live holder
        self.addCleanup(lock.unlink, missing_ok=True)
        with self.assertRaisesRegex(drive.DriveError, "is held by drive.py pid"):
            drv.repolish()
        self.assertEqual(drv.st["polish"]["status"], "done")
        self.assertFalse((repo / ".overhaul" / "state.json").is_file()
                         and "repolishes\": 1" in (repo / ".overhaul" / "state.json").read_text(encoding="utf-8"))


class Deferred(Base):
    def test_a_deferred_row_needs_no_files_stubs_or_tests(self):
        later = row("F004", phase="2", module="report", deps="F001", entry="`tiny later`", acc="", status="deferred",
                    files="src/nowhere.py")
        notes = []
        problems, n = pv.validate(self.make(table(R1, R2, r3(), later), cycles=OK, entry_lint=OK), notes=notes)
        self.assertEqual((problems, n), ([], 3))
        self.assertTrue(any("1 deferred" in x for x in notes), notes)

    def test_a_deferred_row_still_checks_id_module_deps_and_phase(self):
        bad = row("F004", phase="1", module="nope", deps="F003", acc="", status="deferred")
        p = self.problems(self.make(table(R1, R2, r3(), bad), cycles=FAST, entry_lint=FAST))
        self.assertTrue(self.has(p, "F004", "not in ARCHITECTURE"), p)
        p = self.problems(self.make(table(R1, R2, r3(), row("F004", phase="1", deps="F003", acc="",
                                                            status="deferred")), cycles=FAST, entry_lint=FAST))
        self.assertTrue(self.has(p, "F004", "dep F003 is in phase 2"), p)

    def test_a_planned_row_on_a_deferred_dep_is_flagged(self):
        r2 = R2.replace("| planned |", "| deferred |")
        p = self.problems(self.make(table(R1, r2, r3()), cycles=FAST, entry_lint=FAST))
        self.assertTrue(self.has(p, "F003", "dep F002 is deferred"), p)

    def test_deferred_is_a_status_and_a_typo_is_not(self):
        self.assertIn("deferred", pv.STATUSES)
        p = self.problems(self.make(table(R1, R2, r3(status="defered")), cycles=FAST, entry_lint=FAST))
        self.assertTrue(self.has(p, "malformed status"), p)


class EveryTable(Base):
    HEADER = ("| id | phase | module | deps | entry | acceptance | status | files | needs |\n"
              "|---|---|---|---|---|---|---|---|---|\n")

    def test_one_table_per_phase_reads_every_row(self):
        text = ("# Functions\n\n## Phase 1\n\n" + self.HEADER + R1 + "\n" + R2 + "\n\nSome prose.\n\n## Phase 2\n\n"
                + self.HEADER + r3() + "\n")
        self.assertEqual(pv.validate(self.make(text, cycles=OK, entry_lint=OK)), ([], 3))
        rows = pv.parse_functions(text, [], "F")
        self.assertEqual([pv.bare(r["id"]) for r in rows], ["F001", "F002", "F003"])

    def test_a_dep_across_tables_and_a_duplicate_across_tables(self):
        text = "# Functions\n\n" + self.HEADER + R1 + "\n\n" + self.HEADER + R2 + "\n" + r3() + "\n"
        self.assertEqual(self.problems(self.make(text, cycles=FAST, entry_lint=FAST)), [])
        dup = "# Functions\n\n" + self.HEADER + R1 + "\n" + R2 + "\n\n" + self.HEADER + r3() + "\n" + R1 + "\n"
        p = self.problems(self.make(dup, cycles=FAST, entry_lint=FAST))
        self.assertTrue(self.has(p, "F001", "duplicate"), p)

    def test_tables_without_rows_and_a_file_without_a_table(self):
        problems = []
        self.assertEqual(pv.parse_functions("# F\n\n" + self.HEADER + "\n" + self.HEADER, problems, "F"), [])
        self.assertTrue(any("no function rows" in x for x in problems), problems)
        problems = []
        self.assertEqual(pv.parse_functions("# F\n\nno table here\n", problems, "F"), [])
        self.assertTrue(any("no table with the header" in x for x in problems), problems)

    def test_a_row_shape_problem_names_the_right_table_line(self):
        text = "# Functions\n\n" + self.HEADER + R1 + "\n\n" + self.HEADER + R2 + "\n| F009 | 2 | report |\n"
        problems = []
        pv.parse_functions(text, problems, "F")
        bad = [x for x in problems if "cells" in x]
        self.assertEqual(len(bad), 1, problems)
        self.assertIn("F:10", bad[0])


if __name__ == "__main__":
    unittest.main()
