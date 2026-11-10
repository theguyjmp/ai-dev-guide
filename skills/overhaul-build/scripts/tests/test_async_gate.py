"""The pre-review gate runs off the main loop (pilot2026-11-10: serial ~3.5 min gates starved reviews and lands)."""
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import drive  # noqa: E402


def fake(sync):
    return mock.Mock(gating={}, gate_pool=None, gate_parallel=2, sync_problem=sync)


class AsyncGate(unittest.TestCase):
    def test_a_slow_gate_does_not_block_and_its_result_goes_to_review(self):
        d = fake(lambda fid: (time.sleep(0.5), "")[1])
        job = SimpleNamespace(fn="F1")
        t0 = time.monotonic()
        drive.Driver.gate_async(d, job)
        self.assertLess(time.monotonic() - t0, 0.3)
        self.assertIn("F1", d.gating)
        self.assertFalse(drive.Driver.reap_gates(d))
        d.to_review.assert_not_called()
        d.gating["F1"][1].result(timeout=5)
        self.assertTrue(drive.Driver.reap_gates(d))
        d.to_review.assert_called_once_with(job, "")
        self.assertEqual(d.gating, {})

    def test_gates_run_in_parallel(self):
        d = fake(lambda fid: (time.sleep(0.6), f"red {fid}")[1])
        t0 = time.monotonic()
        for fid in ("F1", "F2"):
            drive.Driver.gate_async(d, SimpleNamespace(fn=fid))
        for _, fut in list(d.gating.values()):
            fut.result(timeout=5)
        self.assertLess(time.monotonic() - t0, 1.1)
        drive.Driver.reap_gates(d)
        self.assertEqual(sorted(c.args[1] for c in d.to_review.call_args_list), ["red F1", "red F2"])

    def test_a_crashed_gate_is_a_failed_gate(self):
        def boom(fid):
            raise OSError("disk gone")
        d = fake(boom)
        job = SimpleNamespace(fn="F1")
        drive.Driver.gate_async(d, job)
        d.gating["F1"][1].exception(timeout=5)
        drive.Driver.reap_gates(d)
        self.assertIn("pre-review gate crashed: OSError: disk gone", d.to_review.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
