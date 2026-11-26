"""The verifier's full gate on one trunk sha. Exit 0 green, 1 red, 2 no verdict.

    py -3 trunk.py verify --gate-cmd "py -3 gate_sha.py {sha}"
"""
from __future__ import annotations

import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as h  # noqa: E402

GREEN, RED, NO_VERDICT = 0, 1, 2
VERIFIED = "verified"


def checkout(sha: str) -> None:
    if not (h.TRAIN / ".git").exists():
        h.git("worktree", "add", "-f", "--detach", str(h.TRAIN), sha)
    h.git("reset", "-q", "--hard", cwd=h.TRAIN, check=False)
    h.git("checkout", "-q", "--detach", sha, cwd=h.TRAIN)
    h.git("reset", "-q", "--hard", sha, cwd=h.TRAIN)
    h.copy_assets(h.TRAIN)


def verdict(ok: bool, fails: list, log: str, capped: set = frozenset(), slow_at_base: set = frozenset()) -> int:
    """Capped at the candidate only: no verdict; capped at the base too: pre-existing slow (CYCLE.md)."""
    if h.P["env_fail_re"] and re.search(h.P["env_fail_re"], log or ""):
        return NO_VERDICT
    kept = [t for t in fails if t not in h.known_red_tests() and t not in slow_at_base]
    if [t for t in kept if t not in capped]:
        return RED
    if kept:
        return NO_VERDICT
    return GREEN if ok or fails else NO_VERDICT


def judge(sha: str, base: str) -> tuple:
    """(code, fails): the duplicate review, then harness.gate."""
    if h.desk.offload(h) and (why := h.desk.lease_refusal(h)):  # cargo falls back to local units instead
        return NO_VERDICT, [why]
    checkout(sha)
    try:
        dups = h.dup_text(h.batch_review([], cwd=h.TRAIN, base=base))
    except h.ReviewUnavailable:
        return NO_VERDICT, ["duplicate review unavailable"]
    if dups:
        return RED, [dups[:300]]
    spec = h.TRAIN / h.SPEC
    rows = h.parse_spec(spec.read_text(encoding="utf-8", errors="replace")) if spec.is_file() else {}
    h.save(h.CAPPED, {})
    ok, fails, log = h.gate([], rows)
    capped = {t: tail for t, tail in h.load(h.CAPPED, {}).items() if t in fails}
    slow = {}
    if capped:
        checkout(base)
        slow = {t: capped[t] for t, tail in h.run_alone(sorted(capped)).items() if tail.startswith(h.TIME_CAP)}
        h.quarantine.slow(h, slow, base)
    return verdict(ok, fails, log, set(capped), set(slow)), fails


def main(argv: list) -> int:
    if len(argv) != 1:
        print(__doc__)
        return NO_VERDICT
    base = h.git("rev-parse", "-q", "--verify", VERIFIED, check=False).stdout.strip()
    if not base:
        print(f"NO VERDICT no `{VERIFIED}` ref")
        return NO_VERDICT
    h.BASE_REF = VERIFIED  # main may already hold the candidate
    try:
        with h.lock("train"):
            code, fails = judge(argv[0], base)
    except Exception as e:  # noqa: BLE001  exit 1 would read as red
        code, fails = NO_VERDICT, [f"{type(e).__name__}: {str(e)[-300:]}"]
    h.event("verifier_gate", sha=argv[0], verdict=["green", "red", "no verdict"][code], fails=fails[:20])
    print(["GREEN", "RED", "NO VERDICT"][code], argv[0][:12], " ".join(fails[:10]))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
