"""[harness.remote]: builder slots on a second machine. A fake ssh (a local command) stands in for the host: it runs
the host program locally, and a bare clone stands in for the host repo."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests  # noqa: E402,F401  tests/__init__.py drops an inherited GIT_DIR & co (git hook, rebase -x, bisect run)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import remote  # noqa: E402
from test_drive import THREE, Toy, git, pid_alive, put, wait_dead  # noqa: E402

FAKE_SSH = '''
import subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
args = sys.argv[1:]
while args and args[0] == "-o":
    args = args[2:]
host, cmd = args[0], args[1:]
with open(ROOT / "ssh.log", "a", encoding="utf-8") as fh:
    fh.write(host + " " + " ".join(cmd) + "\\n")
if (ROOT / "ssh_down").exists():
    print("ssh: connect to host " + host + " port 22: Connection timed out", file=sys.stderr)
    sys.exit(255)
sys.exit(subprocess.run(cmd).returncode)
'''


class RemoteToy(Toy):
    def make_remote(self, slots: int = 1, max_parallel: int = 1, down: bool = False, **kw) -> Path:
        self.root_hint = Path(tempfile.mkdtemp(prefix="remote-host-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root_hint, ignore_errors=True))
        host_git, work = self.root_hint / "host.git", self.root_hint / "work"
        put(self.root_hint / "fake_ssh.py", FAKE_SSH)
        py = Path(sys.executable).as_posix()
        extra = f"""
