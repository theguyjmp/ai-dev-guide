"""worker.py: how drive.py launches a `claude -p` worker lean, and watches its event stream.

Lean launch. A worker gets only what its role needs: no user or global CLAUDE.md, no auto-memory, no skills list,
no MCP servers and a short tool list. The project's own rules reach it through its brief. The `project` setting
source stays, so the worktree's lint hook (.claude/settings.json) still loads. Prompt caching is untouched.
Measured on the pilot project (2026-11-09): the first call of a default worker carried 37.5k tokens of fixed context before
it read anything, 48% of all replayed context.

Streamed output. Workers write `--output-format stream-json --verbose`, one JSON event per line. Stream reads the new
lines as they arrive and keeps the age of the last event, the turn count, the tokens so far and the last tool run;
drive.py writes them to .overhaul/runs/<run>.progress and kills a worker with no event for [harness]
worker_idle_min minutes (default 10). The last line of the stream is the same `result` object `--output-format
json` writes, so drive.read_result reads both.

Detached launch. `py -3 worker.py --mem GB --rc FILE -- cmd...` is how drive.py starts a local builder outside its own
kill-on-close job: the launcher puts itself in a new job with the same per-process memory cap (memcap.cap_self, kill on
close, so killing the launcher's pid kills its builder tree), runs cmd with the inherited stdout and stderr, writes
cmd's exit code to FILE and exits with it. A drive that restarts reads FILE for a builder it re-adopted (the pilot project
2026-11-09: 73 of 248 builds were killed by drive restarts).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from functools import cache
from pathlib import Path

BUILD_TOOLS = "Read,Edit,Write,Bash,Grep,Glob"  # builders, conflict and fix passes, plan fixes
READ_TOOLS = "Read,Grep,Glob,Bash"  # reviewers, verifiers, auditors: they write their one file with a script
READ_ROLES = ("review", "verify", "polish_review", "polish_verify", "polish_audit", "arch_audit", "proof")
LEAN_ENV = {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
LEAN_MARK = "--strict-mcp-config"  # a command carrying it is a lean claude worker and gets LEAN_ENV
STREAM = ["--output-format", "stream-json", "--verbose"]
IDLE_MIN = 10.0
PROGRESS_EVERY_S = 5.0


@cache
def help_text(exe: str) -> str:
    try:
        return subprocess.run([exe, "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=60, stdin=subprocess.DEVNULL, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def lean_flags(exe: str, role: str) -> list[str]:
    """The lean flags for a worker of this role, each one only if the installed claude lists it in --help."""
    tools = READ_TOOLS if role in READ_ROLES else BUILD_TOOLS
    # --tools and --mcp-config take a variable number of values: a plain flag follows each, never the prompt
    wanted = [("--tools", [tools]), ("--mcp-config", ['{"mcpServers":{}}']), ("--setting-sources", ["project,local"]),
              ("--strict-mcp-config", []), ("--disable-slash-commands", [])]
    have = help_text(exe)
    flags: list[str] = []
    for name, args in wanted:
        if not have or name + " " in have or name + "\n" in have or name + "," in have:
            flags += [name, *args]
    return flags


def env(cmd: list[str], base: dict[str, str] | None) -> dict[str, str] | None:
    """base plus LEAN_ENV for a lean claude command; a custom worker_cmd keeps base unchanged."""
    if LEAN_MARK not in cmd:
        return base
    return {**(base if base is not None else dict(os.environ)), **LEAN_ENV}


def tool_label(block: dict) -> str:
    """`Bash: cargo test -q` from a tool_use block: the tool and the first 100 characters of what it ran."""
    name, arg = str(block.get("name") or "?"), block.get("input") or {}
    if isinstance(arg, dict):
        what = next((arg[k] for k in ("command", "file_path", "pattern", "path", "url") if arg.get(k)), "")
    else:
        what = ""
    what = " ".join(str(what).split())[:100]
    return f"{name}: {what}" if what else name


class Stream:
    """Reads a worker's stream-json output as it grows. poll() parses the new complete lines."""

    def __init__(self, out: Path, started: float):
        self.out, self.pos, self.rest = out, 0, b""
        self.started = self.last_event = started
        self.usage: dict[str, int] = {}  # assistant message id -> its context + output tokens
        self.events = 0
        self.first_ctx: int | None = None
        self.ctx = 0
        self.last_tool = ""
        self.result: dict | None = None
        self.written = 0.0

    def poll(self, now: float | None = None) -> bool:
        """Parse what the worker wrote since the last poll; True if it wrote at least one event."""
        now = time.monotonic() if now is None else now
        try:
            with open(self.out, "rb") as fh:
                fh.seek(self.pos)
                chunk = fh.read()
        except OSError:
            return False
        if not chunk:
            return False
        self.pos += len(chunk)
        *lines, self.rest = (self.rest + chunk).split(b"\n")
        seen = False
        for raw in lines:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if isinstance(ev, dict):
                self.take(ev)
                seen = True
        if seen:
            self.last_event = now
        return seen

    def take(self, ev: dict) -> None:
        self.events += 1
        kind = ev.get("type")
        if kind == "result":
            self.result = ev
            return
        if kind != "assistant":
            return
        msg = ev.get("message") or {}
        u = msg.get("usage") or {}
        ctx = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                "cache_creation_input_tokens"))
        if ctx:
            self.ctx = ctx
            if self.first_ctx is None:
                self.first_ctx = ctx
            self.usage[str(msg.get("id") or self.events)] = ctx + int(u.get("output_tokens") or 0)
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                self.last_tool = tool_label(block)

    @property
    def turns(self) -> int:
        return len(self.usage)

    @property
    def tokens(self) -> int:
        return sum(self.usage.values())

    def idle_s(self, now: float) -> float:
        return now - self.last_event

    def progress(self, now: float) -> dict:
        return {"t": datetime.now().astimezone().isoformat(timespec="seconds"),
                "age_s": round(now - self.started), "idle_s": round(self.idle_s(now)), "events": self.events,
                "turns": self.turns, "tokens": self.tokens, "ctx": self.ctx, "first_ctx": self.first_ctx,
                "last_tool": self.last_tool, "done": self.result is not None}

    def write_progress(self, now: float, force: bool = False) -> None:
        """One JSON line in <run>.progress beside the run file, at most every PROGRESS_EVERY_S seconds."""
        if not force and now - self.written < PROGRESS_EVERY_S:
            return
        self.written = now
        try:
            self.out.with_suffix(".progress").write_text(json.dumps(self.progress(now)) + "\n", encoding="utf-8")
        except OSError:
            pass

    def stalled(self, now: float, idle_min: float) -> str:
        """'' while the worker is live; else the timeout note naming the last tool it ran."""
        if idle_min <= 0 or self.result is not None or self.idle_s(now) < idle_min * 60:
            return ""
        return (f"no worker event for {self.idle_s(now) / 60:.0f} min (worker_idle_min {idle_min:g}); "
                f"last tool {self.last_tool or 'none'}")


