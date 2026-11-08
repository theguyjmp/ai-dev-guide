"""anchors.py: planned tests are anchors (overhaul v3). Python standard library only.

    py -3 anchors.py --repo R --base <main sha> --head <branch sha> [--functions spec/FUNCTIONS.md] [--plan <sha>]

A test file (under tests/, named test_*.py or *_test.py, or any .rs file there) that exists in the plan commit may not be modified, renamed or
deleted by a builder branch; new test files are fine. The plan commit is overhaul.toml [probe] plan_commit at --base,
else the first commit that added the FUNCTIONS file. Prints each touched anchor and exits 1, or exits 0.
land.py calls touched_anchors() for every land, polish and probe fix included.
"""
import argparse
import re
import subprocess
import sys
import tomllib


def property_file(fid, suffix=".py"):
    """The plan's property tests for function `fid`: tests/property/test_<id>.py (F01 -> test_f01.py); Rust plans
    pass suffix=".rs" (proptest)."""
    return f"tests/property/test_{re.sub(r'[^a-z0-9]+', '_', fid.lower()).strip('_')}{suffix}"


def git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", check=False)
    return p.stdout if p.returncode == 0 else ""


def is_test(path):
    """A test file: tests/**/test_*.py or tests/**/*_test.py, or any tests/**.rs (cargo test targets and helpers)."""
    name = path.rsplit("/", 1)[-1]
    if path.startswith("tests/") and name.endswith(".rs"):
        return True
    return path.startswith("tests/") and name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def plan_commit(repo, base, functions="spec/FUNCTIONS.md"):
    try:
        conf = tomllib.loads(git(repo, "show", f"{base}:overhaul.toml"))
    except tomllib.TOMLDecodeError:
        conf = {}
    pinned = str((conf.get("probe") or {}).get("plan_commit") or "").strip()
    if pinned:
        return pinned
    added = git(repo, "log", "--diff-filter=A", "--format=%H", "--reverse", base, "--", functions).split()
    return added[0] if added else ""


def touched_anchors(repo, base, head, functions="spec/FUNCTIONS.md", plan=""):
    """Planned test files the branch head modifies, renames or deletes relative to base."""
    plan = plan or plan_commit(repo, base, functions)
    if not plan:
        return []
    planned = {p for p in git(repo, "ls-tree", "-r", "--name-only", plan, "--", "tests").splitlines() if is_test(p)}
    out = []
    for line in git(repo, "diff", "--name-status", "-M", f"{base}...{head}").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0][:1] in "MDR" and parts[1] in planned:
            out.append(parts[1])
    return sorted(set(out))


def main(argv=None):
    ap = argparse.ArgumentParser(description="planned tests are anchors")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--functions", default="spec/FUNCTIONS.md")
    ap.add_argument("--plan", default="")
    a = ap.parse_args(argv)
    bad = touched_anchors(a.repo, a.base, a.head, a.functions, a.plan)
    for p in bad:
        print(p)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
