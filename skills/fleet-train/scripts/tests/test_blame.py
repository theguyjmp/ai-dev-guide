"""Blame by file (harness.blame_by_file, the one blame owner): a row's own files are what its train merge adds over the
rows merged ahead of it, so a row stacked on a queued row (even on its pre-restack commits) is never blamed for the
parent's files; an assert_eq! diff names only the entries that differ between its sides; more than one suspect is
bisected, never ejected together (pilot: a whole stack ejected for one row). Temp repos and stubs only.

  py -3 tests/test_blame.py
"""
from __future__ import annotations

import os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="test-blame-"))
os.environ.update({"HARNESS_HOME": str(TMP / "home"), "HARNESS_REPO": str(TMP / "repo"), "HARNESS_WT": str(TMP / "wt")})
sys.path.insert(0, str(HERE))
import harness as h  # noqa: E402

FAILS: list[str] = []


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def git(root, *a):
    return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def commit(root, files, msg):
    for f, text in files.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", msg)
    return git(root, "rev-parse", "HEAD")


if True:
    root, train = h.REPO, h.TRAIN
    root.mkdir(parents=True, exist_ok=True)
    h.ST.mkdir(parents=True, exist_ok=True)
    train.parent.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    commit(root, {f"crates/x/src/{n}.rs": n for n in "abcde"}, "base")
    git(root, "checkout", "-q", "-b", "p")
    p1 = commit(root, {"crates/x/src/a.rs": "a1"}, "P: a")
    p2 = commit(root, {"crates/x/src/b.rs": "b1"}, "P: b (restack)")  # P's queued tip
    git(root, "checkout", "-q", "-b", "c", p1)  # C stacked on P's pre-restack commit
    c = commit(root, {"crates/x/src/c.rs": "c1"}, "C: c")
    git(root, "checkout", "-q", "-b", "d", "main")
    dd = commit(root, {"crates/x/src/d.rs": "d1", "crates/x/src/e.rs": "e1"}, "D: d e")
    git(root, "checkout", "-q", "-b", "e", "main")
    ee = commit(root, {"crates/x/src/e.rs": "e2 conflicting", "crates/x/src/b.rs": "b"}, "E: e")
    git(root, "checkout", "-q", "main")
    merged = [{"row": "P", "sha": p2}, {"row": "C", "sha": c}, {"row": "D", "sha": dd}]
    git(root, "worktree", "add", "-q", "--detach", str(train), "main")
    for q in merged:
        git(train, "merge", "-q", "--no-ff", "--no-edit", "-m", f"train: land {q['row']}", q["sha"])
    git(train, "commit", "-q", "--allow-empty", "-m", "train: rustfmt")

    path = lambda n: f"crates/x/src/{n}.rs"

    own = h.own_files(merged, train)
    check("own files are the delta over the rows ahead", own == {"P": {path("a"), path("b")}, "C": {path("c")},
                                                              "D": {path("d"), path("e")}}, own)
    check("an entry the chain lacks falls back to main...sha", h.own_files([{"row": "Z", "sha": c}], train)["Z"]
          == {path("a"), path("c")})

    log = f"error[E0308]: mismatched types\n  --> {path('a')}:3:5\nfailing crates: \n"
    guilty, named, _ = h.blame_by_file(merged, {}, ["unit"], log, train)
    check("a file the parent changed blames the parent, not the stacked row", [q["row"] for q in guilty] == ["P"],
          [q["row"] for q in guilty])

    amap = lambda *ns: "{" + ", ".join(f'"{path(n)}": 1' for n in ns) + "}"
    log = (f"thread 'ratchet::tests::t' panicked at crates\\x\\src\\ratchet_tests.rs:1:5:\nassertion `left == right` failed\n"
           f"  left: {amap('a', 'b', 'c', 'd')}\n right: {amap('a', 'b', 'd')}\nfailing crates: crates/x/\n")
    check("assert diff names the differing entry only", h.assert_diff_files(log) == {path("c")}, h.assert_diff_files(log))
    guilty, named, _ = h.blame_by_file(merged, {}, ["unit"], log, train)
    check("assert diff blames only the row adding it", [q["row"] for q in guilty] == ["C"], [q["row"] for q in guilty])
    check("value change and set member differ", h.assert_diff_files(
        f'  left: {{"{path("a")}": 1, "{path("b")}": 2}}\n right: {{"{path("a")}": 1, "{path("b")}": 3}}\n'
        f'  left: {{"{path("c")}", "{path("d")}"}}\n right: {{"{path("d")}"}}\n') == {path("b"), path("c")})
    check("a side cut off by the log tail names nothing", h.assert_diff_files(
        f'  left: {amap("a", "c")}\n right: {{"{path("a")}"') == set())

    # two suspects: no eject, the existing bisect lands the halves (gate stubbed green)
    rejected, events, gated = [], [], []
    h.reject = lambda q, why, locked=False: rejected.append(q["row"])
    h.event = lambda name, **kw: events.append(name)
    h.quarantine = type("Q", (), {"heal": staticmethod(lambda *a: False)})
    h.build_train = lambda batch, rows: (list(batch), [])
    red = []  # the next gate's log: red once, then green

    def gate(ids, rows, quick=False):
        gated.append(sorted(ids))
        return (False, ["unit"], red.pop()) if red else (True, [], "")
    h.gate = gate
    h.finish_spec = h.promote = lambda *a: None
    log = f"error: cannot find value\n  --> {path('e')}:1:1\n"  # D and E both own e.rs
    two = merged[2:] + [{"row": "E", "sha": ee}]
    git(train, "merge", "-q", "--no-ff", "--no-edit", "-X", "theirs", "-m", "train: land E", ee)
    rows = {r: {"id": r, "tests": [], "file_list": [], "status": "cleanup", "deps": []} for r in ("D", "E")}
    red.append(log)
    landed = h.try_batch(two, rows, reviewed=True)
    check("two suspects are bisected, never ejected together", not rejected and "blame_suspects" in events
          and [q["row"] for q in landed] == ["D", "E"], rejected, events, gated[1:])
    rejected.clear(), gated.clear()
    rows.update({r: dict(rows["D"], id=r) for r in ("P", "C")})
    red.append(f"  --> {path('c')}:1:1")
    landed = h.try_batch(merged, rows, reviewed=True)
    check("one suspect is ejected, the rest gated again", rejected == ["C"] and gated[1:] == [["D", "P"]]
          and [q["row"] for q in landed] == ["P", "D"], rejected, gated)
    git(root, "worktree", "remove", "--force", str(train))

print("FAILED: " + ", ".join(FAILS) if FAILS else "OK")
sys.exit(1 if FAILS else 0)
