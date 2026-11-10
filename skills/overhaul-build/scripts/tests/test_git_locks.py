"""A killed run's stale .git lock files are cleared at drive start; a fresh one is left alone."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import drive  # noqa: E402


class GitLocks(unittest.TestCase):
    def test_stale_lock_cleared_fresh_lock_kept(self):
        with tempfile.TemporaryDirectory() as d:
            g = Path(d) / ".git"
            g.mkdir()
            old, new = g / "packed-refs.lock", g / "index.lock"
            old.write_text("")
            new.write_text("")
            past = time.time() - 600
            os.utime(old, (past, past))
            drv = mock.Mock(repo=Path(d))
            drive.Driver.clear_git_locks(drv)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            drv.log.assert_called_once_with(None, "git_lock_cleared", "packed-refs.lock")


if __name__ == "__main__":
    unittest.main()