def launch_cmd(cmd: list[str], mem_gb: float, rc: Path) -> list[str]:
    """cmd wrapped in the detached launcher (this file's main)."""
    return [sys.executable, str(Path(__file__).resolve()), "--mem", f"{float(mem_gb or 0):g}", "--rc", str(rc),
            "--", *cmd]


def main(argv: list[str]) -> int:
    """--mem GB --rc FILE -- cmd...: run cmd in a fresh job with the cap, write its exit code to FILE."""
    try:
        cut = argv.index("--")
        opts, cmd = dict(zip(argv[:cut:2], argv[1:cut:2])), argv[cut + 1:]
        gb, rc = float(opts.get("--mem") or 0), Path(opts["--rc"])
    except (ValueError, KeyError):
        print("usage: worker.py --mem GB --rc FILE -- cmd [args...]", file=sys.stderr)
        return 2
    if not cmd:
        print("usage: worker.py --mem GB --rc FILE -- cmd [args...]", file=sys.stderr)
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import memcap
    note = memcap.cap_self(gb, kill_on_close=True)
    if note.startswith("process memory cap not set"):
        print(f"worker.py: {note}", file=sys.stderr, flush=True)
    try:
        code = subprocess.call(cmd, stdin=subprocess.DEVNULL)
    except OSError as e:
        print(f"worker.py: cannot start {cmd[0]}: {e}", file=sys.stderr, flush=True)
        code = 127
    tmp = rc.with_name(rc.name + ".tmp")
    try:
        tmp.write_text(str(code), encoding="utf-8")
        os.replace(tmp, rc)
    except OSError:
        pass
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
