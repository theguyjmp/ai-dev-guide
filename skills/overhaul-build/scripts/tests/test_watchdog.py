"""watchdog.py: the 5-minute invariants; each breach writes a `watchdog` event and acts or alerts."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
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
        self.now += 2000
        self.assertEqual(self.dog.check(), [])  # cleared

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
