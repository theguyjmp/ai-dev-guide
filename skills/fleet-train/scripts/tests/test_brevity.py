"""The brevity mechanisms: `harness.py note`, proven-row pruning, held rows and brevity_check.py. Temp folders only."""
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="fleet-brevity-"))
os.environ.update({"HARNESS_HOME": str(TMP / "home"), "HARNESS_REPO": str(TMP / "repo"), "HARNESS_WT": str(TMP / "wt")})
sys.path.insert(0, str(HERE))
import brevity_check as bc  # noqa: E402
import harness as h  # noqa: E402

FUNCTIONS = """# Functions

| id | phase | module | deps | entry | files | status | tests |
|---|---|---|---|---|---|---|---|
| F01 | 1 | a | - | cli:x | a.rs | planned | t::a |
| F02 | 1 | a | - | cli:y | a.rs | planned | t::b |

- F01: `x` prints the thing
  and keeps going on this line.
- F02: `y` stays.
"""
PACKETS = "# Phase 1\n\n- `F01` [F01] Packet: build x.\n  more of F01.\n- `F02` [F02] Packet: build y.\n"


class Note(unittest.TestCase):
    def test_appends_dated_lines_and_refuses_bad_notes(self):
        mock.patch.object(h, "LOG_DIR", TMP / "log").start()
        self.addCleanup(mock.patch.stopall)
        at = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        p = h.append_note("  first\n fact ", at)
        h.append_note("second", at)
        self.assertEqual(p.name, "2026-01.md")
        lines = p.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[-2:], ["- 2026-01-02 03:04Z first fact", "- 2026-01-02 03:04Z second"])
        for bad in ("", "x" * (h.NOTE_MAX_CHARS + 1)):
            with self.assertRaises(ValueError):
                h.append_note(bad, at)


class Prune(unittest.TestCase):
    def test_proven_rows_lose_bullet_and_packet(self):
        mock.patch.object(h, "TRAIN", TMP / "train").start()
        self.addCleanup(mock.patch.stopall)
        (h.TRAIN / "spec" / "packets").mkdir(parents=True)
        (h.TRAIN / h.SPEC).write_text(FUNCTIONS, encoding="utf-8")
        (h.TRAIN / h.PACKETS / "phase-1.md").write_text(PACKETS, encoding="utf-8")
        with mock.patch.object(h, "sh"), mock.patch.object(h, "git"):  # the index refresh and the commit
            h.finish_spec(["F01"])
        spec = (h.TRAIN / h.SPEC).read_text(encoding="utf-8")
        self.assertRegex(spec, r"\| F01 \|.*\| proven \|")
        self.assertNotIn("F01: ", spec)
        self.assertNotIn("keeps going", spec)
        self.assertIn("- F02: `y` stays.", spec)
        self.assertEqual((h.TRAIN / h.PACKETS / "phase-1.md").read_text(encoding="utf-8"),
                         "# Phase 1\n\n- `F02` [F02] Packet: build y.\n")


class Held(unittest.TestCase):
    def test_held_rows_never_reach_the_frontier(self):
        rows = {i: {"id": i, "status": "planned", "deps": [], "module": "a", "file_list": []} for i in ("F01", "F02")}
        h.ST.mkdir(parents=True, exist_ok=True)
        (h.ST / "held-rows.txt").write_text("F01\n", encoding="utf-8")
        try:
            self.assertEqual(h.frontier(rows, {}, [], {}), ["F02"])
        finally:
            (h.ST / "held-rows.txt").unlink()


class Brevity(unittest.TestCase):
    def home(self):
        home = Path(tempfile.mkdtemp(dir=TMP))
        (home / "notes" / "briefs").mkdir(parents=True)
        (home / "notes" / "handbacks").mkdir()
        return home

    def test_caps_and_copies(self):
        home = self.home()
        (home / "STATE.md").write_text("- 2026-01-02 a dated line\n" + "word " * 900, encoding="utf-8")
        (home / "notes" / "briefs" / "b.md").write_text("Read big.md first.\n", encoding="utf-8")
        (home / "big.md").write_text("line\n" * 400, encoding="utf-8")
        (home / "notes" / "handbacks" / "x.md").write_text("w " * 40, encoding="utf-8")
        (home / "harness.py.bak").write_text("", encoding="utf-8")
        found = bc.breaches(home, home)
        for want in ("STATE.md words", "STATE.md dated-lines", "b.md->big.md lines", "x.md first-line-words",
                     "copies 1 > 0"):
            self.assertTrue(any(want in f for f in found), (want, found))

    def test_code_ratchet_only_falls(self):
        home = self.home()
        (home / "tool.py").write_text("# a comment of several words here\nx = 1\n", encoding="utf-8")
        self.assertTrue(any("tool.py comment-share" in f for f in bc.breaches(home, home)))
        share = bc.measure_ratchets(home)["tool.py"]["share"]
        (home / "brevity_budget.json").write_text(json.dumps({"code_ratchet": {"tool.py": {"share": share + 0.1,
                                                                                            "dated": 0}}}))
        self.assertEqual(bc.breaches(home, home), [])
        self.assertEqual(bc.tighten(home), 1)
        self.assertEqual(json.loads((home / "brevity_budget.json").read_text())["code_ratchet"]["tool.py"]["share"], share)

    def test_skill_scripts_hold_their_own_comment_budget(self):
        found = [f for f in bc.breaches(HERE, HERE) if re.search(r"comment-share|dated-comment", f)]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
