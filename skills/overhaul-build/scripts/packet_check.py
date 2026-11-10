"""packet_check.py: one unit = one function, sized to one agent (overhaul-build R9 and F7). Two modes:

  py -3 packet_check.py brief <brief.md> [--repo <repo>] [--budget 60000]
      The dispatcher runs it on every brief it writes and drops any brief that fails. Required reading = the brief +
      every file named on its `Read:` lines (INDEX.md files, the owner files it edits); tokens ~ bytes / 4.
      Over budget: the plan must split the unit (F394a, F394b), never a bigger agent.
  py -3 packet_check.py diff <brief.md> [--repo <repo>] [--base origin/main]
      The check CLI and the gate's solo pre-check run it on every PR. Every changed file matches a glob on the
      brief's `Allowed:` line, and changed lines (added + removed, tests included) stay under its `Lines:` cap
      (default 300). (The PR's base being main is checked by the gate pre-check and land-keeper.)
Exit 0 pass, 1 fail with one line per breach.
"""
import fnmatch
import os
import re
import subprocess
import sys


def field(text, name):
    vals = []
    for m in re.finditer(rf"^{name}:\s*(.+)$", text, re.M | re.I):
        vals += [v.strip().strip("`") for v in re.split(r"[,\s]+", m.group(1)) if v.strip().strip("`")]
    return vals


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, errors="replace").stdout


def brief_mode(path, repo, budget):
    text = open(path, encoding="utf-8", errors="replace").read()
    total, bad = len(text.encode()), []
    for f in field(text, "Read"):
        p = os.path.join(repo, f)
        if os.path.isfile(p):
            total += os.path.getsize(p)
        else:
            bad.append(f"Read file missing: {f}")
    tokens = total // 4
    if tokens > budget:
        bad.append(f"required reading {tokens} tokens > budget {budget}: split the unit in the plan")
    if not field(text, "Allowed"):
        bad.append("no Allowed: line")
    if not re.search(r"^Test:\s*\S+::\S+", text, re.M):
        bad.append("no Test: <file>::<test> line")
    return bad, f"reading {tokens} tokens of {budget}"


def diff_mode(path, repo, base):
    text = open(path, encoding="utf-8", errors="replace").read()
    allowed = field(text, "Allowed")
    cap = int((field(text, "Lines") or ["300"])[0])
    names = [n for n in git(repo, "diff", "--name-only", f"{base}...HEAD").splitlines() if n.strip()]
    bad = [f"outside Allowed: {n}" for n in names if not any(fnmatch.fnmatch(n, g) for g in allowed)]
    changed = 0
    for line in git(repo, "diff", "--numstat", f"{base}...HEAD").splitlines():
        a, d = (line.split("\t") + ["0", "0"])[:2]
        changed += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    if changed > cap:
        bad.append(f"{changed} changed lines > cap {cap}: the plan splits the unit")
    return bad, f"{len(names)} files, {changed} lines of {cap}"


def main():
    a = sys.argv[1:]
    if len(a) < 2 or a[0] not in ("brief", "diff"):
        print(__doc__)
        return 2
    repo = a[a.index("--repo") + 1] if "--repo" in a else "."
    if a[0] == "brief":
        budget = int(a[a.index("--budget") + 1]) if "--budget" in a else 60000
        bad, summary = brief_mode(a[1], repo, budget)
    else:
        bad, summary = diff_mode(a[1], repo, a[a.index("--base") + 1] if "--base" in a else "origin/main")
    for b in bad:
        print("FAIL", b)
    print(("PASS " if not bad else "FAIL ") + summary)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
