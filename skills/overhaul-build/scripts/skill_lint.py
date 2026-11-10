"""skill_lint.py: keeps overhaul skills from accreting rules kept by memory (R7).
Fails when, in SKILL.md:
  - a rule bullet ("- " line under a "## " section other than Why/Changelog) names no mechanism: `[by <script, hook,
    lint or check>]`;
  - an incident date note ("owner 2026-", "(2026-") appears: incidents go to CHANGELOG.md;
  - the file is over 200 lines.
  py -3 skill_lint.py <skill dir>...   exit 1 on any breach
"""
import os
import re
import sys

MECH = re.compile(r"\[by [^\]]+\]")
DATE = re.compile(r"owner 20\d\d-|\(20\d\d-\d\d-\d\d")


def lint(skill_dir):
    path = os.path.join(skill_dir, "SKILL.md")
    lines = open(path, encoding="utf-8").read().splitlines()
    bad, section = [], ""
    if len(lines) > 200:
        bad.append(f"{path}: {len(lines)} lines > 200")
    for i, line in enumerate(lines, 1):
        if line.startswith("## "):
            section = line[3:].strip().lower()
        if DATE.search(line):
            bad.append(f"{path}:{i}: incident note in SKILL.md; move it to CHANGELOG.md")
        if section.startswith("rules") and line.startswith("- ") and not MECH.search(line):
            bad.append(f"{path}:{i}: rule names no mechanism [by ...]")
    return bad


def main():
    bad = [b for d in sys.argv[1:] for b in lint(d)]
    for b in bad:
        print("FAIL", b)
    print("PASS" if not bad else f"FAIL {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
