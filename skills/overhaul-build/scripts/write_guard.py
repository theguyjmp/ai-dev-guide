"""write_guard.py: per-worker write limits (overhaul v3). A Claude Code PreToolUse hook for Edit, Write, MultiEdit and
NotebookEdit.

drive.py (and remote.py on a build host) writes it into each builder worktree's .claude/settings.json next to the
lint hook, and before each build or conflict pass writes that pass's limits to <worktree>/.claude/write-limits.json:
    {"job": "build F9", "allowed": ["src/alpha/f9.py", "tests/unit/"], "visibility": ["src/beta/"]}
`allowed` is the claim (the row's files cell or its module's paths) plus the unit-test dirs; `visibility` is where a
visibility-only edit is allowed (the content is checked after the build, as before). Paths are repo-relative: a dir
ends in /, * ? [ make a glob.

Reads the hook JSON on stdin (tool_input.file_path or notebook_path, cwd), finds write-limits.json at or above cwd and
refuses, with exit 2 and the reason on stderr (Claude Code hands it to the builder), a write outside the worktree
(the system temp dir excepted) or to a worktree path in neither list. No limits file, or any setup problem, exits 0.
Bash can still write files: the post-build visibility check and the review stay the backstop for that.
"""
import fnmatch
import json
import os
import sys
import tempfile
from pathlib import Path

LIMITS = Path(".claude") / "write-limits.json"
SHOW = 12  # allowed paths named in a refusal


def in_paths(path: str, allowed: list[str]) -> bool:
    """Whether a repo-relative file is under one of the allowed paths (a dir ends in /, * ? [ make a glob)."""
    return any(fnmatch.fnmatchcase(path, p) if any(c in p for c in "*?[") else
               path.startswith(p) if p.endswith("/") else path == p for p in allowed)


def find_root(start: Path) -> Path | None:
    for d in (start, *start.parents):
        if (d / LIMITS).is_file():
            return d
    return None


def under(path: str, root: str) -> bool:
    path, root = os.path.normcase(path), os.path.normcase(root).rstrip("\\/")
    return path == root or path.startswith(root + os.sep)


def refusal(event: dict) -> str:
    """Why this write is outside the limits, or '' when it is allowed (or there are no limits)."""
    tool = event.get("tool_input") or {}
    target = str(tool.get("file_path") or tool.get("notebook_path") or "")
    cwd = Path(os.path.abspath(event.get("cwd") or "."))
    root = find_root(cwd)
    if not target or root is None:
        return ""
    limits = json.loads((root / LIMITS).read_text(encoding="utf-8"))
    job = limits.get("job") or "pass"
    path = os.path.abspath(cwd / target)
    # a junction or subst drive may name the worktree two ways: plain first, then both fully resolved
    pairs = [(path, str(root)), (os.path.realpath(path), os.path.realpath(root))]
    inside = next(((p, r) for p, r in pairs if under(p, r)), None)
    if inside is None:
        tmp = tempfile.gettempdir()
        if any(under(p, t) for p in (path, os.path.realpath(path)) for t in (tmp, os.path.realpath(tmp))):
            return ""
        return f"{Path(path).as_posix()} is outside your worktree {root.as_posix()}"
    rel = Path(os.path.relpath(*inside)).as_posix()
    allowed, vis = list(limits.get("allowed") or []), list(limits.get("visibility") or [])
    fold = (lambda s: s.lower()) if os.name == "nt" else (lambda s: s)
    if in_paths(fold(rel), [fold(p) for p in allowed + vis]):
        return ""
    more = f" (+{len(allowed) - SHOW} more)" if len(allowed) > SHOW else ""
    return (f"{rel} is outside the allowed paths of this {job}: "
            + ", ".join(allowed[:SHOW]) + more
            + (f"; visibility-only edits in: {', '.join(vis[:SHOW])}" if vis else ""))


def main() -> int:
    try:
        why = refusal(json.loads(sys.stdin.read() or "{}"))
    except (ValueError, OSError, AttributeError, TypeError):
        return 0
    if not why:
        return 0
    print(f"write-limits: refused. {why}. Do not write it another way. If the function cannot be built without "
          "this file, stop: a landed dependency's bug gets a BLOCKED-BY line, any other missing file a BLOCKED "
          "final message naming it.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
