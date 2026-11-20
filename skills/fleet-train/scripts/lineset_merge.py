"""The `lineset` git merge driver: a three-way merge of a line-set data file (an allowlist, a held-row list), where
each line is one entry and order carries no meaning.

  .gitattributes:  tools/cloud/held-rows.txt merge=lineset
  .git/config:     merge.lineset.driver = py -3 <harness>/lineset_merge.py %O %A %B %P   (harness.ensure_lineset_driver)

A line stays when both sides keep it or one side adds it; a line either side removed stays removed. git's text merge
instead conflicts whenever two branches touch adjacent lines (two rows each shrinking a different line of a 3-line
allowlist), and `merge=union` would resurrect the removed lines. Falls back to git's own text merge (conflict markers,
exit 1) when the set merge would be ambiguous: the file is not a `.txt` data file (never Rust source), or the result
holds two `<count> <key>` / `<key> <count>` lines for one key (both sides changed one entry's count). Where the driver
is not configured (a cloud clone) git falls back to its text merge by itself.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def count_key(line: str) -> str | None:
    """The key of a two-token `<count> <key>` or `<key> <count>` entry, else None (comments, single-token lines)."""
    parts = line.split()
    if len(parts) != 2 or line.lstrip().startswith("#"):
        return None
    a, b = parts
    if a.isdigit() != b.isdigit():
        return b if a.isdigit() else a
    return None


def merge(base: list[str], ours: list[str], theirs: list[str]) -> list[str] | None:
    """The merged lines (ours' order, then theirs' additions), or None when two entries would share a counted key."""
    o, b = set(base), set(theirs)
    kept = [line for line in ours if not (line in o and line not in b)]
    have = set(kept)
    added = [line for line in theirs if line not in o and line not in have]
    out = kept + added
    keys = [k for k in map(count_key, out) if k is not None]
    return None if len(keys) != len(set(keys)) else out


def read(path: str) -> tuple[list[str], str, bool]:
    """Lines of path, its line ending and whether it ends in one."""
    text = Path(path).read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in text else "\n"
    return text.splitlines(), eol, text.endswith(("\n", "\r"))


def text_merge(base: str, ours: str, theirs: str) -> int:
    """git's own three-way text merge into ours (conflict markers); exit 1 so git records the conflict."""
    subprocess.run(["git", "merge-file", "-L", "ours", "-L", "base", "-L", "theirs", ours, base, theirs], check=False)
    return 1


def main(argv: list[str]) -> int:
    base, ours, theirs, path = argv[1:5]
    if not path.endswith(".txt"):
        return text_merge(base, ours, theirs)
    (b, _, _), (a, eol, tail), (t, _, _) = read(base), read(ours), read(theirs)
    out = merge(b, a, t)
    if out is None:
        return text_merge(base, ours, theirs)
    text = eol.join(out) + (eol if out and (tail or not a) else "")
    Path(ours).write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
