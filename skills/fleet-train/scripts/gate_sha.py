"""The verifier's full gate on one trunk sha. Exit 0 green, 1 red, 2 no verdict.

    py -3 trunk.py verify --gate-cmd "py -3 gate_sha.py {sha}"
"""
from __future__ import annotations

import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as h  # noqa: E402

GREEN, RED, NO_VERDICT = 0, 1, 2


def checkout(sha: str) -> None:
    if not (h.TRAIN / ".git").exists():
        h.git("worktree", "add", "-f", "--detach", str(h.TRAIN), sha)
    h.git("reset", "-q", "--hard", cwd=h.TRAIN, check=False)
    h.git("checkout", "-q", "--detach", sha, cwd=h.TRAIN)
    h.git("reset", "-q", "--hard", sha, cwd=h.TRAIN)
    h.copy_assets(h.TRAIN)


def verdict(ok: bool, fails: list, log: str) -> int:
    """Known-red never counts; an env failure or a nameless red is no verdict."""
    if h.P["env_fail_re"] and re.search(h.P["env_fail_re"], log or ""):
        return NO_VERDICT
    kept = [t for t in fails if t not in h.known_red_tests()]
    if ok or (fails and not kept):
        return GREEN
    return RED if kept else NO_VERDICT


def main(argv: list) -> int:
    if len(argv) != 1:
        print(__doc__)
        return NO_VERDICT
    with h.lock("train"):
        checkout(argv[0])
        spec = h.TRAIN / h.SPEC
        rows = h.parse_spec(spec.read_text(encoding="utf-8", errors="replace")) if spec.is_file() else {}
        ok, fails, log = h.gate([], rows)
    code = verdict(ok, fails, log)
    h.event("verifier_gate", sha=argv[0], verdict=["green", "red", "no verdict"][code], fails=fails[:20])
    print(["GREEN", "RED", "NO VERDICT"][code], argv[0][:12], " ".join(fails[:10]))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
