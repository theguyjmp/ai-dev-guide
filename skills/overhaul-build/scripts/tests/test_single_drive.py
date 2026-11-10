"""One drive per repo, no orphans, one job per slot (drivelock.py; measured 2026-11-09 on the pilot project, R0422)."""
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
import drive  # noqa: E402
import drivelock  # noqa: E402

ME = os.getpid()


class FakeTable:
    """pid -> start time: the live process table the lock is checked against."""

    def __init__(self, procs):
        self.procs = dict(procs)

    def __call__(self, pid):
        return self.procs.get(pid)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        os.environ.pop(drivelock.ENV, None)
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)

    def table(self, procs):
        p = mock.patch.object(drivelock, "proc_start", FakeTable(procs))
        p.start()
        self.addCleanup(p.stop)

    def write_lock(self, pid, start, owner="drive.py"):
        path = drivelock.lock_path(self.repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": pid, "start": start, "owner": owner, "since": "2026-11-09T10:00:00"}))


class LockTest(Tmp):
    def test_first_start_takes_the_lock_with_pid_and_start(self):
        self.table({ME: "111"})
        self.assertEqual(drivelock.acquire(self.repo, "drive.py"), "taken")
        rec = json.loads(drivelock.lock_path(self.repo).read_text())
        self.assertEqual((rec["pid"], rec["start"], rec["owner"]), (ME, "111", "drive.py"))
        self.assertEqual(os.environ[drivelock.ENV], f"{ME} 111")
        drivelock.release(self.repo)
        self.assertFalse(drivelock.lock_path(self.repo).exists())

    def test_double_start_exits_naming_the_running_pid(self):
        self.table({ME: "111", 4242: "999"})
        self.write_lock(4242, "999", "supervise.py")
        with self.assertRaises(drivelock.Held) as cm:
            drivelock.acquire(self.repo, "drive.py")
        self.assertIn("supervise.py pid 4242", str(cm.exception))
        self.assertEqual(json.loads(drivelock.lock_path(self.repo).read_text())["pid"], 4242)  # untouched

    def test_stale_lock_of_a_dead_pid_is_taken_over(self):
        self.table({ME: "111"})
        self.write_lock(4242, "999")
        self.assertEqual(drivelock.acquire(self.repo, "drive.py"), "taken")
        self.assertEqual(json.loads(drivelock.lock_path(self.repo).read_text())["pid"], ME)
        self.assertEqual(list(drivelock.lock_path(self.repo).parent.glob("drive.lock.stale-*")), [])

    def test_reused_pid_with_another_start_time_is_stale(self):
        self.table({ME: "111", 4242: "555"})  # 4242 runs, but it is not the process that took the lock
        self.write_lock(4242, "999")
        self.assertEqual(drivelock.acquire(self.repo, "drive.py"), "taken")

    def test_drive_started_by_the_holder_runs_under_its_lock(self):
        self.table({ME: "111", 4242: "999"})
        self.write_lock(4242, "999", "supervise.py")
        os.environ[drivelock.ENV] = "4242 999"
        self.assertEqual(drivelock.acquire(self.repo, "drive.py"), "inherited")

    def test_release_never_removes_another_holders_lock(self):
        self.table({ME: "111", 4242: "999"})
        self.write_lock(4242, "999")
        drivelock.release(self.repo)
        self.assertTrue(drivelock.lock_path(self.repo).exists())

    def test_admin_refusal_names_the_pid_and_the_request_file_when_live_replan_is_in(self):
        self.table({ME: "111", 4242: "999"})
        self.write_lock(4242, "999")
        msg = drivelock.refuse_admin(self.repo, "--reset", live_replan=True)
        self.assertIn("pid 4242", msg)
        self.assertIn(".overhaul/requests/reset-<id>", msg)
        msg = drivelock.refuse_admin(self.repo, "--mark-proven", live_replan=True)
        self.assertIn("stop it (pid 4242", msg)
        with mock.patch.object(drivelock, "acquire", side_effect=drivelock.Held("held")):
            with self.assertRaises(drive.DriveError) as cm:
                drive.take_drive_lock(self.repo, "--reset")
        self.assertIn("--reset refused", str(cm.exception))


class DoubleStartTest(Tmp):
    """Real processes: a lock held by this live test process refuses drive.py, --reset and supervise.py at once."""

    def run_script(self, name, *args):
        env = {k: v for k, v in os.environ.items() if k != drivelock.ENV}
        return subprocess.run([sys.executable, str(SCRIPTS / name), "--repo", str(self.repo), *args],
                              capture_output=True, text=True, timeout=120, env=env)

    def test_second_start_exits_at_once(self):
        self.write_lock(ME, drivelock.proc_start(ME) or "", "supervise.py")
        for name, args, want in (("drive.py", (), "one drive per repo"),
                                 ("drive.py", ("--reset", "F01"), "--reset refused"),
                                 ("supervise.py", (), "one drive per repo")):
            t0 = time.monotonic()
            p = self.run_script(name, *args)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn(want, p.stdout)
            self.assertIn(f"pid {ME}", p.stdout)
            self.assertLess(time.monotonic() - t0, 60)
        self.assertEqual(json.loads(drivelock.lock_path(self.repo).read_text())["pid"], ME)

    def test_dead_holder_does_not_block_a_start(self):
        self.write_lock(2 ** 22 + 7, "1")  # no such pid
        p = self.run_script("drive.py")
        self.assertNotIn("one drive per repo", p.stdout)
        self.assertIn("no overhaul.toml", p.stdout)  # it got past the lock
        self.assertFalse(drivelock.lock_path(self.repo).exists())  # and released it on exit


class OrphanTest(Tmp):
    def test_kills_only_this_repos_worktree_and_target_processes(self):
        root = str(self.repo.resolve())
        procs = [
            (101, f"claude -p Read and do {root}\\.overhaul\\wt\\F01\\brief.md"),
            (102, f"{root.upper()}/.overhaul/target/slot-3/debug/app.exe --check"),
            (103, f"rustc --out-dir {root}/.overhaul/target/land-0/debug/deps"),
            (104, f"cargo test --manifest-path {root}-rebuild/.overhaul/wt/F01/Cargo.toml"),  # another repo
            (105, f"py -3 drive.py --repo {root}"),  # names the repo, not wt/target
            (106, f"notepad {root}/.overhaul/wtx/notes.txt"),
            (ME, f"python {root}/.overhaul/wt/F01/x.py"),  # never itself
        ]
        killed, logged = [], []
        with mock.patch.object(drivelock, "processes", return_value=procs), \
                mock.patch.object(drivelock, "kill_pid", side_effect=lambda pid: killed.append(pid) or True):
            out = drivelock.kill_orphans(self.repo, lambda pid, cmd: logged.append(pid))
        self.assertEqual(sorted(out), [101, 102, 103])
        self.assertEqual(sorted(killed), [101, 102, 103])
        self.assertEqual(sorted(logged), [101, 102, 103])

    def test_single_start_logs_orphan_killed(self):
        drv = bare_driver(self.repo)
        with mock.patch.object(drivelock, "processes", return_value=[(201, f"{self.repo}/.overhaul/wt/F02/a")]), \
                mock.patch.object(drivelock, "kill_pid", return_value=True):
            drv.single_start()
        events = [json.loads(ln) for ln in (drv.ov / "log.jsonl").read_text().splitlines()]
        self.assertEqual([e["event"] for e in events], ["orphan_killed"])
        self.assertIn("pid 201", events[0]["detail"])

    @unittest.skipUnless(os.name == "nt", "Job Objects are Windows only")
    def test_killing_drive_kills_its_children(self):
        pidfile = self.repo / "child.pid"
        prog = ("import subprocess, sys, time; sys.path.insert(0, sys.argv[1]); import memcap;"
                "print(memcap.cap_self(12, kill_on_close=True), flush=True);"
                "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
                "open(sys.argv[2], 'w').write(str(c.pid)); time.sleep(120)")
        parent = subprocess.Popen([sys.executable, "-c", prog, str(SCRIPTS), str(pidfile)],
                                  stdout=subprocess.PIPE, text=True)
        self.assertIn("the tree dies with this process", parent.stdout.readline())
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
        child = int(pidfile.read_text())
        self.assertIsNotNone(drivelock.proc_start(child))
        subprocess.run(["taskkill", "/PID", str(parent.pid), "/F"], capture_output=True)  # no /T: the job does it
        parent.wait(30)
        for _ in range(50):
            if drivelock.proc_start(child) is None:
                break
            time.sleep(0.1)
        self.assertIsNone(drivelock.proc_start(child), "a child outlived its killed drive")


def bare_driver(repo: Path, **extra):
    drv = drive.Driver.__new__(drive.Driver)
    drv.repo, drv.ov, drv.main = repo, repo / ".overhaul", "main"
    drv.ov.mkdir(parents=True, exist_ok=True)
    drv.jobs, drv.slots, drv.st = {}, {}, {"functions": {}}
    for k, v in extra.items():
        setattr(drv, k, v)
    return drv


class SlotTest(Tmp):
    def setUp(self):
        super().setUp()
        self.table({ME: "111", 4242: "999"})

    def test_slot_assignment_is_persisted_and_marked(self):
        drv = bare_driver(self.repo, drive_id=f"{ME} 111")
        drv.jobs = {"F01": None, "F02": None}
        self.assertEqual((drv.slot("F01"), drv.slot("F02")), (0, 1))
        self.assertEqual(drv.st["slots"], {"F01": 0, "F02": 1})
        self.assertEqual(drivelock.read_marker(drv.slot_dir(1))["fid"], "F02")

    def test_a_slot_held_by_another_live_drive_is_never_handed_out(self):
        drivelock.claim(self.repo / ".overhaul" / "target" / "slot-0", "4242 999", "F09")
        drv = bare_driver(self.repo, drive_id=f"{ME} 111")
        drv.jobs = {"F01": None}
        self.assertEqual(drv.slot("F01"), 1)

    def test_start_check_drops_a_slot_given_twice_or_held_by_a_live_drive(self):
        target = self.repo / ".overhaul" / "target"
        drivelock.claim(target / "slot-2", "4242 999", "F09")
        drivelock.claim(target / "slot-1", "77 1", "F03")  # a dead drive: its slot is reusable
        kept, notes = drivelock.check_slots({"F01": 0, "F02": 0, "F03": 1, "F04": 2}, target, f"{ME} 111")
        self.assertEqual(kept, {"F01": 0, "F03": 1})
        self.assertEqual(len(notes), 2)
        drv = bare_driver(self.repo)
        drv.st["slots"] = {"F01": 0, "F02": 0}
        with mock.patch.object(drivelock, "processes", return_value=[]):
            drv.single_start()
        self.assertEqual((drv.slots, drv.st["slots"]), ({"F01": 0}, {"F01": 0}))


class AcceptanceSlotTest(Tmp):
    def setUp(self):
        super().setUp()
        self.table({ME: "111", 4242: "999"})
        self.drv = bare_driver(self.repo, drive_id=f"{ME} 111", cfg={
            "acceptance": "cargo test --test {file} {test}", "commands": {}, "harness": {}},
            fns={"F01": SimpleNamespace(acceptance=["tests/acceptance/a.rs::opens"])})
        self.drv.jobs = {"F01": None}
        self.wt = self.repo / ".overhaul" / "wt" / "F01"
        self.wt.mkdir(parents=True)
        self.calls = []

    def events(self):
        path = self.drv.ov / "log.jsonl"
        return [json.loads(ln)["event"] for ln in path.read_text().splitlines()] if path.exists() else []

    def sh_seq(self, *results):
        it = iter(results)

        def sh(cmd, wt, fid=""):
            self.drv.slot(fid)  # slot_env picks the slot, as the real sh does
            self.calls.append(cmd)
            step = next(it)
            return step() if callable(step) else step
        self.drv.sh = sh

    def test_slot_taken_during_the_run_is_a_rebuild_not_a_red(self):
        def red_after_steal():
            drivelock.claim(self.drv.slot_dir(0), "4242 999", "F07")  # another job built into F01's slot
            return SimpleNamespace(returncode=101, stdout="test opens ... FAILED", stderr="")
        self.sh_seq(red_after_steal, SimpleNamespace(returncode=0, stdout="ok", stderr=""))
        self.assertEqual(self.drv.acceptance_problem(self.wt, "F01"), "")
        self.assertEqual(len(self.calls), 2)
        self.assertIn("slot_rebuild", self.events())

    def test_binary_from_another_dir_is_a_rebuild_not_a_red(self):
        other = (self.repo / ".overhaul" / "target" / "slot-5" / "debug" / "deps" / "a-1.exe").as_posix()
        self.sh_seq(SimpleNamespace(returncode=101, stdout=f"     Running tests/acceptance/a.rs ({other})\nFAILED",
                                    stderr=""), SimpleNamespace(returncode=0, stdout="ok", stderr=""))
        self.assertEqual(self.drv.acceptance_problem(self.wt, "F01"), "")
        self.assertIn("slot_rebuild", self.events())

    def test_slot_last_built_by_another_job_rebuilds_before_the_test(self):
        self.drv.slots = {"F01": 0}
        drivelock.claim(self.drv.slot_dir(0), f"{ME} 111", "F02")
        self.drv.cfg["commands"]["build"] = "cargo build"
        self.sh_seq(SimpleNamespace(returncode=0, stdout="", stderr=""),
                    SimpleNamespace(returncode=0, stdout="ok", stderr=""))
        self.assertEqual(self.drv.acceptance_problem(self.wt, "F01"), "")
        self.assertEqual(self.calls[0], "cargo build")
        self.assertEqual(self.events(), ["slot_rebuild"])

    def test_own_red_stays_a_red(self):
        own = (self.drv.slot_dir(0) / "debug" / "deps" / "a-1.exe").as_posix()
        self.sh_seq(SimpleNamespace(returncode=101, stdout=f"     Running tests/acceptance/a.rs ({own})\nFAILED",
                                    stderr=""))
        self.assertIn("acceptance red", self.drv.acceptance_problem(self.wt, "F01"))
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("slot_rebuild", self.events())


if __name__ == "__main__":
    unittest.main()
