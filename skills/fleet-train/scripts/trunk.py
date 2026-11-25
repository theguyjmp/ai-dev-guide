"""Trunk refs of the landing cycle (CYCLE.md "Reference implementation"): receipts, the CAS push, `verified`.

    py -3 trunk.py push --sha S --base B --log CHECK.log --name N [--rows R ...] [--kind lane]
    py -3 trunk.py verify --gate-cmd "<cmd; {sha} is replaced; exit 0 green, 1 red, other no verdict>"
    py -3 trunk.py status
    py -3 trunk.py unhold --why "<fix>"
"""
from __future__ import annotations

import argparse, json, os, shlex, subprocess, sys, tempfile, time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

TRUNK, MAIN, VERIFIED = "integ", "main", "verified"
PUSHED, REFUSED, HELD, RECHECK, RACE = 0, 1, 3, 4, 5
PUSH_CODES = {PUSHED: "PUSHED", REFUSED: "REFUSED", HELD: "HELD", RECHECK: "RECHECK", RACE: "RACE"}
MAX_PUSHES = 32


@dataclass(frozen=True)
class Receipt:
    id: str
    base: str
    sha: str
    kind: str  # lane | revert | verifier
    name: str = ""
    rows: tuple = ()
    patch_id: str = ""
    selection: str = ""
    reverts: tuple = ()
    t: float = 0.0
    rebased_from: str = ""


@dataclass
class Round:
    pushes: list = field(default_factory=list)
    verified: str = ""  # the sha `verified` moved to, "" when it did not move
    culprit: str = ""  # the one push a red isolated (reverted)
    unrecorded: list = field(default_factory=list)  # trunk commits with no valid receipt (reverted first)
    no_verdict: list = field(default_factory=list)
    held: str = ""  # a hold stops the round: nothing gated, moved or reverted


