"""watchdog.py: the 5-minute invariants; each breach writes a `watchdog` event and acts or alerts."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import remote  # noqa: E402
import watchdog  # noqa: E402

NOW = 1_800_000_000.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


class Dog(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="watchdog-")).resolve()
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.ov = self.repo / ".overhaul"
        self.ov.mkdir()
        self.now, self.free = NOW, 500.0
        self.dog = watchdog.Watchdog(self.repo, free_gb=lambda: self.free, now=lambda: self.now)

    def log(self, *evs: tuple[float, str, str]) -> None:
        with open(self.ov / "log.jsonl", "a", encoding="utf-8") as f:
            for ts, event, detail in evs:
                f.write(json.dumps({"t": iso(ts), "fn": None, "event": event, "detail": detail}) + "\n")

    def state(self, **st) -> None:
        (self.ov / "state.json").write_text(json.dumps(st), encoding="utf-8")

    def watchdog_events(self) -> list[str]:
        lines = (self.ov / "log.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(x)["detail"] for x in lines if json.loads(x)["event"] == "watchdog"]

    def alerts(self) -> str:
        path = self.ov / "ALERT.txt"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_a_quiet_healthy_build_fires_nothing(self):
        self.log((NOW - 60, "build_start", "x"), (NOW - 120, "land_ok", "main -> x"))
        self.state(width={"ready": 0, "free": 2}, functions={"A": {"status": "landing"}})
        self.assertEqual(self.dog.check(), [])
        self.assertEqual(self.alerts(), "")

    def test_a_module_audit_locked_over_20_min_alerts_and_requests_a_release(self):
        self.log((NOW - 60, "build_start", "x"))
        self.state(audit_locks={"geom": NOW - 10 * 60, "mesh": NOW - 5 * 60})
        self.assertEqual(self.dog.check(), [])  # 10 min: fine
        self.state(audit_locks={"geom": NOW - 25 * 60, "mesh": NOW - 5 * 60})
        self.assertEqual(self.dog.check(), ["audit_lock"])
        self.assertTrue((self.ov / "requests" / "release-audit-locks").is_file())
        self.assertIn("audit_lock: 1 module(s) audit-locked over 20 min (longest 25 min): geom; alert: requested "
                      "release-audit-locks", self.alerts())

    def test_ready_rows_beside_free_slots_for_10_min_request_a_reload_once(self):
        self.log((NOW - 3600, "build_start", "x"))
        self.state(width={"ready": 4, "free": 3})
        self.assertEqual(self.dog.check(), [])  # first seen now: not yet 10 min
        self.now += 601
        self.assertEqual(self.dog.check(), ["starved"])
        self.assertTrue(list((self.ov / "requests").glob("reload-watchdog-*")))
        self.assertIn("starved: 4 ready rows beside 3 free slots", self.alerts())
        self.now += 300
        self.assertEqual(self.dog.check(), [])  # persists: repeats at most every 30 min
        self.state(width={"ready": 0, "free": 3})
        for p in (self.ov / "requests").iterdir():
            p.unlink()  # drive applied the reload
        self.now += 2000
        self.assertEqual(self.dog.check(), [])  # cleared

    # ---- pass G: what should be happening is happening ----
    def plan(self, phases: dict[int, list[str]]) -> None:
        rows = [f"| {fid} | {n} | m{n} | - | `toy {fid}` | tests/acceptance/test_{fid}.py::t | planned |"
                for n, ids in phases.items() for fid in ids]
        (self.repo / "spec").mkdir(exist_ok=True)
        (self.repo / "spec" / "FUNCTIONS.md").write_text(
            "# Functions\n\n| id | phase | module | deps | entry | acceptance | status |\n"
            "|----|-------|--------|------|-------|------------|--------|\n" + "\n".join(rows) + "\n", encoding="utf-8")

    def test_a_phase_finished_20_min_ago_with_no_audit_gets_one_requested(self):
        self.plan({1: ["A1"], 2: ["B1"], 3: ["C1", "C2"], 4: ["D1"]})
        self.log((NOW - 3 * 3600, "land_ok", "main -> A1"), (NOW - 1500, "land_ok", "main -> C1"),
                 (NOW - 1200, "land_ok", "main -> C2"))
        with open(self.ov / "log.jsonl", "r+", encoding="utf-8") as f:  # land_ok events carry their fn
            lines = [json.loads(x) for x in f.read().splitlines()]
            for ev in lines:
                ev["fn"] = ev["detail"].split("-> ")[1]
            f.seek(0)
            f.truncate()
            f.write("".join(json.dumps(ev) + "\n" for ev in lines))
        proven = {"status": "proven"}
        self.state(functions={"A1": proven, "B1": proven, "C1": proven, "C2": proven, "D1": {"status": "planned"}},
                   phase_polish={"1": {"round": 1, "status": "done"}, "2": {"round": 2, "status": "fix"}})
        self.assertEqual(self.dog.check(), ["phase_audit"])
        self.assertTrue((self.ov / "requests" / "audit-phase-3").exists())
        self.assertFalse((self.ov / "requests" / "audit-phase-2").exists())  # running
        self.assertFalse((self.ov / "requests" / "audit-phase-4").exists())  # not finished
        self.assertIn("phase_audit: phase 3 all proven 20 min ago, no audit; alert: requested audit-phase-3",
                      self.watchdog_events()[0])
        self.assertIn("audit-phase-3", self.alerts())
        # drive applied it and the audit started: nothing more to do, even after the refire window
        (self.ov / "requests" / "audit-phase-3").unlink()
        self.log((NOW + 30, "request_applied", "audit-phase-3"), (NOW + 40, "phase_audit", "phase 3 round 1 start"))
        self.now += 31 * 60
        self.assertNotIn("phase_audit", self.dog.check())

    def test_a_phase_finished_10_min_ago_is_not_due_yet(self):
        self.plan({1: ["A1"], 2: ["B1"]})
        self.log((NOW - 600, "land_ok", "main -> A1"))
        self.state(functions={"A1": {"status": "proven"}, "B1": {"status": "planned"}})
        self.assertEqual(self.dog.check(), [])

    def test_an_overdue_arch_sweep_is_requested(self):
        recs = {f"R{i}": {"status": "proven" if i < 30 else "planned"} for i in range(40)}
        self.state(functions=recs, arch_sweep={"at": 0, "status": "idle", "history": []})
        self.assertEqual(self.dog.check(), [])  # due from now: 15 min to start on its own
        self.now += 16 * 60
        self.assertEqual(self.dog.check(), ["arch_sweep"])
        self.assertTrue((self.ov / "requests" / "arch-sweep").exists())
        self.assertIn("arch_sweep: 30 proven since the last sweep (at 0, every 25)", self.alerts())

    def test_a_red_gate_with_no_bisect_is_re_armed_and_a_broken_gate_goes_to_the_owner(self):
        self.log((NOW - 900, "periodic_red", "gpu run at abc exit 1: s14_first_sort over"),
                 (NOW - 900, "periodic_red", "linux run at abc exit 127: bash: C:\\x: No such file or directory"))
        self.state(periodic={"red": {"gpu": {"sha": "a", "reason": "exit 1: s14", "culprit": "F7"},
                                     "linux": {"sha": "a", "reason": "exit 127: bash: No such file or directory",
                                               "culprit": "a (no earlier green)"}}, "bisect": None})
        self.assertEqual(self.dog.check(), ["periodic"])
        self.assertTrue((self.ov / "requests" / "periodic-gpu").exists())
        self.assertFalse((self.ov / "requests" / "periodic-linux").exists())
        self.assertIn("owner: linux gate command broken", self.alerts())

    def test_a_red_gate_already_bisecting_is_fine(self):
        self.log((NOW - 900, "periodic_red", "gpu run at abc exit 1"), (NOW - 890, "periodic_start", "gpu bisect at d"))
        self.state(periodic={"red": {"gpu": {"sha": "a", "reason": "exit 1", "culprit": ""}}, "bisect": None})
        self.assertEqual(self.dog.check(), [])

    def test_waiting_requests_needs_plan_rows_and_a_stale_dashboard_alert(self):
        req = self.ov / "requests"
        req.mkdir()
        (req / "reset-R9").write_text("x", encoding="utf-8")
        self.state(functions={"R9": {"status": "needs-plan"}})
        page = self.repo / "dash" / "index.html"
        page.parent.mkdir()
        page.write_text("old", encoding="utf-8")
        os.utime(page, (NOW - 7200, NOW - 7200))
        self.log((NOW - 60, "build_start", "x"))
        with mock.patch.object(watchdog.subprocess, "run") as run:
            self.assertEqual(self.dog.check(), ["dashboard"])
            run.assert_called_once()
        self.assertIn("dashboard: dash/index.html 120 min old", self.alerts())  # dash.py wrote nothing
        self.now += 31 * 60
        self.log((self.now - 60, "build_start", "y"))
        with mock.patch.object(watchdog.subprocess, "run"):
            fired = self.dog.check()
        self.assertEqual(sorted(fired), ["dashboard", "needs_plan", "reload"])
        self.assertIn("reload: 1 plan requests waiting over 10 min (reset-R9); drive not running?", self.alerts())
        self.assertIn("needs_plan: 1 needs-plan rows (R9) and no plan round", self.alerts())

    def test_no_land_for_45_min_with_landing_rows_removes_a_stale_lock(self):
        self.log((NOW - 50 * 60, "land_ok", "main -> a"))
        self.state(functions={"R1": {"status": "landing"}, "R2": {"status": "proven"}})
        (self.ov / "lock").write_text("pid 999999 land.py since x", encoding="utf-8")
        self.assertEqual(self.dog.check(), ["no_land"])
        self.assertFalse((self.ov / "lock").exists())
        self.assertIn("no_land: no land_ok for 50 min with 1 landing (R1); removed a stale .overhaul/lock",
                      self.watchdog_events()[0])
        self.assertEqual(self.alerts(), "")  # acted: no alert needed

    def test_no_land_with_a_live_lock_holder_alerts(self):
        self.log((NOW - 50 * 60, "land_ok", "main -> a"))
        self.state(functions={"R1": {"status": "landing"}})
        (self.ov / "lock").write_text(f"pid {os.getpid()} land.py", encoding="utf-8")
        self.assertEqual(self.dog.check(), ["no_land"])
        self.assertTrue((self.ov / "lock").exists())
        self.assertIn("no_land: no land_ok for 50 min", self.alerts())

    def test_disk_under_prune_at_prunes_quiet_incremental_dirs_first(self):
        (self.repo / "overhaul.toml").write_text("[limits]\nprune_at_gb = 80\n", encoding="utf-8")
        old = self.ov / "target" / "slot-1" / "debug" / "incremental" / "a-1"
        busy = self.ov / "target" / "slot-2" / "debug" / "incremental" / "b-1"
        for d in (old, busy):
            d.mkdir(parents=True)
        os.utime(old, (NOW - 3600, NOW - 3600))
        os.utime(busy, (NOW - 60, NOW - 60))
        self.free = 70.0
        sizes = {old: 15.0}

        def free():
            return 70.0 + sum(v for d, v in sizes.items() if not d.exists())
        self.dog._free_gb = free
        self.assertEqual(self.dog.check(), ["disk"])
        self.assertFalse(old.exists())
        self.assertTrue(busy.exists())  # written to within 15 min: in use
        self.assertIn("disk: free 70.0 GB < prune_at_gb 80; pruned 1 incremental dirs, free 85.0 GB",
                      self.watchdog_events()[0])
        self.assertEqual(self.alerts(), "")

    def test_a_stop_event_alerts_once(self):
        self.dog.check()
        self.log((NOW + 10, "stop", "needs-plan: main red in plan stubs: dup a.rs"))
        self.now += 300
        self.assertEqual(self.dog.check(), ["stop"])
        self.assertIn("stop: drive stopped: needs-plan: main red in plan stubs", self.alerts())
        self.now += 300
        self.assertEqual(self.dog.check(), [])

    def test_a_host_offline_twice_in_30_min_is_held_offline_for_remote_py(self):
        self.log((NOW - 600, "remote_offline", "host=desk for 10 min: timed out with no builder output"),
                 (NOW - 60, "remote_offline", "host=desk for 10 min: timed out with no builder output"))
        self.assertEqual(self.dog.check(), ["host_offline"])
        self.assertIn("held offline by the watchdog", remote.held(self.repo, "desk", now=NOW + 60))
        self.assertEqual(remote.held(self.repo, "desk", now=NOW + 31 * 60), "")
        self.assertIn("host_offline: host desk offline 2 times in 30 min", self.alerts())

    def test_the_thread_checks_on_its_interval_and_stops(self):
        self.state(width={"ready": 0, "free": 0})
        logs: list[str] = []
        stop = watchdog.start(self.repo, logs.append, every=0.1)
        end = time.monotonic() + 10
        while not (self.ov / "watchdog.json").exists() and time.monotonic() < end:
            time.sleep(0.05)
        stop.set()
        self.assertTrue((self.ov / "watchdog.json").exists())


if __name__ == "__main__":
    unittest.main()
