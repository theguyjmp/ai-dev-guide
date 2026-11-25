"""Rust integration-test targets: which cargo `--test` target builds a test file, and the name its tests run under.

A test file is either its own root Cargo.toml `[[test]]` target (its tests are named `<fn>`), or a module of a group
target: a `[[test]]` whose root file, in the same directory, declares `mod <file stem>;` (its tests are named
`<file stem>::<fn>`). A derived group root (`group_<area>.rs`) lists no modules: it includes
`$OUT_DIR/test_groups/<dir>/<area>.rs`, which the repo's build.rs writes, declaring every sibling test file of its
area (DERIVED, `member_area`), so two branches adding test files never touch one shared line. Grouping a few thousand
test files into a few targets keeps `cargo clippy --all-targets` and `cargo test --no-run` from checking and linking
one crate per file. A repo may hold these layouts side by side; a test's identity stays `<file>::<fn>` either way, and
callers turn it into the cargo command through `Target`.

  py -3 rust_tests.py <repo> <test file or stem>...   # per input: <target> <filter> (filter '' = the whole target)
"""
from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MOD = re.compile(r"^mod (\w+);", re.M)  # a group root's out-of-line child modules (not `pub mod`, not inline ones)
DERIVED = 'include!(concat!(env!("OUT_DIR"), "/test_groups/'  # a derived group root (build.rs writes its modules)
DERIVED_ROOT = re.compile(r"group_(\w+)")  # a derived root's file stem: `group_<area>`
MEMBER_LEAD = "test_"  # what a derived member's stem may start with before its area (`test_r0530` in area r0)


def member_area(stem: str, areas) -> str | None:
    """The area of the derived group whose member the test file `stem` is: the longest area its stem starts with
    after an optional MEMBER_LEAD (None: no area fits, or `stem` is a group root). The repo's build.rs compiles the
    files by this same rule; a file no area fits stops that build."""
    if DERIVED_ROOT.fullmatch(stem):
        return None
    rest = stem.removeprefix(MEMBER_LEAD)
    fits = [a for a in areas if rest.startswith(a)]
    return max(fits, key=len) if fits else None


@dataclass(frozen=True)
class Target:
    name: str  # the cargo `--test` target
    module: str = ""  # the file's module in a group target; '' when the file is its own target

    def test(self, fn: str) -> str:
        """The libtest name of the file's `fn`: what `--exact` matches and what a run prints."""
        return f"{self.module}::{fn}" if self.module else fn

    def fn(self, printed: str) -> str:
        """The file's fn name of a libtest name this target printed (`test` undone)."""
        return printed.removeprefix(f"{self.module}::") if self.module else printed

    @property
    def filter(self) -> str:
        """The libtest filter that runs every test of this file and no other ('' = the whole target)."""
        return f"{self.module}::" if self.module else ""


def targets(repo) -> dict[str, Target]:
    """{repo-relative posix path: Target} of every Rust test file the root Cargo.toml builds ({} without one)."""
    repo = Path(repo)
    try:
        conf = tomllib.loads((repo / "Cargo.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    own = {}
    for t in conf.get("test") or []:
        if isinstance(t, dict) and t.get("path") and t.get("name"):
            own[str(t["path"]).replace("\\", "/").removeprefix("./")] = Target(str(t["name"]))
    out = dict(own)
    derived: dict[PurePosixPath, dict[str, str]] = {}  # folder: {area: target}
    for rel, t in own.items():
        try:
            text = (repo / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        folder = PurePosixPath(rel).parent
        if DERIVED in text and (m := DERIVED_ROOT.fullmatch(PurePosixPath(rel).stem)):
            derived.setdefault(folder, {})[m.group(1)] = t.name
        for m in MOD.finditer(text):
            child = str(folder / f"{m.group(1)}.rs")
            if child not in own and (repo / child).is_file():
                out.setdefault(child, Target(t.name, m.group(1)))
    for folder, areas in derived.items():
        for p in sorted((repo / folder).glob("*.rs")):
            child = str(folder / p.name)
            if child not in own and (area := member_area(p.stem, areas)) is not None:
                out.setdefault(child, Target(areas[area], p.stem))
    return out


def target_of(table: dict[str, Target], rel: str) -> Target:
    """`rel`'s Target in `table`; a file it lacks is taken as its own target named after its stem (cargo then names
    the missing target)."""
    rel = rel.replace("\\", "/").removeprefix("./")
    return table.get(rel) or Target(PurePosixPath(rel).stem)


def root_of(table: dict[str, Target], rel: str) -> str:
    """The group root file that declares the test file `rel` as a module ('' when `rel` is its own target or no
    target builds it)."""
    t = table.get(rel.replace("\\", "/").removeprefix("./"))
    if t is None or not t.module:
        return ""
    return next((p for p, x in table.items() if x.name == t.name and not x.module), "")


def problem(rel: str, table: dict[str, Target]) -> str:
    """A problem line when the Rust test file `rel` is not built by any test target, else ''."""
    if rel in table:
        return ""
    stem, folder = PurePosixPath(rel).stem, PurePosixPath(rel).parent
    grouped = {t.name for t in table.values() if t.module}
    roots = sorted(p for p, t in table.items() if not t.module and t.name in grouped and PurePosixPath(p).parent == folder)
    if roots:
        return (f"{rel} is not a cargo test module: declare `mod {stem};` in its area's group root, or (a derived "
                f"root) start its name with the root's area ({', '.join(roots)})")
    return f'{rel} is not a cargo test target: add [[test]] name = "{stem}" path = "{rel}" to the root Cargo.toml'


def main(argv: list[str]) -> int:
    table = targets(argv[1])
    for item in argv[2:]:
        rel = item if item.endswith(".rs") else next((p for p in table if PurePosixPath(p).stem == item), item)
        t = target_of(table, rel)
        print(t.name, t.filter)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
