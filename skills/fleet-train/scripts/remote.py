"""Desktop builder loop, run on the laptop: claim a row here, build it on a second machine (ssh host HARNESS_REMOTE_HOST) with `claude -p`,
fetch the branch back, submit it to the train.   py -3 remote.py --slot 100
"""
import argparse, json, subprocess, sys, time
from pathlib import Path

H = Path(__file__).parent
import os
REPO = os.environ.get("HARNESS_REPO", ".")
HOST = os.environ.get("HARNESS_REMOTE_HOST", "remote-host")
ROOT = os.environ.get("HARNESS_REMOTE_ROOT", "D:\\fleet")  # remote dir holding repo.git and vh\
LOCAL_WT = os.environ.get("HARNESS_WT", str(Path(REPO).parent / "vh"))
# cmd.exe env prefix on the remote host, e.g. "set CARGO_HOME=D:\\dev\\cargo&& set CARGO_BUILD_JOBS=3"
ENV = os.environ.get("HARNESS_REMOTE_ENV", "set CARGO_BUILD_JOBS=3")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def hz(*a):
    return run(["py", "-3", str(H / "harness.py"), *a])


BRIEF = """You are a builder for the rebuild (Rust workspace) on the desktop PC. One row, end to end.
Row claim (JSON): {claim}
Your worktree is the current directory, already on branch {branch}. Run cargo here with CARGO_TARGET_DIR={target}.
Read the row's line in spec/FUNCTIONS.md, its acceptance tests and only the source you need. If previous_branch is set,
`git merge --no-edit origin/{branch_prev}` is NOT available; ignore it. old_harness_reason and recent_rejects say why
earlier attempts failed - fix exactly that. You OWN the row: if a dependency capability is missing, a stub is in the way or
a file is outside the row's list, implement what is needed in the module where it belongs, minimal and clean, clippy -D warnings clean.
Done = each test in "tests" passes: `cargo test -q --test <stem> -- --exact <name>` (s*.rs tests: add --release if debug is
slow), then `cargo clippy -q -p <crate> --all-targets -- -D warnings` for each crate you touched. Never run the whole suite.
Never edit the status column of spec/FUNCTIONS.md or other rows' tests. About 90 tool calls max; if stuck, commit what compiles.
Finish with `git add -A && git commit -m "<row>: <what>"`. Your last line of output must be exactly DONE or BLOCKED: <reason>."""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, required=True)
    ap.add_argument("--max", type=int, default=40)
    a = ap.parse_args()
    for _ in range(a.max):
        out = hz("claim", "--slot", str(a.slot), "--wait", "540")
        try:
            c = json.loads(out.stdout)
        except ValueError:
            print(f"s{a.slot} claim error: {out.stderr[-300:]}", flush=True)
            time.sleep(60)
            continue
        if not c.get("row"):
            continue
        row, br = c["row"], c["branch"]
        wt = f"{ROOT}\\vh\\s{a.slot}"
        tgt = f"{ROOT}\\vh\\target-{a.slot}"
        run(["git", "-C", REPO, "push", "-q", "-f", "desk", f"main:refs/heads/main", f"main:refs/heads/{br}"])
        setup = (f"cd /d {ROOT}\\repo.git && (if not exist {wt}\\.git git worktree add -q -f {wt} {br})"
                 f" & cd /d {wt} && git checkout -q -f -B {br} {br} 2>nul & git -C {wt} reset -q --hard {br} & git -C {wt} clean -qfd")
        run(["ssh", HOST, setup])
        Path(H / "state" / f"remote-{a.slot}.brief").write_text(
            BRIEF.format(claim=json.dumps(c), branch=br, target=tgt, branch_prev=br), encoding="utf-8")
        run(["scp", "-q", str(H / "state" / f"remote-{a.slot}.brief"), f"{HOST}:{wt}\\..\\brief-{a.slot}.txt"])
        t0 = time.time()
        r = run(["ssh", "-o", "ServerAliveInterval=30", HOST,
                 f"{ENV}&& set CARGO_TARGET_DIR={tgt}&& cd /d {wt} && type ..\\brief-{a.slot}.txt | claude -p --dangerously-skip-permissions --max-turns 120"],
                timeout=3 * 3600)
        tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
        f = run(["git", "-C", REPO, "fetch", "-q", "desk", f"+refs/heads/{br}:refs/remotes/desk/{br}"])
        lw = f"{LOCAL_WT}/s{a.slot}"
        run(["git", "-C", lw, "reset", "-q", "--hard", f"desk/{br}"])
        ahead = run(["git", "-C", REPO, "rev-list", "--count", f"main..desk/{br}"]).stdout.strip()
        if tail[0].startswith("DONE") and ahead not in ("", "0"):
            s = hz("submit", "--slot", str(a.slot), "--row", row)
            print(f"s{a.slot} {row} submitted {round(time.time()-t0)}s {s.stdout.strip()[:80]} {s.stderr.strip()[:120]}", flush=True)
        else:
            hz("release", "--slot", str(a.slot), "--row", row, "--reason", f"desktop builder: {tail[0][:400]} {f.stderr[:200]}")
            print(f"s{a.slot} {row} released: {tail[0][:120]}", flush=True)


if __name__ == "__main__":
    main()
