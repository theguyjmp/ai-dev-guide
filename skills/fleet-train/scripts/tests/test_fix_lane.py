"""Cloud fix lane (cloud.py Lane FIX, kit FIXER.md): claim_task.sh claims one task per session, a stamped fix branch
queues as CLEAN-fix-<task> ahead of every row, unstamped, plan-stamped or unlisted ones go back to cloud/wip-fix-<task>
with the reason, silent claims recycle, landed tasks leave fix-tasks.txt. Temp repo, temp bare origin, temp state.

  py -3 tests/test_fix_lane.py
"""
from __future__ import annotations

import os, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
CLAIM_SH = HERE.parent / "cloud" / "claim_task.sh"
FAILS: list[str] = []
BASH = shutil.which("bash") or "bash"  # PATH order: on Windows Git bash, not System32 WSL (which CreateProcess finds first)
OLD = {"GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z"}


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def run(cwd, *a, env=None, ok=True):
    r = subprocess.run(list(a), cwd=str(cwd), capture_output=True, text=True, env={**os.environ, **(env or {})})
    if ok and r.returncode:
        raise RuntimeError(f"{a}: {r.stdout[-300:]} {r.stderr[-500:]}")
    return r


def git(cwd, *a, env=None):
    return run(cwd, "git", *a, env=env).stdout.strip()


def commit_on_main(repo, msg, files) -> str:
    git(repo, "checkout", "-q", "--detach", "main")
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return sha


def stamp(repo, sha, trailer) -> str:
    git(repo, "checkout", "-q", "--detach", sha)
    t = git(repo, "rev-parse", "HEAD^{tree}")
    git(repo, "-c", "trailer.ifexists=replace", "commit", "-q", "--amend", "--no-edit", "--trailer", f"{trailer}{t}")
    out = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return out


def remote(origin, ref):
    return git(origin, "for-each-ref", "--format=%(objectname)", ref) or None


