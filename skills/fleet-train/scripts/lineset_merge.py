"""`lineset` git merge driver: three-way merge of an order-free line-set data file.

  .gitattributes:  tools/cloud/held-rows.txt merge=lineset
  .git/config:     merge.lineset.driver = py -3 <harness>/lineset_merge.py %O %A %B %P

A removed line stays removed (`merge=union` would resurrect it). Ambiguous: git's text merge, exit 1.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def count_key(line: str) -> str | None:
    """Key of a `<count> <key>` or `<key> <count>` line, else None."""
    parts = line.split()
    if len(parts) != 2 or line.lstrip().startswith("#"):
        return None
    a, b = parts
    if a.isdigit() != b.isdigit():
        return b if a.isdigit() else a
    return None


def counts(lines: list[str]) -> dict[str, int]:
    out = {}
    for line in lines:
        k = count_key(line)
        if k is not None:
            out[k] = int(next(t for t in line.split() if t.isdigit()))
    return out


def merge(base: list[str], ours: list[str], theirs: list[str]) -> list[str] | None:
    """Merged lines (ours' order, then theirs' additions), or None when ambiguous. Counts sum each side's delta."""
    cb, co, ct = counts(base), counts(ours), counts(theirs)
    merged = {}
    for k in set(cb) | set(co) | set(ct):
        if k not in cb:  # added: both sides must agree
            if k in co and k in ct and co[k] != ct[k]:
                return None
            merged[k] = co.get(k, ct.get(k))
            continue
        if k not in co or k not in ct:
            continue
        if co[k] > cb[k] or ct[k] > cb[k]:
            return None  # a growing count: let git conflict
        n = cb[k] + (co[k] - cb[k]) + (ct[k] - cb[k])
        if n > 0:
            merged[k] = n
    o, b = set(base), set(theirs)
    plain = lambda ls: [l for l in ls if count_key(l) is None]
    kept = [line for line in plain(ours) if not (line in o and line not in b)]
    have = set(kept)
    added = [line for line in plain(theirs) if line not in o and line not in have]
    out, seen = [], set()
    for line in ours + theirs:
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
    text = Path(path).read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in text else "\n"
    return text.splitlines(), eol, text.endswith(("\n", "\r"))


def text_merge(base: str, ours: str, theirs: str) -> int:
    """git merge-file into ours: 0 when clean, else 1."""
    r = subprocess.run(["git", "merge-file", "-L", "ours", "-L", "base", "-L", "theirs", ours, base, theirs], check=False)
    return 0 if r.returncode == 0 else 1


def main(argv: list[str]) -> int:
    base, ours, theirs, path = argv[1:5]
    if not path.endswith(".txt"):
        return text_merge(base, ours, theirs) or 1  # never clean
    (b, _, _), (a, eol, tail), (t, _, _) = read(base), read(ours), read(theirs)
    out = merge(b, a, t)
    if out is None:
        return text_merge(base, ours, theirs) or 1  # never clean
    text = eol.join(out) + (eol if out and (tail or not a) else "")
    Path(ours).write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