class Trunk:
    def __init__(self, repo: str | Path, state: str | Path):
        self.repo, self.state = Path(repo), Path(state)
        self.state.mkdir(parents=True, exist_ok=True)

    def git(self, *args: str, check: bool = True, input: str | None = None) -> str:
        r = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, input=input)
        if check and r.returncode:
            raise RuntimeError(f"git {' '.join(args[:3])}: {r.stderr.strip()[-300:]}")
        return r.stdout.strip() if r.returncode == 0 else ""

    def rev(self, ref: str) -> str:
        return self.git("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False)

    def ancestor(self, a: str, b: str) -> bool:
        return subprocess.run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", a, b]).returncode == 0

    def cas(self, ref: str, new: str, old: str) -> bool:
        """Compare-and-swap: moves refs/heads/<ref> only while it still holds `old`."""
        r = subprocess.run(["git", "-C", str(self.repo), "update-ref", f"refs/heads/{ref}", new, old or "0" * 40],
                           capture_output=True, text=True)
        return r.returncode == 0

    def patch_id(self, base: str, sha: str) -> str:
        diff = self.git("diff", "--binary", base, sha)
        out = self.git("patch-id", "--stable", input=diff + "\n") if diff else ""
        return out.split()[0] if out else "empty"

    def receipts(self) -> dict:
        p = self.state / "receipts.jsonl"
        if not p.exists():
            return {}
        rs = (json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip())
        return {r["sha"]: Receipt(**{**r, "rows": tuple(r["rows"]), "reverts": tuple(r["reverts"])}) for r in rs}

    def _record(self, r: Receipt) -> None:
        with open(self.state / "receipts.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(asdict(r)) + "\n")

    def hold(self) -> str:
        p = self.state / "hold.txt"
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""

    def unhold(self, why: str) -> None:
        (self.state / "hold.txt").unlink(missing_ok=True)
        self._event("unhold", why=why)

    def _event(self, kind: str, **kw) -> None:
        with open(self.state / "trunk-events.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"event": kind, "t": time.time(), **kw}) + "\n")

    def follow_main(self) -> bool:
        """`main` fast-forwards to the trunk; a lost swap is lag the next move heals, never a hold."""
        head, main = self.rev(TRUNK), self.rev(MAIN)
        if not head or head == main:
            return True
        if main and not self.ancestor(main, head):
            (self.state / "hold.txt").write_text(f"main {main[:12]} is no ancestor of {TRUNK}\n", encoding="utf-8")
            return False
        return self.cas(MAIN, head, main)

    def push(self, sha: str, base: str, log: str, kind: str = "lane", name: str = "", rows: tuple = (),
             reverts: tuple = (), selection: str = "") -> tuple:
        """(code, message). The log's first line is `CHECKED <sha> <patch-id>` and its last `GATES OK`."""
        if (why := self.hold()) and kind == "lane":
            return HELD, why
        lines = [l for l in log.splitlines() if l.strip()]
        head = lines[0].split() if lines else []
        if len(head) != 3 or head[0] != "CHECKED" or lines[-1].strip() != "GATES OK":
            return REFUSED, "the check log must start `CHECKED <sha> <patch-id>` and end `GATES OK`"
        trunk = self.rev(TRUNK)
        if base != trunk:
            return RACE, f"the trunk moved to {trunk[:12]}: rebase onto it and push again"
        if not self.ancestor(base, sha) or self.git("rev-list", "--merges", f"{base}..{sha}"):
            return REFUSED, "not a linear descendant of the trunk (rebase; merge commits are refused)"
        pid = self.patch_id(base, sha)
        if head[1] != sha and head[2] != pid:
            return RECHECK, "the patch changed since its checks ran: run the checks again"
        r = Receipt(f"{kind}@{sha[:12]}", base, sha, kind, name, tuple(rows), pid, selection, tuple(reverts),
                    time.time(), head[1] if head[1] != sha else "")
        self._record(r)  # before the swap: a trunk commit without a receipt is reverted as unrecorded
        if not self.cas(TRUNK, sha, base):
            return RACE, "lost the swap: rebase onto the trunk and push again"
        self.follow_main()
        self._event("push", id=r.id, sha=sha)
        return PUSHED, r.id

    def pushes(self) -> tuple:
        """([(sha, Receipt)] after `verified`, [unrecorded shas]); a receipt sits on its push's tip."""
        v, head = self.rev(VERIFIED), self.rev(TRUNK)
        shas = self.git("rev-list", "--first-parent", "--reverse", f"{v}..{head}" if v else head).split() if head else []
        recs, out, bad, pending = self.receipts(), [], [], []
        base = self.rev(f"{shas[0]}^") if shas else ""
        undone = {x.split("..")[-1] for s in shas if (r := recs.get(s)) and r.kind == "revert" for x in r.reverts}
        for s in shas:
            r = recs.get(s)
            if r is None:
                pending.append(s)
                continue
            if r.base == base and (r.kind != "lane" or self.patch_id(r.base, s) == r.patch_id) and (
                    r.kind != "unrecorded" or s in undone):
                out.append((s, r))
            else:
                bad += [*pending, s]
            pending, base = [], s
        return out, bad + pending

    def move_verified(self, sha: str) -> bool:
        old = self.rev(VERIFIED)
        if old and not self.ancestor(old, sha):
            return False
        return self.ancestor(sha, self.rev(TRUNK)) and self.cas(VERIFIED, sha, old)

    def revert(self, specs: list, reason: str) -> str:
        """One recorded revert of `specs` (shas or `base..tip`, oldest first) on the head; "" and a hold on failure."""
        head, who = self.rev(TRUNK), ("-c", "user.name=verifier", "-c", "user.email=verifier@example.invalid")
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d) / "wt"
            self.git("worktree", "add", "-q", "--detach", str(wt), head)
            try:
                w = Trunk(wt, self.state)
                w.git(*who, "revert", "--no-commit", *reversed(specs))
                w.git(*who, "commit", "-qm", f"revert {' '.join(x[-12:] for x in specs)}: {reason}")
                new = w.rev("HEAD")
            except RuntimeError as e:
                (self.state / "hold.txt").write_text(f"revert of {specs} failed: {e}\n", encoding="utf-8")
                return ""
            finally:
                self.git("worktree", "remove", "--force", str(wt), check=False)
        pid = self.patch_id(head, new)
        self._record(Receipt(f"revert@{new[:12]}", head, new, "revert", reason, (), pid, "", tuple(specs), time.time()))
        if not self.cas(TRUNK, new, head):
            return ""
        self.follow_main()
        with open(self.state / "reverted.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"reverted": specs, "reason": reason, "revert_sha": new}) + "\n")
        return new

    def verify(self, gate: Callable[[str], bool | None], max_pushes: int = MAX_PUSHES) -> Round:
        """Unrecorded commits are reverted first; then closed prefixes of n, n/2 ... pushes, longest first: `verified`
        takes the longest green, a red single push is reverted. gate(sha): True, False or None (no verdict)."""
        if why := self.hold():
            return Round(held=why)
        pushes, bad = self.pushes()
        out = Round([s for s, _ in pushes], unrecorded=bad)
        if bad:
            for s in bad:  # recorded as `unrecorded` so the chain stays whole; the revert undoes them
                self._record(Receipt(f"unrecorded@{s[:12]}", self.rev(f"{s}^"), s, "unrecorded", t=time.time()))
            self.revert(bad, "unrecorded push")
            return out
        n = min(len(pushes), max_pushes)
        for size in sorted({max(1, n >> k) for k in range(n.bit_length())}, reverse=True):
            if not closed(pushes, size):
                continue
            sha, r = pushes[size - 1]
            verdict = gate(sha)
            if verdict is None:
                out.no_verdict.append(sha)
                return out
            if verdict:
                self.move_verified(sha)
                out.verified = sha
                return out
            if size == 1 and r.kind == "lane":
                out.culprit = sha
                self.revert([f"{r.base}..{sha}"], "red at the verifier's full gate")
        return out


def closed(pushes: list, size: int) -> bool:
    """False when a push in the first `size` is undone by a revert after them (half a revert pair proves nothing)."""
    undone = {spec.split("..")[-1] for _, r in pushes[size:] if r.kind == "revert" for spec in r.reverts}
    return not undone & {s for s, _ in pushes[:size]}


def run_gate(cmd: str) -> Callable[[str], bool | None]:
    def gate(sha: str) -> bool | None:
        rc = subprocess.run(cmd.replace("{sha}", shlex.quote(sha)), shell=True).returncode
        return {0: True, 1: False}.get(rc)
    return gate


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=os.environ.get("HARNESS_REPO", "."))
    ap.add_argument("--state", default=str(Path(os.environ.get("HARNESS_HOME", ".")) / "state" / "trunk"))
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("push")
    for a in ("--sha", "--base", "--log", "--name"):
        p.add_argument(a, required=True)
    p.add_argument("--rows", nargs="*", default=[])
    p.add_argument("--kind", default="lane")
    sp.add_parser("verify").add_argument("--gate-cmd", required=True)
    sp.add_parser("status")
    sp.add_parser("unhold").add_argument("--why", required=True)
    a = ap.parse_args(argv)
    t = Trunk(a.repo, a.state)
    if a.cmd == "push":
        code, msg = t.push(a.sha, a.base, Path(a.log).read_text(encoding="utf-8"), a.kind, a.name, tuple(a.rows))
        print(f"{PUSH_CODES[code]} {msg}")
        return code
    if a.cmd == "verify":
        print(json.dumps(asdict(t.verify(run_gate(a.gate_cmd)))))
    elif a.cmd == "unhold":
        t.unhold(a.why)
    pushes, bad = t.pushes()
    print(f"{MAIN} {t.rev(MAIN)[:12]} {VERIFIED} {t.rev(VERIFIED)[:12]} | {len(pushes)} unverified, "
          f"{len(bad)} unrecorded | hold: {t.hold() or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
