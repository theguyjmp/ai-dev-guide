"""The `lineset` git merge driver: a three-way merge of a line-set data file (an allowlist, a held-row list), where
each line is one entry and order carries no meaning.

  .gitattributes:  tools/cloud/held-rows.txt merge=lineset
  .git/config:     merge.lineset.driver = py -3 <harness>/lineset_merge.py %O %A %B %P   (harness.ensure_merge_drivers)

A line stays when both sides keep it or one side adds it; a line either side removed stays removed. git's text merge
instead conflicts whenever two branches touch adjacent lines (two rows each shrinking a different line of a 3-line
allowlist), and `merge=union` would resurrect the removed lines. Counted `<count> <key>` / `<key> <count>` entries
merge three-way by count (merge). Falls back to git's own text merge (conflict markers, exit 1) when the merge would be
ambiguous: the file is not a `.txt` data file (never source code), a count grows, or both sides add one key with
different counts. Where the driver is not configured (a cloud clone) git falls back to its text merge by itself.
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


def counts(lines: list[str]) -> dict[str, int]:
    """{key: count} of the counted entries in lines."""
    out = {}
    for line in lines:
        k = count_key(line)
        if k is not None:
            out[k] = int(next(t for t in line.split() if t.isdigit()))
    return out


def merge(base: list[str], ours: list[str], theirs: list[str]) -> list[str] | None:
    """The merged lines (ours' order, then theirs' additions), or None when the merge is ambiguous.

    Counted entries (`<key> <count>`) merge by count, three-way: each side's change from base adds up
    (base 3, ours 2, theirs 2 -> 1: both sides typed one key each). A key either side removed, or whose merged count
    reaches 0, is removed. Identical changed lines used to collapse to one line and undercount the shrink (11-22)."""
    cb, co, ct = counts(base), counts(ours), counts(theirs)
    merged = {}
    for k in set(cb) | set(co) | set(ct):
        if k not in cb:  # added on a side: keep whichever side added it (both: must agree)
            if k in co and k in ct and co[k] != ct[k]:
                return None
            merged[k] = co.get(k, ct.get(k))
            continue
        if k not in co or k not in ct:
            continue  # removed on a side
        if co[k] > cb[k] or ct[k] > cb[k]:
            return None  # a count that grows is not a shrink-only change: let git conflict
        n = cb[k] + (co[k] - cb[k]) + (ct[k] - cb[k])
        if n > 0:
            merged[k] = n
    o, b = set(base), set(theirs)
    plain = lambda ls: [l for l in ls if count_key(l) is None]
    kept = [line for line in plain(ours) if not (line in o and line not in b)]
    have = set(kept)
    added = [line for line in plain(theirs) if line not in o and line not in have]
    out, seen = [], set()
    for line in ours + theirs:  # ours' order first, then theirs' new keys
        k = count_key(line)
        if k is None:
            if line in kept or line in added:
                if line not in seen or not line.strip():
                    out.append(line)
                seen.add(line)
            continue
        if k in merged and k not in seen:
            tok = line.split()
            out.append(f"{k} {merged[k]}" if not tok[0].isdigit() else f"{merged[k]} {k}")
            seen.add(k)
    return out


def read(path: str) -> tuple[list[str], str, bool]:
    """Lines of path, its line ending and whether it ends in one."""
    text = Path(path).read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in text else "\n"
    return text.splitlines(), eol, text.endswith(("\n", "\r"))


def text_merge(base: str, ours: str, theirs: str) -> int:
    """git's own three-way text merge into ours (conflict markers): 0 when clean, else 1."""
    r = subprocess.run(["git", "merge-file", "-L", "ours", "-L", "base", "-L", "theirs", ours, base, theirs], check=False)
    return 0 if r.returncode == 0 else 1


def main(argv: list[str]) -> int:
    base, ours, theirs, path = argv[1:5]
    if not path.endswith(".txt"):
        return text_merge(base, ours, theirs) or 1  # ambiguous: a conflict even when clean
    (b, _, _), (a, eol, tail), (t, _, _) = read(base), read(ours), read(theirs)
    out = merge(b, a, t)
    if out is None:
        return text_merge(base, ours, theirs) or 1  # ambiguous: a conflict even when clean
    text = eol.join(out) + (eol if out and (tail or not a) else "")
    Path(ours).write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
