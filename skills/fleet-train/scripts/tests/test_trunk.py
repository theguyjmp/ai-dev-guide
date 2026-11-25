"""trunk.py on a temp repo: receipts, the compare-and-swap push and its exit codes, `main` following the trunk,
unrecorded commits, and verifier rounds (longest green prefix, a red push isolated and reverted, no verdict moves nothing).

Usage: py -3 tests/test_trunk.py"""
from __future__ import annotations

import subprocess, sys, tempfile
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOME))
import trunk as tk  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {detail}"))
    if not ok:
        FAILS.append(name)


def git(d, *a) -> str:
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def commit(d, name: str, text: str, base: str) -> str:
    """A commit of file `name` on top of `base` (detached), returned as its sha."""
    git(d, "checkout", "-q", "--detach", base)
    Path(d, name).write_text(text, encoding="utf-8")
    git(d, "add", "-A")
    git(d, "commit", "-qm", name)
    return git(d, "rev-parse", "HEAD")


def log_for(t: tk.Trunk, base: str, sha: str) -> str:
    return f"CHECKED {sha} {t.patch_id(base, sha)}\nunit ok\nGATES OK\n"


with tempfile.TemporaryDirectory() as d:
    repo, st = Path(d) / "repo", Path(d) / "state"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    Path(repo, "README").write_text("x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "root")
    root = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "integ")
    git(repo, "branch", "verified")
    t = tk.Trunk(repo, st)

    a = commit(repo, "a.txt", "a\n", root)
    check("refused without GATES OK", t.push(a, root, f"CHECKED {a} x\n")[0] == tk.REFUSED)
    code, rid = t.push(a, root, log_for(t, root, a), name="A")
    check("a checked push lands", code == tk.PUSHED, rid)
    check("main follows the trunk at once", t.rev("main") == a == t.rev("integ"))
    b = commit(repo, "b.txt", "b\n", root)
    check("a stale base is a RACE", t.push(b, root, log_for(t, root, b))[0] == tk.RACE)
    b2 = commit(repo, "b.txt", "b\n", a)
    check("a clean rebase with the same patch-id keeps its checks", t.push(b2, a, log_for(t, root, b))[0] == tk.PUSHED)
    c = commit(repo, "a.txt", "a changed\n", root)
    c2 = commit(repo, "a.txt", "a changed\n", b2)
    check("a changed patch-id is RECHECK", t.push(c2, b2, log_for(t, root, c))[0] == tk.RECHECK)
    pushes, bad = t.pushes()
    check("pushes after verified carry receipts", [s for s, _ in pushes] == [a, b2] and not bad, (pushes, bad))

    git(repo, "update-ref", "refs/heads/integ", commit(repo, "sneak.txt", "s\n", b2), b2)
    _, bad = t.pushes()
    check("a commit with no receipt is unrecorded", len(bad) == 1, bad)
    r = t.verify(lambda sha: True)
    check("the verifier reverts an unrecorded commit before any gate",
          r.unrecorded == bad and not r.verified and "sneak.txt" not in git(repo, "ls-tree", "--name-only", "integ"), r)

    r = t.verify(lambda sha: True)
    check("green: verified takes the longest prefix", r.verified == t.rev("integ") == t.rev("verified"), r)

    head = t.rev("integ")
    shas = []
    for i in range(4):
        s = commit(repo, f"f{i}.txt", f"{i}\n", head)
        check(f"push f{i}", t.push(s, head, log_for(t, head, s), name=f"f{i}")[0] == tk.PUSHED)
        shas.append(s)
        head = s
    bad_sha = shas[2]
    gate = lambda sha: not subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", bad_sha, sha]  # noqa: E731
                                          ).returncode == 0 or "f2.txt" not in git(repo, "ls-tree", "--name-only", sha)
    r1 = t.verify(gate)
    check("red at 4, green at 2: verified moves to the good half", r1.verified == shas[1], r1)
    r2 = t.verify(gate)
    check("the red push is isolated and reverted", r2.culprit == bad_sha and "f2.txt" not in git(
        repo, "ls-tree", "--name-only", "integ"), r2)
    check("main follows the revert", t.rev("main") == t.rev("integ"))
    r3 = t.verify(gate)
    check("after the revert the rest verifies (prefix closed over the revert)", r3.verified == t.rev("integ"), r3)

    s = commit(repo, "g.txt", "g\n", t.rev("integ"))
    t.push(s, t.rev("integ"), log_for(t, t.rev("integ"), s))
    before = t.rev("verified")
    r4 = t.verify(lambda sha: None)
    check("no verdict moves nothing and reverts nothing", r4.no_verdict and t.rev("verified") == before
          and t.rev("integ") == s, r4)

    sneak = commit(repo, "sneak2.txt", "s\n", s)
    git(repo, "update-ref", "refs/heads/integ", sneak, s)
    t._record(tk.Receipt(f"unrecorded@{sneak[:12]}", s, sneak, "unrecorded"))  # its revert never landed
    _, bad = t.pushes()
    check("an unrecorded receipt without its revert is still unrecorded", bad == [sneak], bad)
    orig, t.revert = t.revert, lambda specs, reason: ""  # the revert fails
    r5 = t.verify(lambda sha: True)
    t.revert = orig
    check("a failed revert never lets the commit be gated or verified", t.rev("verified") == before
          and not r5.verified, r5)
    (st / "hold.txt").write_text("revert failed\n", encoding="utf-8")
    r6 = t.verify(lambda sha: True)
    check("a hold stops the verifier round", bool(r6.held) and t.rev("verified") == before, r6)
    t.unhold("reverted by hand")
    t.revert([sneak], "unrecorded push")
    check("after its revert lands the chain verifies", t.verify(lambda sha: True).verified == t.rev("integ"))
    s = t.rev("integ")

    (st / "hold.txt").write_text("test hold\n", encoding="utf-8")
    h = commit(repo, "h.txt", "h\n", s)
    check("a hold stops lane pushes", t.push(h, s, log_for(t, s, h))[0] == tk.HELD)
    t.unhold("fixed")
    check("unhold lets pushes through", t.push(h, s, log_for(t, s, h))[0] == tk.PUSHED)

print("ALL PASS" if not FAILS else f"FAILED {FAILS}")
sys.exit(1 if FAILS else 0)