[harness.remote]
host = "remote-host"
name = "desk"
slots = {slots}
repo = '{host_git.as_posix()}'
git = '{host_git.as_posix()}'
work = '{work.as_posix()}'
python = ['{py}']
ssh = ['{py}', '{(self.root_hint / "fake_ssh.py").as_posix()}']
env = {{ APP_NATIVE_DIR = "D:/dev/native-sdk-1.0" }}
"""
        repo = self.make_repo(functions=THREE, max_parallel=max_parallel, harness_extra=extra, **kw)
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(host_git)], check=True, capture_output=True)
        # no git identity on the host: the builder commits with the laptop repo's (Pool.start)
        if down:
            put(self.root_hint / "ssh_down", "1")
        return repo


class TestRemoteDrive(RemoteToy):
    def test_a_ready_function_builds_on_the_free_remote_slot_and_lands(self):
        repo = self.make_remote()
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        recs = self.state(repo)["functions"]
        self.assertEqual({r["status"] for r in recs.values()}, {"proven"})
        starts = {e["fn"]: e["detail"] for e in self.events(repo) if e["event"] == "build_start"}
        self.assertIn("host=desk", starts["F2"])  # F1 took the one local slot, F2 the remote one
        self.assertNotIn("host=", starts["F1"])
        brief = self.seen("F2-build-1.md")
        self.assertIn((self.root_hint / "work" / "wt" / "F2").as_posix(), brief)  # the host's worktree path
        self.assertNotIn((repo / ".overhaul" / "wt" / "F2").as_posix(), brief)
        self.assertIn("fn/F2", git(self.root_hint / "host.git", "branch", "--list", "fn/F2"))
        self.assertIn("remote-host", (self.root_hint / "ssh.log").read_text(encoding="utf-8"))
        self.assertFalse(list((self.root_hint / "work").glob("slot-*.pid")))  # the builder exited: no pid left
        self.assertIn("F2", git(repo, "log", "--oneline", "main"))  # fetched back, reviewed and landed on main

    def test_an_unreachable_host_goes_offline_and_requeues_without_an_attempt(self):
        repo = self.make_remote(down=True)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        recs = self.state(repo)["functions"]
        self.assertEqual({r["status"] for r in recs.values()}, {"proven"})
        self.assertEqual(recs["F2"]["attempts"], recs["F1"]["attempts"])  # the offline try cost nothing
        offline = [e for e in self.events(repo) if e["event"] == "remote_offline"]
        self.assertEqual(len(offline), 1, offline)  # 10 min offline: no second try in this run
        self.assertIn("host=desk for 10 min", offline[0]["detail"])
        self.assertIn("unreachable", offline[0]["detail"])
        starts = [e["detail"] for e in self.events(repo) if e["event"] == "build_start" and e["fn"] == "F2"]
        self.assertTrue(starts[0].endswith("host=desk") and "host=" not in starts[-1], starts)


class TestHostProgram(unittest.TestCase):
    """The program `python -` runs on the host, run here directly."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="remote-prog-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))

    def run_prog(self, job: dict, env: dict | None = None, wait: bool = True):
        p = subprocess.Popen([sys.executable, "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env)
        p.stdin.write(remote.host_program(job))
        p.stdin.close()
        if wait:
            p.wait(timeout=60)
        return p

    def job(self, **kw) -> dict:
        base = {"action": "build", "fid": "F9", "attempt": 1, "slot": 0, "name": "desk", "host": "desk",
                "repo": "", "work": (self.root / "work").as_posix(),
                "wt": (self.root / "work" / "wt" / "F9").as_posix(),
                "branch": "fn/F9", "sha": "", "pidfile": (self.root / "work" / "slot-0.pid").as_posix(), "env": {},
                "path_prefix": [], "auth": False, "cmd": [], "brief": "# Build F9\n", "ship": {}, "lint_hook": ""}
        return {**base, **kw}

    def test_no_claude_auth_fails_fast_with_one_line(self):
        home = self.root / "home"
        home.mkdir()
        env = {k: v for k, v in os.environ.items() if k != remote.AUTH_ENV}
        env.update(HOME=str(home), USERPROFILE=str(home))
        p = self.run_prog(self.job(auth=True), env)
        self.assertEqual(p.returncode, remote.OFFLINE)
        err = p.stderr.read().decode().strip().splitlines()
        self.assertEqual(len(err), 1, err)
        self.assertIn("no Claude auth on desk: set CLAUDE_CODE_OAUTH_TOKEN", err[0])

    def test_a_spent_account_is_an_offline_host_not_an_attempt(self):
        spent = json.dumps({"type": "result", "is_error": True,
                            "result": "You've hit your weekly limit · resets Nov 11, 3am (America/Chicago)"})
        self.assertIn("weekly limit", remote.account_problem(0, spent.encode()))
        ok = json.dumps({"type": "result", "is_error": False, "result": "DONE: kept under the usage limit"})
        self.assertEqual(remote.account_problem(0, ok.encode()), "")
        self.assertIn("Invalid API key", remote.account_problem(1, "Invalid API key · Please run /login".encode()))
        self.assertEqual(remote.account_problem(1, b"error: tests failed"), "")

    def test_kill_job_kills_what_a_dropped_session_left_in_the_slot(self):
        src = self.root / "src"
        src.mkdir()
        for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@e"), ("config", "user.name", "t"),
                     ("commit", "-q", "--allow-empty", "-m", "base")):
            git(src, *args)
        subprocess.run(["git", "clone", "-q", "--bare", str(src), str(self.root / "host.git")], check=True)
        sha = git(src, "rev-parse", "HEAD").strip()
        hang = [sys.executable, "-c", "import time; time.sleep(300)"]
        job = self.job(repo=(self.root / "host.git").as_posix(), sha=sha, cmd=hang)
        helper = self.run_prog(job, wait=False)
        pidfile = Path(job["pidfile"])
        end = time.monotonic() + 30
        while not pidfile.is_file() and time.monotonic() < end:
            time.sleep(0.2)
        builder = int(pidfile.read_text(encoding="utf-8"))
        helper.kill()  # the session drops: only the helper dies, the builder runs on
        helper.wait(timeout=15)
        self.assertTrue(pid_alive(builder))
        self.addCleanup(lambda: pid_alive(builder) and subprocess.run(["taskkill", "/PID", str(builder), "/F"]
                                                                      if os.name == "nt" else ["kill", str(builder)]))
        k = self.run_prog({"action": "kill", "pidfile": pidfile.as_posix()})
        self.assertEqual(k.returncode, 0)
        self.assertTrue(wait_dead(builder))
        self.assertFalse(pidfile.exists())
        self.assertTrue((self.root / "work" / "briefs" / "F9.md").is_file())


    def test_builder_events_stream_to_stdout_and_the_host_log_with_a_heartbeat(self):
        src = self.root / "src"
        src.mkdir()
        for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@e"), ("config", "user.name", "t"),
                     ("commit", "-q", "--allow-empty", "-m", "base")):
            git(src, *args)
        subprocess.run(["git", "clone", "-q", "--bare", str(src), str(self.root / "host.git")], check=True)
        events = ('import json, sys, time\n'
                  'for i in range(3):\n'
                  '    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", '
                  '"name": "Bash"}]}}), flush=True)\n'
                  '    time.sleep(0.5)\n'
                  'print(json.dumps({"type": "result", "subtype": "success", "is_error": False}), flush=True)\n')
        job = self.job(repo=(self.root / "host.git").as_posix(), sha=git(src, "rev-parse", "HEAD").strip(),
                       cmd=[sys.executable, "-c", events], hb_s=0.3)
        p = self.run_prog(job)
        self.assertEqual(p.returncode, 0)
        out = p.stdout.read().decode().splitlines()
        self.assertEqual(len(out), 4)
        self.assertIn('"result"', out[-1])
        err = p.stderr.read().decode()
        self.assertIn(remote.HB_TAG, err)  # the laptop's relay reads these as life and drops them
        log = (self.root / "work" / "logs" / "F9.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("builder: assistant tools=Bash"), 3)
        self.assertIn("builder: result/success", log)
        self.assertIn("builder exit 0 after 4 events", log)
        self.assertIn("events=", (self.root / "work" / "logs" / "F9.hb").read_text(encoding="utf-8"))
        fetched = self.run_prog({"action": "log", "log": (self.root / "work" / "logs" / "F9.log").as_posix()})
        self.assertEqual(fetched.stdout.read(), (self.root / "work" / "logs" / "F9.log").read_bytes())


    def test_the_first_builder_line_reaches_the_laptop_before_the_builder_exits(self):
        """remote-hang-report 2026-11-10: proc.communicate() held every line until claude exited, so each slow remote
        build looked silent. A fake claude prints one event, then sleeps: that line must arrive while it sleeps."""
        src = self.root / "src"
        src.mkdir()
        for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@e"), ("config", "user.name", "t"),
                     ("commit", "-q", "--allow-empty", "-m", "base")):
            git(src, *args)
        subprocess.run(["git", "clone", "-q", "--bare", str(src), str(self.root / "host.git")], check=True)
        fake = ('import json, time\n'
                'print(json.dumps({"type": "system", "subtype": "init"}), flush=True)\n'
                'time.sleep(60)\n')
        job = self.job(repo=(self.root / "host.git").as_posix(), sha=git(src, "rev-parse", "HEAD").strip(),
                       cmd=[sys.executable, "-c", fake])
        p = self.run_prog(job, wait=False)
        got: list[bytes] = []
        reader = threading.Thread(target=lambda: got.append(p.stdout.readline()), daemon=True)
        reader.start()
        reader.join(timeout=40)
        try:
            self.assertTrue(got and b'"init"' in got[0], f"no line before exit: {got}")
            self.assertIsNone(p.poll())  # the builder still sleeps: the line was streamed, not buffered
        finally:
            pidfile = Path(job["pidfile"])
            if pidfile.is_file():
                self.run_prog({"action": "kill", "pidfile": pidfile.as_posix()})
            p.kill()
            p.wait(timeout=15)


