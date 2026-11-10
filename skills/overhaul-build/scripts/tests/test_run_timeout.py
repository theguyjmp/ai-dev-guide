"""drive.run kills the whole tree on timeout and returns promptly even when a grandchild holds the pipes."""
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import drive  # noqa: E402

GRANDCHILD = ("import subprocess, sys, time; "
              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)")


class RunTimeout(unittest.TestCase):
    def test_timeout_with_a_grandchild_returns_promptly(self):
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            drive.run([sys.executable, "-c", GRANDCHILD], Path("."), timeout=2)
        self.assertLess(time.monotonic() - t0, 30)

    def test_normal_run_returns_output(self):
        r = drive.run([sys.executable, "-c", "print('ok')"], Path("."), timeout=60)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"))


if __name__ == "__main__":
    unittest.main()
