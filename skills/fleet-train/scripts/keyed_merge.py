"""The `keyed` git merge driver: a three-way merge of a structured spec file unit by unit (a table row, a bullet, a
Cargo `[[test]]` entry), so two branches that each add or edit their own units never conflict.

  .gitattributes:  spec/FUNCTIONS.md merge=keyed
  .git/config:     merge.keyed.driver = py -3 <harness>/keyed_merge.py %O %A %B %L %P   (harness.ensure_merge_drivers)

A file splits into ordered units (SPLITTERS, by extension):
  .md    a table row is one unit keyed by its first cell within its table (heading and header line); a table's header
         and separator line are one unit; a bullet with its indented continuation lines is one unit; any other line is
         one unit.
  .toml  a `[[test]]` block through its trailing blank lines is one unit keyed by its `name`; any other line is one.
Keyed units match the base by key, the others align by content between them (an equal-length run of changed unkeyed
units is an edit of each). Per base unit, a change on one side wins; both sides changing it differently (edit vs edit,
edit vs delete) leaves conflict markers around that unit only. An added unit goes after the unit before it on its
side, ours before theirs at one spot; a key both sides add with different text, or both sides replacing the same spot
with different units, is a conflict there, never two units. Exit 0 clean, 1 with conflicts.

A key a file holds twice (a row id listed twice) is keyed by its occurrence. Anything the units cannot express
(another extension, conflict markers in an input, a keyed unit that moved, a result holding a key more often than
an input, any error) falls back to git's text merge and exit 1 (lineset_merge.text_merge). Where the driver is not
configured (a cloud clone) git uses its text merge by itself.
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
    key: Key | None  # None: an unkeyed unit, aligned by its content
    lines: Lines


class Unmergeable(Exception):
    """The input does not split into units with unique keys; git's text merge decides."""


HEADING = re.compile(r"#{1,6}\s")
SEPARATOR = re.compile(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
BULLET = re.compile(r"([-*+]|\d+[.)])\s")
MARKER = re.compile(r"(<{7}|={7}|>{7})( |$)")
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
    """The units of a file, or Unmergeable when its format has no splitter or it holds conflict markers. A key the
    file holds more than once (a row id listed twice) carries its occurrence, so every key is unique."""
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
    """How often each key (without its occurrence) is held."""
    return Counter(u.key[:-1] for u in units if u.key is not None)


@dataclass
class Side:
    """One side's change against the base."""
    state: list[Lines | None]  # per base unit: its lines on this side, None when this side deleted it
    added: dict[int, list[Unit]] = field(default_factory=dict)  # gap g (after base unit g; -1 = first): units added
    replaces: set[int] = field(default_factory=set)  # gaps whose additions stand where this side deleted base units


def align(base: list[Unit], side: list[Unit]) -> dict[int, int]:
    """side index -> base index of every side unit that is a base unit (one key, or an equal or edited unkeyed unit)."""
    def sig(u: Unit) -> tuple:
        return ("k", u.key) if u.key is not None else ("u", u.lines)

    sm = difflib.SequenceMatcher(None, [sig(u) for u in base], [sig(u) for u in side], autojunk=False)
    match: dict[int, int] = {}
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        unkeyed = all(u.key is None for u in base[i1:i2] + side[j1:j2])
        if op == "equal" or (op == "replace" and i2 - i1 == j2 - j1 and unkeyed):
            match.update(zip(range(j1, j2), range(i1, i2)))
    at = {u.key: i for i, u in enumerate(base) if u.key is not None}
    for j, u in enumerate(side):
        if j not in match and u.key in at:
            match[j] = at[u.key]
    order = [match[j] for j in sorted(match)]
    if any(a >= b for a, b in zip(order, order[1:])):
        raise Unmergeable("a keyed unit moved")
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
    """The merged lines and the number of conflicts left in them as markers."""
    o, t = view(base, ours), view(base, theirs)
    out: list[str] = []
    conflicts = 0

    def conflict(a: Lines | list[str], b: Lines | list[str]) -> None:
        nonlocal conflicts
        conflicts += 1
        out.extend(["<" * marker + " ours", *a, "=" * marker, *b, ">" * marker + " theirs"])

    ours_added = {u.key: u for us in o.added.values() for u in us if u.key is not None}
    clash: dict[Key, Unit] = {}  # a key both sides added with different text: the conflict stands at ours' place
    theirs_added: dict[int, list[Unit]] = {}
    for g, us in t.added.items():
        if [u.lines for u in us] == [u.lines for u in o.added.get(g, [])]:
            continue  # both sides added the same units at one spot
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
        if any(n > max(ids(u)[k] for u in sides) for k, n in held.items()):
            raise Unmergeable("the result holds a key more often than any input")
    except Unmergeable:
        return text_merge(base, ours, theirs)
    except Exception:  # noqa: BLE001 - a driver that dies leaves ours unmerged without markers; git's merge decides
        traceback.print_exc()
        return text_merge(base, ours, theirs)
    text = eol.join(out) + (eol if out and (tail or not a) else "")
    Path(ours).write_bytes(text.encode("utf-8"))
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
