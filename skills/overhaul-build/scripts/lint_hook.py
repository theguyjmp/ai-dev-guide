"""lint_hook.py: lint on every edit (overhaul v3). A Claude Code PostToolUse hook for Edit and Write.

drive.py writes it into each builder worktree's .claude/settings.json:
    {"hooks": {"PostToolUse": [{"matcher": "Edit|Write|MultiEdit",
                                "hooks": [{"type": "command", "command": "\"<python>\" \"<this file>\""}]}]}}

Reads the hook JSON on stdin (tool_input.file_path, cwd), finds overhaul.toml at or above cwd and lints the one edited
file: [commands].lint_file with {file}, else [commands].lint whose last token, when it is `.` or a directory (`src`), is replaced by the file.
A ruff lint is skipped for a non-.py file. Lint failures go to stderr with exit 2, which Claude Code hands back to the
builder; a clean file, a skipped file or any setup problem exits 0 so the hook never blocks work by itself.
"""
import json
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

TAIL = 40
LINT_TIMEOUT_S = 60
CODE_SUFFIXES = {".py", ".rs", ".js", ".ts", ".jsx", ".tsx", ".mjs"}


def find_root(start: Path) -> Path | None:
    for d in (start, *start.parents):
        if (d / "overhaul.toml").is_file():
            return d
    return None


def file_cmd(commands: dict, rel: str, root: Path) -> str:
    """The lint command for one file, or '' when this file is not linted per edit."""
    if commands.get("lint_file"):
        return str(commands["lint_file"]).replace("{file}", f'"{rel}"')
    lint = str(commands.get("lint") or "").strip()
    if not lint or ("ruff" in lint and not rel.endswith(".py")) or Path(rel).suffix not in CODE_SUFFIXES:
        return ""
    parts = lint.rsplit(" ", 1)
    if len(parts) < 2 or not (parts[1] == "." or (root / parts[1].strip('"')).is_dir()):
        return ""
    return f'{parts[0]} "{rel}"'


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
        path = Path((event.get("tool_input") or {}).get("file_path") or "")
        root = find_root(Path(event.get("cwd") or ".").resolve())
        if not path.name or root is None or not path.is_file():
            return 0
        rel = path.resolve().relative_to(root).as_posix()
        commands = tomllib.loads((root / "overhaul.toml").read_text(encoding="utf-8")).get("commands") or {}
        cmd = file_cmd(commands, rel, root)
        if not cmd:
            return 0
        proc = subprocess.run(cmd, cwd=root, shell=True, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=LINT_TIMEOUT_S, check=False)
    except (ValueError, OSError, tomllib.TOMLDecodeError, subprocess.TimeoutExpired):
        return 0
    if proc.returncode == 0:
        return 0
    out = (proc.stdout + proc.stderr).strip().splitlines()[-TAIL:]
    print(f"lint failed on {rel} ({shlex.split(cmd, posix=False)[0]} ...); fix it before the next edit:", file=sys.stderr)
    print("\n".join(out), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
