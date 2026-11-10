"""Lean worker launch and the stream-json no-event watchdog (worker.py, drive.py), with fake workers."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import drive  # noqa: E402
import worker  # noqa: E402
from test_drive import Toy  # noqa: E402

HELP = """  --tools <tools...>  x
  --mcp-config <configs...>  x
  --setting-sources <sources>  x
  --strict-mcp-config  x
  --disable-slash-commands  x
  --max-turns <n>  x
"""


def ev_assistant(mid: str, ctx: int, tool: dict | None = None) -> str:
    content = [{"type": "tool_use", **tool}] if tool else [{"type": "text", "text": "ok"}]
    usage = {"input_tokens": 2, "cache_read_input_tokens": ctx - 2, "output_tokens": 10}
    return json.dumps({"type": "assistant", "message": {"id": mid, "content": content, "usage": usage}})


RESULT = json.dumps({"type": "result", "result": "DONE abc", "num_turns": 2,
                     "usage": {"input_tokens": 4, "cache_read_input_tokens": 9000, "output_tokens": 20}})
BASH = {"name": "Bash", "input": {"command": "ssh gpu-box cargo test --release"}}


def streamer(path: Path, lines: list[str], then_sleep: float) -> Path:
    """A fake worker script: prints lines (flushed, one by one), then sleeps."""
    path.write_text(textwrap.dedent(f"""
        import sys, time
        for line in {lines!r}:
            print(line, flush=True)
            time.sleep(0.05)
        time.sleep({then_sleep})
    """), encoding="utf-8")
    return path


class TestLeanCommand(Toy):
    def driver(self, harness_extra: str = "") -> drive.Driver:
        repo = self.make_repo(harness_extra=harness_extra)
        toml = repo / "overhaul.toml"
        toml.write_text("\n".join(ln for ln in toml.read_text(encoding="utf-8").splitlines()
                                  if not ln.startswith("worker_cmd")) + "\n", encoding="utf-8")
        return drive.Driver(repo, drive.load_config(repo))

    def cmds(self, drv: drive.Driver) -> dict[str, list[str]]:
        with mock.patch.object(drive.shutil, "which", return_value="claude"), \
                mock.patch.object(drive, "supports_max_turns", return_value=True), \
                mock.patch.object(worker, "help_text", return_value=HELP):
            out = {k: drv.worker_cmd(k) for k in ("build", "conflict", "review", "verify")}
            out.update({k: drv.polish_cmd(k)[0] for k in ("polish_fix", "polish_audit", "arch_audit")})
            return out

    @staticmethod
    def flag(cmd: list[str], name: str) -> str | None:
        return cmd[cmd.index(name) + 1] if name in cmd else None

    def test_every_claude_worker_is_lean_and_streams(self):
        for kind, cmd in self.cmds(self.driver()).items():
            self.assertEqual(self.flag(cmd, "--output-format"), "stream-json", kind)
            self.assertIn("--verbose", cmd, kind)
            self.assertEqual(self.flag(cmd, "--setting-sources"), "project,local", kind)  # the lint hook stays
            self.assertEqual(json.loads(self.flag(cmd, "--mcp-config")), {"mcpServers": {}}, kind)
            for name in ("--strict-mcp-config", "--disable-slash-commands"):
                self.assertIn(name, cmd, kind)
            for variadic in ("--tools", "--mcp-config"):  # never followed by the prompt
                self.assertTrue(cmd[cmd.index(variadic) + 2].startswith("--"), (kind, cmd))

    def test_tools_by_role(self):
        cmds = self.cmds(self.driver())
        for kind in ("build", "conflict", "polish_fix"):
            self.assertEqual(self.flag(cmds[kind], "--tools"), "Read,Edit,Write,Bash,Grep,Glob", kind)
        for kind in ("review", "verify", "polish_audit", "arch_audit"):
            self.assertEqual(self.flag(cmds[kind], "--tools"), "Read,Grep,Glob,Bash", kind)

    def test_a_flag_the_installed_claude_lacks_is_left_out(self):
        with mock.patch.object(worker, "help_text", return_value=HELP.replace("--disable-slash-commands", "")):
            flags = worker.lean_flags("claude", "build")
        self.assertNotIn("--disable-slash-commands", flags)
        self.assertIn("--strict-mcp-config", flags)

    def test_lean_env_only_for_a_lean_command(self):
        base = {"PATH": "x", "CARGO_TARGET_DIR": "t"}
        lean = worker.env(["claude", "-p", "--strict-mcp-config"], base)
        self.assertEqual(lean, {**base, "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
        self.assertIs(worker.env(["py", "fake_worker.py"], base), base)  # a custom worker_cmd is untouched

    def test_worker_idle_min_comes_from_overhaul_toml(self):
        self.assertEqual(self.driver().idle_min, 10.0)
        self.assertEqual(self.driver("worker_idle_min = 3").idle_min, 3.0)


class TestStreamWatch(Toy):
    def job(self, lines: list[str], then_sleep: float, idle_min: float) -> tuple[drive.Driver, drive.Job]:
        repo = self.make_repo()
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.idle_min = idle_min
        (drv.ov / "runs").mkdir(parents=True, exist_ok=True)
        script = streamer(self.root / "streamer.py", lines, then_sleep)
        cmd = [sys.executable, str(script), "--output-format", "stream-json"]
        out = drv.ov / "runs" / "F1-a1-build.json"
        start = time.monotonic()
        job = drive.Job("F1", "build", drive.spawn(cmd, self.root, out), out, start, start + 600, 1)
        self.addCleanup(drive.kill_tree, job.proc)
        return drv, drv.watch(job, cmd)

    def test_a_stalled_stream_is_killed_as_a_timeout_naming_its_last_tool(self):
        drv, job = self.job([ev_assistant("m1", 7000), ev_assistant("m2", 7500, BASH)], 120, idle_min=0.02)
        end = time.monotonic() + 30
        while not drv.stalled(job, time.monotonic()) and time.monotonic() < end:
            time.sleep(0.2)
        self.assertTrue(job.idle, "the watchdog never fired")
        self.assertIn("last tool Bash: ssh gpu-box cargo test --release", job.idle)
        timeouts = [e for e in self.events(drv.repo) if e["event"] == "timeout"]
        self.assertEqual(len(timeouts), 1)
        self.assertIn("build: no worker event for", timeouts[0]["detail"])
        self.assertEqual(timeouts[0]["tokens"], 7010 + 7510)
        drive.kill_tree(job.proc)
        job.timed_out = True
        self.assertIn("builder killed: no worker event", drv.build_problem(job, ""))
        progress = json.loads(job.out.with_suffix(".progress").read_text(encoding="utf-8"))
        self.assertEqual((progress["turns"], progress["first_ctx"], progress["done"]), (2, 7000, False))
        self.assertEqual(drive.read_result(job.out), (14520, 2, ""))  # no result event: counted from the stream

    def test_a_finished_stream_is_never_stalled_and_its_result_is_read(self):
        drv, job = self.job([ev_assistant("m1", 7000, BASH), ev_assistant("m2", 7400), RESULT], 0, idle_min=0.001)
        job.proc.wait(timeout=30)
        time.sleep(0.2)
        self.assertFalse(drv.stalled(job, time.monotonic() + 3600))
        self.assertEqual(job.stream.turns, 2)
        self.assertEqual(drive.read_result(job.out), (9024, 2, "DONE abc"))

    def test_a_live_stream_resets_the_idle_clock(self):
        _, job = self.job([ev_assistant("m1", 7000)], 120, idle_min=1)
        end = time.monotonic() + 30
        while not job.stream.poll() and time.monotonic() < end:
            time.sleep(0.1)
        self.assertEqual(job.stream.stalled(job.stream.last_event + 59, 1), "")
        self.assertIn("last tool none", job.stream.stalled(job.stream.last_event + 61, 1))
        self.assertEqual(job.stream.stalled(job.stream.last_event + 9999, 0), "")  # 0: no watchdog

    def test_a_stream_json_toy_run_lands_and_leaves_progress_lines(self):
        repo = self.make_repo(worker_args=', "--output-format", "stream-json"')
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ends = [e for e in self.events(repo) if e["event"] == "build_end"]
        self.assertTrue(ends and all(e["tokens"] == 120 and "turns=3/" in e["detail"] for e in ends), ends)
        progress = sorted((repo / ".overhaul" / "runs").glob("*-build.progress"))
        self.assertTrue(progress, "no progress line")
        self.assertTrue(all(json.loads(p.read_text(encoding="utf-8"))["done"] for p in progress))


if __name__ == "__main__":
    unittest.main()
