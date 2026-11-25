"""The one `cargo test` output parser; a failed target no test name explains is kept by its selector.

  py -3 cargo_failures.py <log>
"""
from __future__ import annotations

import re, sys
from dataclasses import dataclass, field

QUIET = re.compile(r"^(\S+) --- FAILED$", re.M)
VERBOSE = re.compile(r"^test (\S+)(?: - should panic)? \.\.\. FAILED$", re.M)
CAPTURED = re.compile(r"^---- (\S+) stdout ----$", re.M)
BLOCK_END = re.compile(r"^(?:---- \S+ stdout ----|failures:)$", re.M)
LISTED = re.compile(r"^failures:\n((?:    \S+\n)+)\ntest result: FAILED\.", re.M)
RESULT_FAILED = re.compile(r"^test result: FAILED\.", re.M)
TARGET = re.compile(r"to rerun pass `(-p [\w-]+(?: --(?:lib|bins?|tests?|examples?|benches)(?: [\w-]+)?)*)`")
CRASH = re.compile(r"to rerun pass `([^`]+)`\s*\n\s*Caused by:\s*\n\s*process didn't exit successfully: `[^`]*` "
                   r"\((exit code: (?!101\))[^)]*|signal: [^)]*)\)")
PASSED = re.compile(r"^test result: \w+\. (\d+) passed", re.M)
UNBUILT = re.compile(r"^error: could not compile `([\w-]+)`(?: \((?:\w+ )*?(?:lib|\w+ \"([\w-]+)\"))?", re.M)
LIB_UNBUILT = re.compile(r"^error: could not compile `([\w-]+)` \(lib\)", re.M)  # the library, not its tests
# Owner of an unresolved reference (`libX-<hash>.rlib(...)` or `X.<cgu>.<n>.rcgu.o`), never the linker's arguments.
LINK_OWNER = re.compile(r"(?:\blib(\w+)-[0-9a-f]{16}\.rlib\([^)\s]*\)|(?<![\w.-])(\w+)\.\w+\.\w+\.rcgu\.o) : error LNK\d+")
UNRESOLVED = re.compile(r"unresolved external symbol (_R\w+)")
INSTANTIATING = re.compile(r".*Cs[0-9A-Za-z]*_(\d+)([A-Za-z]\w*)$")  # v0 mangling: instantiating crate
SELECTOR = "-p "


@dataclass(frozen=True)
class CargoFailures:
    tests: tuple = ()  # sorted
    targets: tuple = ()  # selectors ("-p crate --lib"), cargo's order
    unnamed: tuple = ()  # failed targets no test name explains
    crashes: dict = field(default_factory=dict)  # {selector: "exit code: ..."}
    unbuilt: tuple = ()  # crate names that did not compile or link
    lib_packages: tuple = ()  # packages whose library did not compile
    linked: tuple = ()  # crates a link error blames

    @property
    def found(self) -> bool:
        return bool(self.tests or self.unnamed)

    @property
    def build_failed(self) -> bool:
        """A build or link error, not a red."""
        return bool(self.unbuilt) and not self.found

    def keys(self) -> list:
        """Plain strings for JSON."""
        return list(self.tests) + list(self.unnamed)

    def without(self, known: CargoFailures) -> CargoFailures:
        unnamed = tuple(t for t in self.unnamed if t not in known.unnamed)
        return CargoFailures(tuple(t for t in self.tests if t not in known.tests), self.targets, unnamed,
                             {t: c for t, c in self.crashes.items() if t in unnamed}, self.unbuilt, self.lib_packages,
                             self.linked)

    def describe(self, cap: int = 5) -> str:
        if not self.found:
            return "unit: no failed test target in the output (a build error?)"
        more = f" (+{len(self.tests) - cap} more)" if len(self.tests) > cap else ""
        parts = ["failing tests: " + ", ".join(self.tests[:cap]) + more] if self.tests else []
        parts += [f"`{t}` failed naming no test" + (f" ({self.crashes[t]})" if t in self.crashes else "")
                  for t in self.unnamed]
        return "unit: " + "; ".join(parts)


def parse(out: str) -> CargoFailures:
    out = (out or "").replace("\r\n", "\n")
    names = set(QUIET.findall(out)) | set(VERBOSE.findall(out)) | set(CAPTURED.findall(out))
    for block in LISTED.findall(out):
        names |= set(block.split())
    targets = tuple(dict.fromkeys(TARGET.findall(out)))
    crashes = {t: c for t, c in CRASH.findall(out) if t in targets}
    # an unaccounted target cannot be pinned: keep all
    short = len(targets) - len(crashes) > len(RESULT_FAILED.findall(out))
    unnamed = tuple(t for t in targets if short or t in crashes)
    unbuilt_at = UNBUILT.findall(out)
    unbuilt = tuple(dict.fromkeys((t or c).replace("-", "_") for c, t in unbuilt_at))
    libs = tuple(dict.fromkeys(LIB_UNBUILT.findall(out)))
    return CargoFailures(tuple(sorted(names)), targets, unnamed, crashes, unbuilt, libs, linked_crates(out))


def captured(out: str) -> dict:
    out = (out or "").replace("\r\n", "\n")
    blocks = {}
    for m in CAPTURED.finditer(out):
        end = BLOCK_END.search(out, m.end())
        blocks.setdefault(m.group(1), out[m.end():end.start() if end else len(out)].strip("\n"))
    return blocks


def linked_crates(out: str) -> tuple:
    """Crates a link error blames: reference owners, then symbols' crates (std and registry included)."""
    out = (out or "").replace("\\r\\n", "\n").replace("\\n", "\n")
    found =[a or o for a, o in LINK_OWNER.findall(out)]
    for sym in UNRESOLVED.findall(out):
        m = INSTANTIATING.search(sym)
        if m and len(m.group(2)) == int(m.group(1)):
            found.append(m.group(2))
    return tuple(dict.fromkeys(found))


def from_keys(keys) -> CargoFailures:
    keys = list(keys or [])
    sel = tuple(k for k in keys if k.startswith(SELECTOR))
    return CargoFailures(tuple(sorted(k for k in keys if not k.startswith(SELECTOR))), sel, sel)


def passed(out: str) -> int:
    return sum(int(n) for n in PASSED.findall((out or "").replace("\r\n", "\n")))


if __name__ == "__main__":
    f = parse(open(sys.argv[1], encoding="utf-8", errors="replace").read())
    print(f.describe(cap=50), "| targets:", ", ".join(f.targets) or "-")
