"""Move Rust test files built in the per-file layout into their area's group target (rust_tests.py's grouped layout).

A branch made before its repo grouped its test files still adds each new test file as its own root Cargo.toml
`[[test]]` target. After it merges onto the grouped layout, this moves every such file (and every test file no target
builds, rust_tests.problem) into the group target of its folder:

- the group: a group root is a test target whose file is `group.rs` or `group_<area>.rs` in the test file's folder; a
  file belongs to the root whose prefix (its members' common lead, e.g. `test_`, then `<area>`) is the longest one its
  stem starts with. No root or two equally long ones: refused.
- the root gains `mod <stem>;` in sorted place among its test modules, and loses `mod <stem>;` lines whose file is gone
  (a rename's old name).
- the file's helper declarations (`mod entry;`, with or without `#[path = ".."]`) become `use crate::entry;` when the
  root declares that helper (one helper module per target), or go when the file never names `entry::`. A file
  declaring a module its root lacks is refused (its path would change as a module).
- its `[[test]]` block leaves Cargo.toml; a block with keys beyond name and path (harness, required-features) is
  refused.

Idempotent; all or nothing (a refusal writes no file); each file keeps its line endings. Callers stage and commit.

  py -3 rust_test_groups.py <repo>   # one line per change ("moved <file> into <root> (<target>)", "dropped ..");
                                     # exit 0 done or nothing to do, 1 refused (the reasons are printed)
"""
from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rust_src  # noqa: E402  beside this script: the token-level `mod x;` and path reads
import rust_tests  # noqa: E402  beside this script: the one map of test files to cargo targets

GROUP_FILE = re.compile(r"group(?:_(\w+))?")
MOD_LINE = re.compile(r"^mod (\w+);[ \t]*$")
PATH_MOD = re.compile(r'^#\[path\s*=\s*"([^"\n]*)"\][ \t]*\r?\n(?:[ \t]*\r?\n)*mod (\w+);', re.M)


@dataclass(frozen=True)
class Group:
    root: str  # repo-relative group root file
    target: str  # its cargo test target
    prefix: str | None  # what a member's stem starts with (None: its members share no lead, so nothing is matched)
    helpers: frozenset[str]  # the root's out-of-line modules that are not test files (e.g. entry)


@dataclass
class Result:
    moved: list[tuple[str, str, str]] = field(default_factory=list)  # (file, root, target)
    dropped: list[tuple[str, str]] = field(default_factory=list)  # (root, module whose file is gone)
    refused: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)  # repo-relative files written

    def lines(self) -> list[str]:
        return ([f"refused {r}" for r in self.refused] + [f"moved {f} into {r} ({t})" for f, r, t in self.moved]
                + [f"dropped mod {m}; from {r} (its file is gone)" for r, m in self.dropped])


def _read(p: Path) -> str:
    return p.read_bytes().decode("utf-8")


def _write(p: Path, text: str) -> None:
    p.write_bytes(text.encode("utf-8"))  # bytes: the file's own line endings stay as they are


