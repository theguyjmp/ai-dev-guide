"""Pass C (2026-11-10): builders outlive drive, and the disk never reaches the floor.

the pilot project 2026-11-09: 73 of 248 builds were killed by drive restarts, and the 40 GB floor blocked every local build.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import drive  # noqa: E402
import drivelock  # noqa: E402
import worker  # noqa: E402
from test_drive import DRIVE, Toy, pid_alive, put, wait_dead  # noqa: E402


class LauncherTest(unittest.TestCase):
    def test_launcher_runs_cmd_writes_its_rc_and_exits_with_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = Path(tmp) / "a.rc", Path(tmp) / "a.out"
            cmd = worker.launch_cmd([sys.executable, "-c", "print('hi'); raise SystemExit(5)"], 12, rc)
            with open(out, "wb") as fo:
                code = subprocess.call(cmd, stdout=fo, stdin=subprocess.DEVNULL)
            self.assertEqual(code, 5)
            self.assertEqual(rc.read_text(), "5")
            self.assertEqual(out.read_text().strip(), "hi")  # the cap note never lands in the worker's stream

    def test_bad_usage_is_2(self):
        self.assertEqual(worker.main(["--mem", "1"]), 2)


class AdoptedTest(unittest.TestCase):
    def test_poll_is_none_while_live_then_the_launchers_rc(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = Path(tmp) / "x.rc"
            table = {4242: "77"}
            with mock.patch.object(drivelock, "proc_start", lambda pid: table.get(pid)):
                a = drivelock.Adopted({"pid": 4242, "start": "77", "rc": str(rc)})
                self.assertIsNone(a.poll())
                rc.write_text("0")
                table.clear()
                self.assertEqual(a.poll(), 0)
                b = drivelock.Adopted({"pid": 4243, "start": "1", "rc": str(Path(tmp) / "none.rc")})
                self.assertEqual(b.poll(), -1)  # killed before the launcher wrote one

    def test_a_reused_pid_is_not_the_job(self):
        with mock.patch.object(drivelock, "proc_start", lambda pid: "other"):
            self.assertIsNotNone(drivelock.Adopted({"pid": 5, "start": "77"}).poll())

    def test_kill_orphans_keeps_adopted_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = str(Path(tmp).resolve())
            procs = [(301, f"cargo build --target-dir {root}/.overhaul/target/slot-0"),  # under adopted 300
                     (401, f"cargo build --target-dir {root}/.overhaul/target/slot-1")]  # a dead drive's
            killed = []
            with mock.patch.object(drivelock, "processes", return_value=procs), \
                    mock.patch.object(drivelock, "parents", return_value={300: 1, 301: 300, 401: 1}), \
                    mock.patch.object(drivelock, "kill_pid", side_effect=lambda pid: killed.append(pid) or True):
                drivelock.kill_orphans(Path(tmp), adopted={300})
            self.assertEqual(killed, [401])


class DiskTest(Toy):
    def driver(self, repo: Path) -> drive.Driver:
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        return drv

    def test_prunes_at_prune_at_gb_and_still_builds(self):
        repo = self.make_repo()
        dirs = []
        for i, sub in enumerate(("slot-0/debug/incremental/a", "slot-1/release/incremental/b")):
            d = repo / ".overhaul" / "target" / sub
            put(d / "x.bin", "x")
            os.utime(d, (1000 + i, 1000 + i))
            dirs.append(d)
        drv = self.driver(repo)
        drv.free_gb = lambda: 50.0 + 20.0 * sum(not d.exists() for d in dirs)  # above the 40 floor already
        self.assertTrue(drv.disk_guard("cycle"))
        self.assertEqual([d.exists() for d in dirs], [False, False])  # 50 -> 90: pruned to above 80
        self.assertIn("disk_pruned", [e["event"] for e in self.events(repo)])
        self.assertNotIn("disk_low", drv.st)

    def test_a_finished_jobs_slot_loses_its_incremental_dirs(self):
        repo = self.make_repo()
        drv = self.driver(repo)
        inc = repo / ".overhaul" / "target" / "slot-3" / "debug" / "incremental" / "q"
        deps = repo / ".overhaul" / "target" / "slot-3" / "debug" / "deps"
        put(inc / "x.bin", "x")
        put(deps / "y.rlib", "y")
        drivelock.write_job(drv.ov, "F1", {"pid": 1, "start": ""})
        drv.slots["F1"] = 3
        drv.job_ended(SimpleNamespace(fn="F1", host=""))
        self.assertFalse(inc.parent.exists())
        self.assertTrue(deps.exists())  # deps stay: no cold rebuild
        self.assertFalse((drv.ov / "jobs" / "F1.json").exists())


LONG = 'if mode.startswith("sleep"):\n    time.sleep(float(mode[5:]))\n    mode = "ok"\n'


@unittest.skipUnless(os.name == "nt", "Job Objects are Windows only")
class RestartTest(Toy):
    """A fake long builder survives a drive restart and is reaped by the new drive."""

    def start_drive(self, repo: Path) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, str(DRIVE), "--repo", str(repo)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                errors="replace")

    def test_builder_survives_a_drive_crash_and_the_next_drive_adopts_it(self):
        repo = self.make_repo({"F1": {"build": ["sleep12"]}}, max_parallel=1)
        fw = self.root / "fake_worker.py"
        fw.write_text(fw.read_text(encoding="utf-8").replace('if mode == "slow":', LONG + 'if mode == "slow":'),
                      encoding="utf-8")
        first = self.start_drive(repo)
        self.addCleanup(lambda: first.poll() is None and first.kill())
        pidfile = repo / ".overhaul" / "jobs" / "F1.json"
        end = time.monotonic() + 60
        while not pidfile.is_file() and time.monotonic() < end:
            time.sleep(0.2)
        self.assertTrue(pidfile.is_file(), "no pid file for the detached build")
        builder = json.loads(pidfile.read_text())["pid"]
        self.addCleanup(lambda: drivelock.kill_pid(builder))
        time.sleep(1.0)
        # a crash: drive dies by pid alone; its kill-on-close job would take every attached child with it
        subprocess.run(["taskkill", "/PID", str(first.pid), "/F"], capture_output=True)
        first.wait(30)
        self.assertFalse(wait_dead(builder, 2.0), "the detached builder died with its drive")
        self.assertTrue(pid_alive(builder))
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = self.events(repo)
        adopted = [e for e in ev if e["event"] == "build_adopted" and e.get("fn") == "F1"]
        self.assertEqual(len(adopted), 1, out)
        starts = [e for e in ev if e["event"] == "build_start" and e.get("fn") == "F1"]
        self.assertEqual(len(starts), 1, "the adopted build was started again")
        self.assertEqual(self.state(repo)["functions"]["F1"]["status"], "proven")
        self.assertFalse(pidfile.exists())

    def test_owner_stop_kills_detached_builders(self):
        repo = self.make_repo({"F1": {"build": ["sleep60"]}}, max_parallel=1)
        fw = self.root / "fake_worker.py"
        fw.write_text(fw.read_text(encoding="utf-8").replace('if mode == "slow":', LONG + 'if mode == "slow":'),
                      encoding="utf-8")
        first = self.start_drive(repo)
        self.addCleanup(lambda: first.poll() is None and first.kill())
        pidfile = repo / ".overhaul" / "jobs" / "F1.json"
        end = time.monotonic() + 60
        while not pidfile.is_file() and time.monotonic() < end:
            time.sleep(0.2)
        builder = json.loads(pidfile.read_text())["pid"]
        self.addCleanup(lambda: drivelock.kill_pid(builder))
        rc, out = self.drive_cli(repo, "--stop")
        self.assertEqual(rc, 0, out)
        self.assertIn("killed 1 detached builder(s): F1", out)
        first.wait(30)
        self.assertTrue(wait_dead(builder, 15), "the owner's stop left the builder running")


if __name__ == "__main__":
    unittest.main()
