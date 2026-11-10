"""Pass G (owner 2026-11-10): drive honours the watchdog's requests, so what is due runs even when drive's own
schedule skipped it: audit-phase-<N> (the pilot project: 0 phase audits in two days, a finished final polish hid them),
arch-sweep (the every-25-lands sweep never ran) and periodic-<gate> (55 reds after the last bisect drew no action).

Run from the scripts dir:  py -3 -m unittest tests.test_harness_g
"""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import drive  # noqa: E402
import periodic  # noqa: E402
from test_phase_audit import Base  # noqa: E402


def ask(drv: drive.Driver, *names: str) -> bool:
    req = drv.ov / drive.REQUESTS_DIR
    req.mkdir(parents=True, exist_ok=True)
    for n in names:
        (req / n).write_text("watchdog\n", encoding="utf-8")
    drv.last_harness_request = 0.0
    return drv.harness_requests()


def logged(drv: drive.Driver, event: str) -> list[str]:
    lines = (drv.ov / "log.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(x)["detail"] for x in lines if json.loads(x)["event"] == event]


class Requests(Base):
    def test_audit_phase_runs_a_phase_audit_a_finished_final_polish_had_hidden(self):
        drv = self.driver()
        drv.st["functions"]["F1"]["status"] = "proven"
        drv.st["polish"] = {"round": 3, "status": "done", "issues": 0, "history": []}
        self.assertIsNone(drv.polish_phase())  # the bug: a done final polish hides every phase audit
        self.assertTrue(ask(drv, "audit-phase-1"))
        self.assertEqual(drv.polish_phase(), 1)
        self.assertEqual(logged(drv, "request_applied"), ["audit-phase-1"])
        self.assertFalse((drv.ov / drive.REQUESTS_DIR / "audit-phase-1").exists())
        with mock.patch.object(drv, "start_polish_job"), mock.patch.object(drv, "start_arch_audit"), \
                mock.patch.object(drv, "prepare_worktree", return_value=drv.repo):
            self.assertTrue(drv.start_polish_audit(1))
        self.assertEqual(logged(drv, "phase_audit"), ["phase 1 round 1 start"])
        drv.st["phase_polish"]["1"]["status"] = "done"
        self.assertIsNone(drv.polish_phase())  # ended: back to drive's own schedule
        self.assertTrue(ask(drv, "audit-phase-1"))  # once more over today's code
        self.assertEqual(drv.polish_phase(), 1)
        self.assertEqual(drv.st["phase_polish"]["1"]["audits"], 2)
        self.assertEqual(drv.st["phase_polish"]["1"]["status"], "idle")

    def test_bad_requests_are_refused_with_a_reason(self):
        drv = self.driver()
        self.assertFalse(ask(drv, "audit-phase-9", "periodic-nope"))
        refused = logged(drv, "request_refused")
        self.assertIn("audit-phase-9: no such phase in the plan", refused)
        self.assertIn("periodic-nope: no periodic gate nope", refused)
        self.assertFalse(list((drv.ov / drive.REQUESTS_DIR).iterdir()))

    def test_arch_sweep_runs_now_whatever_the_count_and_the_final_polish(self):
        drv = self.driver()
        drv.st["functions"]["F1"]["status"] = "proven"
        drv.st["phase_polish"] = {"1": {"round": 1, "status": "done", "history": []}}
        drv.st["polish"] = {"round": 3, "status": "done", "issues": 0, "history": []}
        with mock.patch.object(drv, "start_arch_audit") as start:
            self.assertFalse(drv.arch_step())  # 1 land, every 25, final polish done: never due on its own
            self.assertTrue(ask(drv, "arch-sweep"))
            self.assertTrue(drv.arch_step())
            self.assertEqual(start.call_args.args[0], drive.SWEEP_FID)
        self.assertNotIn("forced", drv.sweep_state())
        self.assertFalse(ask(drv, "arch-sweep"))  # already running
        self.assertIn("arch-sweep: a sweep is already audit", logged(drv, "request_refused"))


class Rearm(unittest.TestCase):
    def per(self) -> periodic.Periodic:
        drv = SimpleNamespace(st={"periodic": {"green": {}, "red": {"gpu": {"sha": "b" * 40, "reason": "exit 1: x",
                                                                            "culprit": "F7"}},
                                               "fixed": ["depfix-periodic-gpu"]}})
        return periodic.Periodic(drv, {"commands": {"gpu": "x"}})

    def test_a_red_gate_with_a_spent_depfix_bisects_again_at_most_twice(self):
        per = self.per()
        self.assertEqual(per.rearm("gpu"), "")
        self.assertEqual(per.st["red"]["gpu"]["culprit"], "")
        self.assertEqual(per.st["fixed"], [])
        per.st["red"]["gpu"]["culprit"] = "F7"
        self.assertEqual(per.rearm("gpu"), "")
        self.assertIn("re-armed 2 times already", per.rearm("gpu"))
        self.assertEqual(per.rearm("scale"), "scale is not red")


if __name__ == "__main__":
    unittest.main()