class TestRelay(unittest.TestCase):
    """The laptop side of a remote build: live output, heartbeats as life, a silent host killed."""

    def relay(self, code: str, idle_s: float):
        out, err = mock.Mock(buffer=io.BytesIO()), mock.Mock(buffer=io.BytesIO())
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            t0 = time.monotonic()
            res = remote.relay([sys.executable, "-c", code], b"", idle_s, poll_s=0.1)
        return res, out.buffer.getvalue().decode(), err.buffer.getvalue().decode(), time.monotonic() - t0

    def test_a_host_that_sends_nothing_is_killed_as_hung(self):
        (code, _, hung), _, _, took = self.relay("import time; time.sleep(60)", 1.0)
        self.assertTrue(hung)
        self.assertLess(took, 20)

    def test_heartbeats_keep_a_quiet_build_alive_and_never_reach_the_err_file(self):
        prog = ("import sys, time\n"
                "for i in range(8):\n"
                "    print('remote-hb t events=0 pid=1', file=sys.stderr, flush=True)\n"
                "    time.sleep(0.25)\n"
                "print('remote: note', file=sys.stderr, flush=True)\n"
                "print('{\"type\": \"result\"}', flush=True)\n")
        (code, err_text, hung), out, err, _ = self.relay(prog, 1.0)
        self.assertEqual((code, hung), (0, False))
        self.assertIn('"result"', out)
        self.assertEqual(err.strip(), "remote: note")
        self.assertNotIn("remote-hb", err_text)

    def test_a_host_the_watchdog_holds_is_refused_until_its_time(self):
        repo = Path(tempfile.mkdtemp())
        self.assertEqual(remote.held(repo, "desk"), "")
        path = remote.hold_path(repo, "desk")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"until": 1000.0, "why": "offline twice in 30 min"}), encoding="utf-8")
        self.assertIn("offline twice in 30 min", remote.held(repo, "desk", now=999.0))
        self.assertEqual(remote.held(repo, "desk", now=1001.0), "")


