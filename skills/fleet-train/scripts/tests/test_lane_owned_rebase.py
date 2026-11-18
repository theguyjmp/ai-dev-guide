"""cloud.lane_owned: a conflicting CLEAN-* row (its fix lane owns and restacks it) is never sent to the cloud rebasers
(send_rebase refuses it, one rebase_skip_lane_owned per row and sha); a conflicting spec row still is. A lane-owned
row's rebased answer is never taken, and its stale rebase marks and refs are dropped. Temp repo, temp bare origin,
temp state.

  py -3 tests/test_lane_owned_rebase.py
"""
from __future__ import annotations

import os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
FAILS: list[str] = []


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True, check=True).stdout.strip()


def branch(repo, name, text):
    git(repo, "checkout", "-q", "-b", name, "main")
    (repo / "a.txt").write_text(text, encoding="utf-8")
    git(repo, "commit", "-q", "-am", f"{name}: work")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return sha


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    origin, repo = root / "origin.git", root / "repo"
    git(root, "init", "-q", "--bare", str(origin))
    git(root, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.email", "t@t.invalid")
    git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("0", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "remote", "add", "origin", str(origin))
    lane, plan, row = branch(repo, "CLEAN-api2", "lane"), branch(repo, "CLEAN-plan-9", "plan"), branch(repo, "R1", "row")
    (repo / "a.txt").write_text("main", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "main moves")  # every row now conflicts with main
    home = root / "home"
    (home / "state").mkdir(parents=True)
    os.environ.update(HARNESS_HOME=str(home), HARNESS_WT=str(root / "wt"), HARNESS_REPO=str(repo))
    sys.path.insert(0, str(HERE))
    import harness as h  # noqa: E402
    import cloud  # noqa: E402

    def remote(r):
        return git(origin, "for-each-ref", "--format=%(objectname)", f"refs/heads/cloud/rebase-{r}")

    def skips(r):
        return [e for e in h.read_events() if e.get("event") == "rebase_skip_lane_owned" and e.get("row") == r]

    check("lane_owned: CLEAN-* yes, spec rows no",
          cloud.lane_owned("CLEAN-api2") and cloud.lane_owned("CLEAN-plan-9") and not cloud.lane_owned("R1"))

    h.save("queue.json", [{"row": "CLEAN-api2", "sha": lane, "ts": 1}, {"row": "CLEAN-plan-9", "sha": plan, "ts": 2},
                          {"row": "R1", "sha": row, "ts": 3}])
    st = {"rows": {}}
    check("export_conflicts sends only the spec row", cloud.export_conflicts({}, st) == 1, st)
    check("spec row pushed and recorded", remote("R1") == row and st["rows"]["R1"]["rebase_sent"] == row)
    check("CLEAN rows not pushed, no rebase_sent", remote("CLEAN-api2") == "" and remote("CLEAN-plan-9") == ""
          and all("rebase_sent" not in st["rows"].get(r, {}) for r in ("CLEAN-api2", "CLEAN-plan-9")))
    check("one rebase_skip_lane_owned per row", len(skips("CLEAN-api2")) == 1 and len(skips("CLEAN-plan-9")) == 1)
    cloud.export_conflicts({}, st)
    check("same row and sha: no second event", len(skips("CLEAN-api2")) == 1)

    st["rows"]["CLEAN-api2"]["rebase_sent"] = lane  # sent before lane_owned existed
    hd = {"rebase-CLEAN-api2": lane, "rebase-claim-CLEAN-plan-9": plan, "rebase-R1": row}
    gone = []
    cloud.drop_lane_owned_rebases(hd, st, gone)
    check("stale rebase_sent of a CLEAN row cleared, the spec row's kept",
          "rebase_sent" not in st["rows"]["CLEAN-api2"] and st["rows"]["R1"]["rebase_sent"] == row)
    check("CLEAN rebase refs deleted, spec row's kept", sorted(gone) == ["rebase-CLEAN-api2", "rebase-claim-CLEAN-plan-9"],
          gone)
    st["rows"]["CLEAN-api2"]["rebase_sent"] = lane
    check("take_rebased never swaps a CLEAN row's sha", not cloud.take_rebased("CLEAN-api2", row, st)
          and [q["sha"] for q in h.load("queue.json", []) if q["row"] == "CLEAN-api2"] == [lane])

sys.exit(1 if FAILS else 0)
