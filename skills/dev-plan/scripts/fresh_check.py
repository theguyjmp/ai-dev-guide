"""Step 8 consistency check for a dev-plan spec (a fresh project, no OLD).

Usage: py -3 fresh_check.py <NEW repo dir> <FLEET dir> [--cap 5200] [--spec-check PATH]
Runs overhaul-plan's spec_check.py on the spec and drops only the misses that do
not apply to a fresh project: salvage, quarantine and the old-project fingerprint.
Prints every other miss, then a one-line summary. Exit 1 if any miss is left.
"""
import re
import subprocess
import sys
from pathlib import Path

# Misses about an old project. Anything else spec_check reports still counts.
NOT_FRESH = re.compile(r"salvag|quarantin|fingerprint|\bold[- ](project|platform|fleet|code)\b", re.I)
OLD_WORD = re.compile(r"\bOLD\b")


def find_spec_check(argv):
    if "--spec-check" in argv:
        return Path(argv[argv.index("--spec-check") + 1])
    # dev-plan/scripts/fresh_check.py -> <skills>/overhaul-plan/scripts/spec_check.py
    return Path(__file__).resolve().parents[2] / "overhaul-plan" / "scripts" / "spec_check.py"


def main():
    argv = sys.argv[1:]
    spec_check = find_spec_check(argv)
    if "--spec-check" in argv:
        i = argv.index("--spec-check")
        argv = argv[:i] + argv[i + 2:]
    if not spec_check.is_file():
        print(f"FAIL spec_check.py not found at {spec_check}")
        sys.exit(2)
    run = subprocess.run([sys.executable, "-I", str(spec_check), *argv],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if run.returncode == 2 or "SUMMARY" not in run.stdout:
        sys.stdout.write(run.stdout + run.stderr)
        print(f"FAIL spec_check.py exited {run.returncode} without a summary")
        sys.exit(2)
    kept, dropped, summary = [], [], ""
    for line in run.stdout.splitlines():
        if line.startswith("MISS "):
            (dropped if NOT_FRESH.search(line) or OLD_WORD.search(line) else kept).append(line)
        elif line.startswith("SUMMARY"):
            summary = line
    for line in kept:
        print(line)
    rest = re.sub(r"^SUMMARY misses=\d+\s*", "", summary)
    print(f"SUMMARY misses={len(kept)} dropped_not_fresh={len(dropped)} {rest}".rstrip())
    sys.exit(1 if kept else 0)


if __name__ == "__main__":
    main()
