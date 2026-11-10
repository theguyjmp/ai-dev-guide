"""A systemic reason green on clean main does not stop the build (pilot2026-11-10 03:04: 57 min of no launches)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mainred  # noqa: E402

REASON = ("regression before review: proven R0814 tests/acceptance/r0814_ref_link.rs::test_r0814_live_reference "
          "(exit 101) is red in the worktree after merging main")


def fake(pending, test_ok=True, check_ok=True):
    d = mock.Mock(red_pending=pending, red_stop_reason="", main="main")
    d.main_test_ok.return_value = test_ok
    d.main_check.return_value = (check_ok, "out", "abc123def456")
    d.red_clear_systemic.side_effect = lambda key, why: mainred.MainRed.red_clear_systemic(d, key, why)
    return d


class SystemicClear(unittest.TestCase):
    def test_a_proven_test_green_on_main_is_cleared_not_stopped(self):
        d = fake(("key", "", REASON))
        self.assertTrue(mainred.MainRed.red_step(d))
        d.main_test_ok.assert_called_once_with("tests/acceptance/r0814_ref_link.rs::test_r0814_live_reference")
        d.red_stop.assert_not_called()
        self.assertEqual(d.log.call_args.args[1], "systemic_clear")

    def test_a_proven_test_red_on_main_stops(self):
        d = fake(("key", "", REASON), test_ok=False)
        mainred.MainRed.red_step(d)
        d.red_stop.assert_called_once()

    def test_a_whole_tree_check_green_on_main_still_stops(self):
        d = fake(("key", "lint", "lint failed"))
        mainred.MainRed.red_step(d)
        d.red_stop.assert_called_once()
        d.red_set.assert_not_called()

    def test_a_check_red_on_main_sets_main_red(self):
        d = fake(("key", "lint", "lint failed"), check_ok=False)
        mainred.MainRed.red_step(d)
        d.red_set.assert_called_once()

    def test_no_test_named_stops_as_before(self):
        d = fake(("key", "", "builder left no committed change"))
        mainred.MainRed.red_step(d)
        d.red_stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
