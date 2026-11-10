"""`cargo fmt --all --check` in batches, for Windows' 32k command-line limit.

cargo fmt hands rustfmt every target root of a package on one command line; a package with a few hundred
[[test]] targets under a long repo path dies with os error 206 (measured 2026-11-09 on the pilot project: 189 functions,
~400 test roots). This runs the same check: every target root `cargo metadata` lists for the workspace members,
`rustfmt --check --edition <package edition>` in batches. Exit 0 when formatted, 1 on a diff, 2 on a tool error.

Usage: py -3 fmt_check.py [--repo DIR] [--batch N]
"""
import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def roots(repo: Path) -> dict[str, list[str]]:
    """Target root files by edition, relative to repo, for every workspace member (what cargo fmt --all checks)."""
    out = subprocess.run(["cargo", "metadata", "--no-deps", "--format-version", "1", "-q"], cwd=repo,
                         capture_output=True, text=True, check=True).stdout
    meta = json.loads(out)
    members = set(meta["workspace_members"])
    by_ed: dict[str, list[str]] = defaultdict(list)
    for pkg in meta["packages"]:
        if pkg["id"] not in members:
            continue
        for t in pkg["targets"]:
            p = Path(t["src_path"])
            try:
                p = p.relative_to(repo)
            except ValueError:
                pass
            by_ed[pkg.get("edition") or "2021"].append(str(p))
    return {ed: sorted(set(files)) for ed, files in by_ed.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--batch", type=int, default=60)
    a = ap.parse_args(argv)
    repo = Path(a.repo).resolve()
    try:
        groups = roots(repo)
    except (subprocess.CalledProcessError, OSError, ValueError) as e:
        print(f"fmt_check: cargo metadata failed: {e}", file=sys.stderr)
        return 2
    rc = 0
    for ed, files in groups.items():
        for i in range(0, len(files), a.batch):
            r = subprocess.run(["rustfmt", "--check", "--edition", ed, *files[i:i + a.batch]], cwd=repo)
            if r.returncode == 1:
                rc = 1
            elif r.returncode:
                print(f"fmt_check: rustfmt exit {r.returncode}", file=sys.stderr)
                return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
