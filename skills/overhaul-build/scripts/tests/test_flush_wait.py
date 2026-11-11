"""flush() keeps waiting while a live land holds the lock (pilot 2026-11-10 02:15: a 120 s wait stopped the run)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import drive  # noqa: E402


class FlushWait(unittest.TestCase):
    def fake(self, results, land_timeout_s=1200.0):
        d = mock.Mock(land_timeout_s=land_timeout_s, state_path=Path("state.json"), repo=Path("."))
        d.save.side_effect = results
        return d

    def test_a_long_held_lock_is_waited_out(self):
        d = self.fake([False, False, True])
        with mock.patch.object(drive, "lock_holder", return_value="land.py pid 1"):
            drive.Driver.flush(d)
        self.assertEqual(d.save.call_count, 3)
        self.assertEqual([c.args[1] for c in d.log.call_args_list], ["state_lock_wait"] * 2)

    def test_past_the_deadline_it_still_errors(self):
        d = self.fake([False] * 5, land_timeout_s=0.0)
        clock = iter([0.0, 10_000.0, 20_000.0, 30_000.0])
        with mock.patch.object(drive, "lock_holder", return_value="land.py pid 1"), \
                mock.patch.object(drive.time, "monotonic", side_effect=lambda: next(clock)):
            with self.assertRaises(drive.DriveError):
                drive.Driver.flush(d)


if __name__ == "__main__":
    unittest.main()