def publish_harness_cloud(origin, cw, root):
    """Push the lane files collect wrote into cw as origin's harness-cloud (what sync_wt + publish do)."""
    pub = root / "pub"
    if not pub.exists():
        git(root, "init", "-q", "-b", "harness-cloud", str(pub))
    for f in cw.rglob("*"):
        if f.is_file():
            (pub / f.relative_to(cw)).parent.mkdir(parents=True, exist_ok=True)
            (pub / f.relative_to(cw)).write_bytes(f.read_bytes())
    git(pub, "add", "-A")
    git(pub, "-c", "user.email=t@t.invalid", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "publish")
    git(pub, "push", "-q", "-f", str(origin), "HEAD:refs/heads/harness-cloud")


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    origin, repo = root / "origin.git", root / "repo"
    git(root, "init", "-q", "--bare", str(origin))
    git(root, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.email", "t@t.invalid")
    git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main", "main:cloud-base")
    home = root / "home"
    (home / "state" / "fix-tasks").mkdir(parents=True)
    os.environ.update(HARNESS_HOME=str(home), HARNESS_WT=str(root / "wt"), HARNESS_REPO=str(repo))
    (home / "state" / "fix-tasks.txt").write_text("# clean-up tasks\nCU-102\nCU-103  # stubs\nnot a task\nCU-102\n",
                                                  encoding="utf-8")
    (home / "state" / "fix-tasks" / "CU-102.md").write_text("# CU-102\n\none id rule\n", encoding="utf-8")
    sys.path.insert(0, str(HERE))
    import harness as h  # noqa: E402
    import cloud  # noqa: E402
    cloud.CW.mkdir(parents=True)
    FIX = cloud.FIX
    claim_sh = CLAIM_SH

    st = {"rows": {}}
    check("fix tasks: listed ids, less malformed and repeated", cloud.fix_tasks(st) == {"CU-102": [], "CU-103": []},
          cloud.fix_tasks(st))
    out = cloud.collect_lane(FIX, cloud.heads(), st)
    tasks = (cloud.CW / "fix-tasks.txt").read_bytes()
    check("fix-tasks.txt lists the open tasks, LF", tasks == b"CU-102\nCU-103\n", tasks)
    check("each task's text is published", "one id rule" in (cloud.CW / "fix-tasks" / "CU-102.md").read_text(encoding="utf-8")
          and "ask the manager" in (cloud.CW / "fix-tasks" / "CU-103.md").read_text(encoding="utf-8"))
    script = (cloud.CW / "fix-check.sh").read_text(encoding="utf-8")
    check("fix-check.sh: the train's commands, merge check, its own stamp", h.CLIPPY_STRICT in script
          and "merge-tree --write-tree origin/cloud-base HEAD" in script and "Fix-Check: $t" in script
          and "FIX CHECK OK" in script)
    check("fix-check.sh parses", subprocess.run([BASH, "-n"], input=script.encode()).returncode == 0)
    check("placeholders become the branch's files; a short command is its own label",
          "{files}" not in cloud.session_cmd("npx eslint {files}") and "git diff --name-only" in cloud.session_cmd("x {changed}")
          and cloud.check_label("npx tsc") == "npx" and cloud.check_label("py -3 /q/dup_blocks.py src") == "dup_blocks.py")
    node = subprocess.run([sys.executable, "-c", "import cloud; print(cloud.check_script(cloud.FIX))"], cwd=str(HERE),
                          capture_output=True, text=True, env={**os.environ, "HARNESS_STACK": "node"})
    check("node stack: fix-check.sh is generated and parses", node.returncode == 0 and "FIX CHECK OK" in node.stdout
          and subprocess.run([BASH, "-n"], input=node.stdout.encode()).returncode == 0, node.stderr[-300:])
    mt = root / "mt"
    git(root, "init", "-q", "-b", "main", str(mt))
    for f, text, ref in (("a.txt", "x", None), ("a.txt", "y", "refs/remotes/origin/cloud-base")):
        (mt / f).write_text(text, encoding="utf-8")
        git(mt, "add", "-A")
        git(mt, "-c", "user.email=t@t.invalid", "-c", "user.name=t", "commit", "-q", "-m", text)
        if ref:
            git(mt, "update-ref", ref, "HEAD")
            git(mt, "reset", "-q", "--hard", "HEAD~1")
    old_git = cloud.MERGES_CLEAN.replace("--write-tree", "--no-such-option", 1)  # git < 2.38: exit 129, fallback
    merges = lambda: [run(mt, BASH, "-c", c, ok=False).returncode for c in (cloud.MERGES_CLEAN, old_git)]  # noqa: E731
    git(mt, "checkout", "-q", "-b", "side")
    (mt / "b.txt").write_text("b", encoding="utf-8")
    git(mt, "add", "-A")
    git(mt, "-c", "user.email=t@t.invalid", "-c", "user.name=t", "commit", "-q", "-m", "b")
    clean = merges()
    (mt / "a.txt").write_text("z", encoding="utf-8")
    git(mt, "-c", "user.email=t@t.invalid", "-c", "user.name=t", "commit", "-q", "-am", "z")
    check("merge check (new and old git): a clean merge passes, a conflict with cloud-base fails",
          clean == [0, 0] and merges() == [1, 1], clean)
    publish_harness_cloud(origin, cloud.CW, root)

    # claim_task.sh: two sessions take two packets, a third finds none; a WIP is resumed
    sess = {}
    for n in ("a", "b", "c"):
        sess[n] = root / f"sess-{n}"
        git(root, "clone", "-q", str(origin), str(sess[n]))
    git(root, "-C", str(sess["b"]), "config", "user.email", "s@t.invalid")
    git(root, "-C", str(sess["b"]), "config", "user.name", "s")
    wip = commit_on_main(repo, "WIP fix-CU-103", {"crates/a.rs": "wip\n"})
    git(repo, "push", "-q", "origin", f"{wip}:refs/heads/cloud/wip-fix-CU-103")
    r = {n: run(sess[n], BASH, claim_sh.as_posix(), "fix", ok=False) for n in ("a", "b", "c")}
    check("first session claims CU-102 from cloud-base", r["a"].stdout.startswith("CLAIMED CU-102 ")
          and git(sess["a"], "rev-parse", "--abbrev-ref", "HEAD") == "fix/cloud-CU-102"
          and remote(origin, "refs/heads/cloud/fix-claim-CU-102") == r["a"].stdout.split()[2], r["a"].stdout + r["a"].stderr)
    check("second session gets CU-103 and resumes its WIP", r["b"].stdout.startswith("CLAIMED CU-103 ")
          and (sess["b"] / "crates" / "a.rs").exists(), r["b"].stdout + r["b"].stderr)
    check("third session: NO TASK, exit 3", r["c"].returncode == 3 and "NO TASK" in r["c"].stdout, r["c"].stdout)
    check("a bad lane is refused", run(sess["c"], BASH, claim_sh.as_posix(), "nope", ok=False).returncode == 2)

    # collect: front of the queue, stamps, salvage
    with h.lock("state"):
        h.queue_clean("older", git(repo, "rev-parse", "main"), 5)
    seeded = h.load("queue.json", [])[0]["ts"]
    good = stamp(repo, commit_on_main(repo, "fix(store): CU-102", {"crates/ids.rs": "one\n"}), "Fix-Check: ")
    plan_stamped = stamp(repo, commit_on_main(repo, "fix: CU-103", {"crates/b.rs": "x\n"}), "Plan-Check: ")
    unlisted = stamp(repo, commit_on_main(repo, "fix: CU-777", {"crates/c.rs": "x\n"}), "Fix-Check: ")
    git(repo, "push", "-q", "origin", f"{good}:refs/heads/fix/cloud-CU-102", f"{plan_stamped}:refs/heads/fix/cloud-CU-103",
        f"{unlisted}:refs/heads/fix/cloud-CU-777")
    cloud.fetch_cloud()
    out = cloud.collect_lane(FIX, cloud.heads(), st)
    q = {x["row"]: x for x in h.load("queue.json", [])}
    check("stamped fix queued as CLEAN-fix-CU-102", q.get("CLEAN-fix-CU-102", {}).get("sha") == good
          and out["queued"] == ["CU-102"] and remote(origin, "refs/heads/fix/cloud-CU-102") is None, out)
    check("... ahead of every queued row", q["CLEAN-fix-CU-102"]["ts"] < seeded == q["CLEAN-older"]["ts"])
    check("a plan stamp does not pass a fix branch", remote(origin, "refs/heads/cloud/wip-fix-CU-103") == plan_stamped
          and "Fix-Check" in (cloud.CW / "fix-rejects" / "CU-103.md").read_text(encoding="utf-8"))
    check("an unlisted packet goes back with its reason", remote(origin, "refs/heads/cloud/wip-fix-CU-777") == unlisted
          and "not open" in (cloud.CW / "fix-rejects" / "CU-777.md").read_text(encoding="utf-8")
          and remote(origin, "refs/heads/fix/cloud-CU-777") is None)
    check("salvaged claim released, queued claim kept", "CU-103" in out["released"]
          and remote(origin, "refs/heads/cloud/fix-claim-CU-102") is not None, out)
    check("fix_status: CU-103 claimable again, CU-102 queued", st["fix_status"]["claimable"] == ["CU-103"]
          and st["fix_status"]["queued"] == ["CU-102"], st["fix_status"])

    old = git(repo, "commit-tree", "main^{tree}", "-p", "main", "-m", "claim fix CU-103 old", env=OLD)
    git(repo, "push", "-q", "-f", "origin", f"{old}:refs/heads/cloud/fix-claim-CU-103")
    git(repo, "push", "-q", "origin", "--delete", "cloud/wip-fix-CU-103")
    out = cloud.collect_lane(FIX, cloud.heads(), st)
    check("a claim silent for 2 h is released", out["released"] == ["CU-103"]
          and remote(origin, "refs/heads/cloud/fix-claim-CU-103") is None, out)

    git(repo, "merge", "-q", "--no-ff", "-m", "train: land CLEAN-fix-CU-102", good)  # the train lands it
    h.save("queue.json", [x for x in h.load("queue.json", []) if x["row"] != "CLEAN-fix-CU-102"])
    out = cloud.collect_lane(FIX, cloud.heads(), st)
    check("a landed fix frees its claim and leaves the task list", out["landed"] == ["CU-102"]
          and "CU-102" in st["fix_landed"] and remote(origin, "refs/heads/cloud/fix-claim-CU-102") is None
          and (cloud.CW / "fix-tasks.txt").read_text(encoding="utf-8").split() == ["CU-103"]
          and not (cloud.CW / "fix-tasks" / "CU-102.md").exists(), out)
print(f"{len(FAILS)} failed" if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
