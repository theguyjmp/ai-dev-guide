"""lineset_merge.py set-merges the repo's line-set data files (2026-11-18: two rows each shrinking a different line of
an allowlist conflicted in 6 queued rows). Temp repo only.

  py -3 tests/test_lineset_merge.py [harness file, default harness.py]
"""
from __future__ import annotations

import importlib.util, os, subprocess, sys, tempfile
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
HARNESS = Path(sys.argv[1]) if len(sys.argv) > 1 else HOME / "harness.py"
FAILS: list[str] = []
sys.path.insert(0, str(HOME))
import lineset_merge as lm  # noqa: E402


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def git(root, *a, ok=True):
    r = subprocess.run(["git", *a], cwd=root, capture_output=True, text=True)
    if ok and r.returncode:
        raise RuntimeError(r.stderr)
    return r


def commit(root, branch, parent, files, msg):
    if parent:
        git(root, "checkout", "-q", "-B", branch, parent)
    for f, text in files.items():
        (root / f).write_text(text, newline="\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)
    return git(root, "rev-parse", "HEAD").stdout.strip()


def merge_tree(root, a, b):
    """(exit code, tree) of git merge-tree a b with the attributes of a."""
    r = git(root, f"--attr-source={a}", "merge-tree", "--write-tree", "--no-messages", a, b, ok=False)
    return r.returncode, r.stdout.split()[0] if r.stdout.split() else ""


# the set merge itself
check("each side's removal stays removed", lm.merge(["1 a", "1 b", "1 c"], ["1 b"], ["1 a", "1 c"]) == [])
check("both sides' additions kept, ours first", lm.merge(["R1"], ["R1", "R2"], ["R1", "R3"]) == ["R1", "R2", "R3"])
check("one side's count change taken", lm.merge(["2 a", "1 b"], ["1 a", "1 b"], ["2 a"]) == ["1 a"])
check("two counts for one key refused", lm.merge(["1 a"], ["2 a"], ["3 a"]) is None)
check("path-first counts keyed too", lm.merge(["x.rs 1"], ["x.rs 2"], ["x.rs 3"]) is None)
check("comment lines are no counted entries", lm.count_key("# 2 lines") is None and lm.count_key("R4627") is None)

with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    os.environ.update(HARNESS_HOME=str(HOME), HARNESS_STATE=str(root / "state"), HARNESS_WT=str(root / "wt"), HARNESS_REPO=str(root))
    (root / "state").mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    spec = importlib.util.spec_from_file_location("harness_under_test", HARNESS)
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    h.main_spec = lambda: {}  # the temp repo has no spec/FUNCTIONS.md
    h.ensure_merge_drivers()
    check("driver registered in the repo config", git(root, "config", "merge.lineset.driver").stdout.strip() == h.MERGE_DRIVERS["lineset"][1])

    attrs = "list.txt merge=lineset\ncode.rs merge=lineset\n"
    base = commit(root, "main", None, {".gitattributes": attrs, "list.txt": "1 a\n1 b\n1 c\n", "code.rs": "x\ny\nz\n",
                                       "s.rs": "1\n2\n3\n4\n"}, "base")
    ours = commit(root, "ours", base, {"list.txt": "1 b\n"}, "ours shrinks a and c")
    theirs = commit(root, "theirs", base, {"list.txt": "1 a\n1 c\n"}, "theirs shrinks b")
    rc, tree = merge_tree(root, ours, theirs)
    merged = git(root, "show", f"{tree}:list.txt").stdout if rc == 0 else None
    check("adjacent shrinks of a line-set file merge clean, every removal kept", rc == 0 and merged == "", rc, merged)
    git(root, "checkout", "-q", "ours")
    landed = git(root, "merge", "-q", "--no-edit", "theirs", ok=False)  # the land and restack path (a worktree merge)
    check("git merge set-merges it too", landed.returncode == 0 and (root / "list.txt").read_text() == "", landed.stderr)
    for name, text in (("o","1 a\n1 b\n1 c\n"), ("a", "1 b\n"), ("b", "1 a\n1 c\n")):
        (root / f"u.{name}").write_text(text, newline="\n")
    union = git(root, "merge-file", "-p", "--union", "u.a", "u.o", "u.b", ok=False).stdout
    check("merge=union would resurrect the removed lines (why the driver exists)", "1 a" in union and "1 b" in union, union)
    for name in "oab":
        (root / f"u.{name}").unlink()
    up = commit(root, "up", base, {"list.txt": "2 a\n1 b\n1 c\n"}, "count up")
    down = commit(root, "down", base, {"list.txt": "3 a\n1 b\n1 c\n"}, "count other")
    check("both sides changing one count is a conflict", merge_tree(root, up, down)[0] == 1)
    r1 = commit(root, "r1", base, {"code.rs": "X\ny\nz\n"}, "edit x")
    r2 = commit(root, "r2", base, {"code.rs": "x\nY\nz\n"}, "edit y")
    check("Rust source is never set-merged, even when attributes name it", merge_tree(root, r1, r2)[0] == 1)

print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