class TestPool(unittest.TestCase):
    CONF = {"host": "remote-host", "name": "desk", "slots": 2, "repo": "D:\\app\\v.git"}

    def test_no_table_or_zero_slots_means_no_pool(self):
        self.assertIsNone(remote.Pool.from_harness({}))
        self.assertIsNone(remote.Pool.from_harness({"remote": {**self.CONF, "slots": 0}}))
        pool = remote.Pool.from_harness({"remote": self.CONF})
        self.assertEqual((pool.repo, pool.work, pool.git, pool.python), ("D:/app/v.git", "D:/app/v.git.work",
                                                                         "desk", ["py", "-3"]))

    def test_remote_builds_get_their_own_timeout(self):
        self.assertEqual(remote.Pool(self.CONF).timeout_s, remote.TIMEOUT_MIN * 60)
        self.assertEqual(remote.Pool({**self.CONF, "timeout_min": 50}).timeout_s, 3000)

    def test_a_silent_timeout_is_the_host_not_the_row(self):
        log = mock.Mock()
        pool = remote.Pool(self.CONF, log)
        pool.busy = {0: "F1", 1: "F2"}
        err = Path(tempfile.mkdtemp()) / "x.err"
        err.write_text("", encoding="utf-8")
        with mock.patch.object(pool, "kill"):
            self.assertFalse(pool.after("F1", 1, True, err, now=0.0))  # timed out after real turns: the row's
            self.assertTrue(pool.after("F2", 1, True, err, now=0.0, silent=True))
        self.assertIsNone(pool.free_slot(1.0))
        self.assertIn("no builder output", log.call_args[0][2])

    def test_offline_window_and_slots(self):
        log = mock.Mock()
        pool = remote.Pool(self.CONF, log)
        pool.busy = {0: "F1", 1: "F2"}
        self.assertIsNone(pool.free_slot(0.0))
        err = Path(tempfile.mkdtemp()) / "x.err"
        err.write_text("noise\nremote: desk unreachable (ssh exit 255)\n", encoding="utf-8")
        with mock.patch.object(pool, "kill") as kill:
            self.assertFalse(pool.after("F1", 1, False, err, now=0.0))  # an ordinary builder failure
            self.assertTrue(pool.after("F2", remote.OFFLINE, False, err, now=0.0))
            kill.assert_called_once_with(1)
        self.assertIsNone(pool.free_slot(599.0))
        self.assertEqual(pool.free_slot(601.0), 0)
        log.assert_called_once()
        self.assertIn("host=desk for 10 min: remote: desk unreachable", log.call_args[0][2])

    def test_a_timed_out_build_is_killed_on_the_host_and_counts(self):
        pool = remote.Pool(self.CONF)
        pool.busy = {1: "F3"}
        with mock.patch.object(pool, "kill") as kill:
            self.assertFalse(pool.after("F3", -1, True, Path("missing.err"), now=0.0))
        kill.assert_called_once_with(1)
        self.assertEqual(pool.free_slot(0.0), 0)

    def test_brief_paths_map_to_the_host(self):
        pool = remote.Pool(self.CONF)
        text = "cd C:/Repo/.overhaul/wt/F1 && py -3 ~/.claude/skills/overhaul-build/scripts/dup.py"
        out = pool.map_text(text, {"C:/Repo/.overhaul/wt/F1": pool.wt("F1"),
                                   "~/.claude/skills/overhaul-build/scripts": "D:/w/skill/scripts"})
        self.assertEqual(out, "cd D:/app/v.git.work/wt/F1 && py -3 D:/w/skill/scripts/dup.py")


if __name__ == "__main__":
    unittest.main()
