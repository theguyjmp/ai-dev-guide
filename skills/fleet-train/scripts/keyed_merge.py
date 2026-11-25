"""`keyed` git merge driver: three-way merge of a spec file unit by unit, so branches editing their own units never
conflict.

  .gitattributes:  spec/FUNCTIONS.md merge=keyed
  .git/config:     merge.keyed.driver = py -3 <harness>/keyed_merge.py %O %A %B %L %P

Units (SPLITTERS): a .md table row keyed by its first cell within its table, a bullet with its continuation lines, any
other line; a .toml `[[test]]` block keyed by `name`. Per base unit one side's change wins; differing changes
conflict on that unit only. Added units follow their predecessor, ours first; a key both sides add differently
conflicts. Exit 0 clean, 1 with conflicts. A moved key, input markers, a duplicated key or an error falls back to
git's text merge.
"""
from __future__ import annotations

import difflib
import re
import sys
import traceback
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from lineset_merge import read, text_merge

Key = tuple
Lines = tuple[str, ...]


@dataclass(frozen=True)
class Unit:
    key: Key | None  # None: aligned by content
    lines: Lines


class Unmergeable(Exception):
    """git's text merge decides."""


HEADING = re.compile(r"#{1,6}\s")
SEPARATOR = re.compile(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
BULLET = re.compile(r"([-*+]|\d+[.)])\s")
MARKER = re.compile(r"(<{7,}|>{7,})( |$)|={7,}$")  # criss-cross merges nest longer ones
TEST_NAME = re.compile(r'name\s*=\s*"([^"]+)"')


def split_markdown(lines: list[str]) -> list[Unit]:
    units: list[Unit] = []
    seen: dict[tuple, int] = {}
    heading: tuple = ("", 0)
    table: tuple | None = None

    def occurrence(k: tuple) -> int:
        seen[k] = seen.get(k, -1) + 1
        return seen[k]

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("|") and i + 1 < len(lines) and SEPARATOR.match(lines[i + 1]):
            table = (heading, line, occurrence((heading, line)))
            units.append(Unit(None, (line, lines[i + 1])))
            i += 2
            continue
        if line.startswith("|") and table is not None:
            units.append(Unit(("row", table, line.split("|")[1].strip()), (line,)))
        elif BULLET.match(line):
            j = i + 1
            while j < len(lines) and lines[j][:1].isspace() and lines[j].strip():
                j += 1
            units.append(Unit(None, tuple(lines[i:j])))
            table, i = None, j
            continue
        else:
            if HEADING.match(line):
                heading = (line, occurrence(("#", line)))
            units.append(Unit(None, (line,)))
            table = None if not line.startswith("|") else table
        i += 1
    return units


def split_toml(lines: list[str]) -> list[Unit]:
    units: list[Unit] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() != "[[test]]":
            units.append(Unit(None, (lines[i],)))
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not lines[j].lstrip().startswith("["):
            j += 1
        names = [m.group(1) for line in lines[i:j] if (m := TEST_NAME.match(line.strip()))]
        if len(names) != 1:
            raise Unmergeable(f"a [[test]] block at line {i + 1} has {len(names)} names")
        units.append(Unit(("test", names[0]), tuple(lines[i:j])))
        i = j
    return units


SPLITTERS = {".md": split_markdown, ".toml": split_toml}


def split(path: str, lines: list[str]) -> list[Unit]:
    """The units of a file; a repeated key carries its occurrence, so every key is unique."""
    splitter = SPLITTERS.get(Path(path).suffix)
    if splitter is None:
        raise Unmergeable(f"no splitter for {path}")
    if any(MARKER.match(line) for line in lines):
        raise Unmergeable("conflict markers in an input")
    seen: Counter = Counter()
    units = []
    for u in splitter(lines):
        if u.key is not None:
            seen[u.key] += 1
            u = Unit((*u.key, seen[u.key] - 1), u.lines)
        units.append(u)
    return units


def ids(units: list[Unit]) -> Counter:
    """{key without occurrence: count}."""
    return Counter(u.key[:-1] for u in units if u.key is not None)


@dataclass
class Side:
    state: list[Lines | None]  # per base unit; None: deleted
    added: dict[int, list[Unit]] = field(default_factory=dict)  # gap (after base unit g; -1 = first) -> units
    replaces: set[int] = field(default_factory=set)  # gaps whose additions replace deleted units


def align(base: list[Unit], side: list[Unit]) -> dict[int, int]:
    """side index -> base index: keyed units by key as anchors, then unkeyed units between anchors by content; an
    equal-length changed unkeyed run is an edit of each."""
    at = {u.key: i for i, u in enumerate(base) if u.key is not None}
    anchors = [(j, at[u.key]) for j, u in enumerate(side) if u.key in at]
    if any(i1 >= i2 for (_, i1), (_, i2) in zip(anchors, anchors[1:])):
        raise Unmergeable("a keyed unit moved")
    match = dict(anchors)

    def sig(u: Unit) -> tuple:
        return ("k", u.key) if u.key is not None else ("u", u.lines)

    for (j0, i0), (j1, i1) in zip([(-1, -1)] + anchors, anchors + [(len(side), len(base))]):
        b, s_ = base[i0 + 1:i1], side[j0 + 1:j1]
        if not b or not s_:
            continue
        sm = difflib.SequenceMatcher(None, [sig(u) for u in b], [sig(u) for u in s_], autojunk=False)
        for op, b1, b2, s1, s2 in sm.get_opcodes():
            unkeyed = all(u.key is None for u in b[b1:b2] + s_[s1:s2])
            if op == "equal" or (op == "replace" and b2 - b1 == s2 - s1 and unkeyed):
                match.update((j0 + 1 + j, i0 + 1 + i) for j, i in zip(range(s1, s2), range(b1, b2)))
    return match


def view(base: list[Unit], side: list[Unit]) -> Side:
    match = align(base, side)
    v = Side(state=[None] * len(base))
    anchor = -1
    for j, u in enumerate(side):
        if j in match:
            anchor = match[j]
            v.state[anchor] = u.lines
        else:
            v.added.setdefault(anchor, []).append(u)
    v.replaces = {g for g in v.added if g + 1 < len(base) and g + 1 not in match.values()}
    return v


def merge(base: list[Unit], ours: list[Unit], theirs: list[Unit], marker: int = 7) -> tuple[list[str], int]:
    """(merged lines, conflict count)."""
    o, t = view(base, ours), view(base, theirs)
    out: list[str] = []
    conflicts = 0

    def conflict(a: Lines | list[str], b: Lines | list[str]) -> None:
        nonlocal conflicts
        conflicts += 1
        out.extend(["<" * marker + " ours", *a, "=" * marker, *b, ">" * marker + " theirs"])

    ours_added = {u.key: u for us in o.added.values() for u in us if u.key is not None}
    clash: dict[Key, Unit] = {}  # key added differently: conflict at ours' place
    theirs_added: dict[int, list[Unit]] = {}
    for g, us in t.added.items():
        if [u.lines for u in us] == [u.lines for u in o.added.get(g, [])]:
            continue  # same additions on both sides
        keep = []
        for u in us:
            if u.key in ours_added:
                if ours_added[u.key].lines != u.lines:
                    clash[u.key] = u
            else:
                keep.append(u)
        theirs_added[g] = keep

    def emit_added(g: int) -> None:
        oa, ta = o.added.get(g, []), theirs_added.get(g, [])
        if oa and ta and g in o.replaces and g in t.replaces:
            conflict([x for u in oa for x in u.lines], [x for u in ta for x in u.lines])
            return
        for u in oa:
            if u.key in clash:
                conflict(u.lines, clash[u.key].lines)
            else:
                out.extend(u.lines)
        for u in ta:
            out.extend(u.lines)

    emit_added(-1)
    for i, b in enumerate(base):
        x, y = o.state[i], t.state[i]
        if x == y or y == b.lines:
            out.extend(x or ())
        elif x == b.lines:
            out.extend(y or ())
        else:
            conflict(x or (), y or ())
        emit_added(i)
    return out, conflicts


def main(argv: list[str]) -> int:
    base, ours, theirs, size, path = argv[1:6]
    try:
        (b, _, _), (a, eol, tail), (t, _, _) = read(base), read(ours), read(theirs)
        sides = [split(path, x) for x in (b, a, t)]
        out, conflicts = merge(*sides, int(size))
        held = ids(split(path, out)) if not conflicts else Counter()
        most = [ids(u) for u in sides]
        if any(n > max(c[k] for c in most) for k, n in held.items()):
            raise Unmergeable("the result holds a key more often than any input")
    except Unmergeable:
        return text_merge(base, ours, theirs)
    except Exception:  # noqa: BLE001 - a dead driver leaves ours unmarked; git decides
        traceback.print_exc()
        return text_merge(base, ours, theirs)
    text = eol.join(out) + (eol if out and (tail or not a) else "")
    Path(ours).write_bytes(text.encode("utf-8"))
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