def _nl(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _groups(repo: Path, table: dict) -> dict[str, list[Group]]:
    """{folder: [Group]} of the group roots the table holds. Each folder's member stems are `<lead><area>..` with one
    lead for the whole folder (the shortest that fits every member, e.g. '' or 'test_'); none fits: no prefix."""
    by_folder: dict[str, list[tuple[str, str, str]]] = {}  # folder: [(root, target, area)]
    for rel, t in table.items():
        m = GROUP_FILE.fullmatch(PurePosixPath(rel).stem)
        if not t.module and m:
            by_folder.setdefault(str(PurePosixPath(rel).parent), []).append((rel, t.name, m.group(1) or ""))
    out: dict[str, list[Group]] = {}
    for folder, roots in by_folder.items():
        area = {name: a for _, name, a in roots}
        members = [(x.module, area[x.name]) for x in table.values() if x.module and x.name in area]
        common = os.path.commonprefix([s for s, _ in members])
        lead = next((common[:n] for n in range(len(common) + 1)
                     if all(s[n:].startswith(a) for s, a in members)), None)
        for rel, name, a in roots:
            names = {x.module for x in table.values() if x.module and x.name == name}
            decls = _decls(repo / folder, _read(repo / rel))
            helpers = frozenset(d for d, p in decls.items() if d not in names and p is not None)
            out.setdefault(folder, []).append(Group(rel, name, None if lead is None else lead + a, helpers))
    return out


def _decls(folder: Path, text: str) -> dict[str, Path | None]:
    """{name: its module file, or None when gone} of the `mod x;` declarations of a file in `folder`
    (`#[path = ".."]` honoured)."""
    paths = {m.group(2): folder / m.group(1) for m in PATH_MOD.finditer(text)}
    out = {}
    for d in rust_src.mod_decls(rust_src.tokens(text)):
        found = [p for p in ([paths[d]] if d in paths else [folder / f"{d}.rs", folder / d / "mod.rs"]) if p.is_file()]
        out[d] = found[0] if found else None
    return out


def _gone(repo: Path, g: Group, text: str) -> list[str]:
    """The root's `mod x;` declarations whose module file is gone (a renamed or deleted test file)."""
    return [d for d, p in _decls(repo / PurePosixPath(g.root).parent, text).items() if p is None]


def _test_blocks(lines: list[str]) -> list[tuple[int, int, dict]]:
    """(start, end, keys) of each `[[test]]` block of Cargo.toml lines; end excludes the comments and blank lines
    that lead into the next table."""
    heads = [i for i, ln in enumerate(lines) if ln.strip().startswith("[")]
    out = []
    for n, i in enumerate(heads):
        if lines[i].strip() != "[[test]]":
            continue
        j = heads[n + 1] if n + 1 < len(heads) else len(lines)
        while j > i + 1 and (not lines[j - 1].strip() or lines[j - 1].lstrip().startswith("#")):
            j -= 1
        out.append((i, j, (tomllib.loads("".join(lines[i:j])).get("test") or [{}])[0]))
    return out


def _drop_blocks(text: str, rels: set[str]) -> tuple[str, dict[str, str]]:
    """Cargo.toml text without the `[[test]]` blocks of `rels` (and the blank lines after each), and {rel: reason}
    for the blocks that cannot go (keys beyond name and path)."""
    lines = text.splitlines(keepends=True)
    cut, bad = [], {}
    for i, j, keys in _test_blocks(lines):
        rel = str(keys.get("path", "")).replace("\\", "/").removeprefix("./")
        if rel not in rels:
            continue
        extra = sorted(set(keys) - {"name", "path"})
        if extra:
            bad[rel] = f"its [[test]] block sets {', '.join(extra)}: move it by hand"
            continue
        while j < len(lines) and not lines[j].strip():
            j += 1
        cut.append((i, j))
    tail = bool(lines) and not lines[-1].strip()  # a file ending in a blank line keeps one; else no blank tail is left
    for i, j in reversed(cut):
        del lines[i:j]
    while cut and not tail and lines and not lines[-1].strip():
        lines.pop()
    return "".join(lines), bad


def _helper_lines(text: str, helpers: frozenset[str]) -> str:
    """`text` with each helper declaration (`#[path = ".."]` optional) turned into `use crate::<helper>;`, or removed
    (with one blank line, so no blank pair is left) when the file never names `<helper>::`."""
    toks = rust_src.tokens(text)
    for h in helpers:
        used = any(toks[i][:2] == ("ident", h) and toks[i + 1][1] == "::" for i in range(len(toks) - 1))
        decl = re.compile(rf'^(?:#\[path\s*=\s*"[^"\n]*"\][ \t]*\r?\n)?mod {h};[ \t]*(\r?\n|$)', re.M)
        m = decl.search(text)
        if not m:
            continue
        if used:
            text = text[:m.start()] + f"use crate::{h};" + m.group(1) + text[m.end():]
            continue
        a, b = m.start(), m.end()
        before_blank = a == 0 or text[:a].endswith(("\n\n", "\r\n\r\n"))
        after = re.match(r"[ \t]*\r?\n", text[b:])
        if before_blank and after:
            b += after.end()
        text = text[:a] + text[b:]
    return text


def _insert_mod(text: str, stem: str, helpers: frozenset[str]) -> str:
    """The group root `text` declaring `mod <stem>;` in sorted place among its test modules."""
    nl = _nl(text)
    lines = text.splitlines(keepends=True)
    mods = [(i, m.group(1)) for i, ln in enumerate(lines) if (m := MOD_LINE.match(ln.rstrip("\r\n")))]
    tests = [(i, name) for i, name in mods if name not in helpers]
    if tests:
        at = next((i for i, name in tests if name > stem), tests[-1][0] + 1)
        lines.insert(at, f"mod {stem};{nl}")
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += nl
        lines += [nl, f"mod {stem};{nl}"]
    return "".join(lines)


def _drop_mod(text: str, name: str) -> str:
    """The group root `text` without its `mod <name>;` line."""
    return "".join(ln for ln in text.splitlines(keepends=True) if ln.rstrip("\r\n") != f"mod {name};")


def convert(repo, write: bool = True) -> Result:
    """Move the per-file test targets and untargeted test files of `repo`'s group folders into their group targets.
    Writes nothing when anything is refused (or `write` is False)."""
    repo = Path(repo)
    res = Result()
    table = rust_tests.targets(repo)
    groups = _groups(repo, table)
    if not groups:
        return res
    texts: dict[str, str] = {}

    def text(rel: str) -> str:
        if rel not in texts:
            texts[rel] = _read(repo / rel)
        return texts[rel]

    roots = {g.root for gs in groups.values() for g in gs}
    for gs in groups.values():  # a rename's old name stays declared in its root: drop it
        for g in gs:
            for name in _gone(repo, g, text(g.root)):
                texts[g.root] = _drop_mod(text(g.root), name)
                res.dropped.append((g.root, name))
    loose = {str(p.relative_to(repo).as_posix()) for f in groups for p in (repo / f).glob("*.rs")}
    candidates = sorted({rel for rel, t in table.items() if not t.module and rel not in roots
                         and str(PurePosixPath(rel).parent) in groups}
                        | {rel for rel in loose if rust_tests.problem(rel, table)})
    moving = []
    for rel in candidates:
        stem = PurePosixPath(rel).stem
        fits = [g for g in groups[str(PurePosixPath(rel).parent)] if g.prefix is not None and stem.startswith(g.prefix)]
        best = [g for g in fits if len(g.prefix) == max((len(x.prefix) for x in fits), default=-1)]
        if len(best) != 1:
            res.refused.append(f"{rel}: no group root matches its name" if not best else
                               f"{rel}: two group roots match its name ({', '.join(g.root for g in best)})")
            continue
        g = best[0]
        own = [d for d in rust_src.mod_decls(rust_src.tokens(text(rel))) if d not in g.helpers]
        if own:
            res.refused.append(f"{rel}: declares `mod {own[0]};`, which {g.root} does not hold (move it by hand)")
            continue
        moving.append((rel, stem, g))
    cargo, bad = _drop_blocks(text("Cargo.toml"), {rel for rel, _, _ in moving})
    res.refused += [f"{rel}: {why}" for rel, why in bad.items()]
    if res.refused:
        res.dropped.clear()
        return res
    texts["Cargo.toml"] = cargo
    for rel, stem, g in moving:
        texts[rel] = _helper_lines(text(rel), g.helpers)
        if stem not in rust_src.mod_decls(rust_src.tokens(text(g.root))):
            texts[g.root] = _insert_mod(text(g.root), stem, g.helpers)
        res.moved.append((rel, g.root, g.target))
    if not write:
        return res
    for rel, body in texts.items():
        if body != _read(repo / rel):
            _write(repo / rel, body)
            res.changed.append(rel)
    after = rust_tests.targets(repo)  # the layout rule itself must now see each moved file as its group's module
    wrong = [rel for rel, stem, g in moving if after.get(rel) != rust_tests.Target(g.target, stem)]
    if wrong:
        raise RuntimeError(f"rust_test_groups: not grouped after the move: {', '.join(wrong)}")
    return res


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-2].strip())
        return 2
    res = convert(argv[1])
    for ln in res.lines():
        print(ln)
    return 1 if res.refused else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
