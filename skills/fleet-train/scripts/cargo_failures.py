"""The one `cargo test` output parser; a failed target no name accounts for is kept by its selector.

  py -3 cargo_failures.py <log>
"""
from __future__ import annotations

import re, sys
from dataclasses import dataclass, field

QUIET = re.compile(r"^(\S+) --- FAILED$", re.M)
VERBOSE = re.compile(r"^test (\S+)(?: - should panic)? \.\.\. FAILED$", re.M)
CAPTURED = re.compile(r"^---- (\S+) stdout ----$", re.M)
LISTED = re.compile(r"^failures:\n((?:    \S+\n)+)\ntest result: FAILED\.", re.M)
RESULT_FAILED = re.compile(r"^test result: FAILED\.", re.M)
TARGET = re.compile(r"to rerun pass `(-p [\w-]+(?: --(?:lib|bins?|tests?|examples?|benches)(?: [\w-]+)?)*)`")
CRASH = re.compile(r"to rerun pass `([^`]+)`\s*\n\s*Caused by:\s*\n\s*process didn't exit successfully: `[^`]*` "
                   r"\((exit code: (?!101\))[^)]*|signal: [^)]*)\)")
PASSED = re.compile(r"^test result: \w+\. (\d+) passed", re.M)
UNBUILT = re.compile(r"^error: could not compile `([\w-]+)`(?: \((?:\w+ )*?(?:lib|\w+ \"([\w-]+)\"))?", re.M)
LIB_UNBUILT = re.compile(r"^error: could not compile `([\w-]+)` \(lib\)", re.M)  # the library itself, not its tests
# A link error's owners: the archive member or object holding an unresolved reference (`libX-<hash>.rlib(...)` or
# `X.<cgu>.<n>.rcgu.o`), never the linker's argument list. Logs may carry cargo's JSON escapes (a literal \r\n).
LINK_OWNER = re.compile(r"(?:\blib(\w+)-[0-9a-f]{16}\.rlib\([^)\s]*\)|(?<![\w.-])(\w+)\.\w+\.\w+\.rcgu\.o) : error LNK\d+")
UNRESOLVED = re.compile(r"unresolved external symbol (_R\w+)")
INSTANTIATING = re.compile(r".*Cs[0-9A-Za-z]*_(\d+)([A-Za-z]\w*)$")  # v0 mangling: the symbol's instantiating crate
SELECTOR = "-p "


@dataclass(frozen=True)
class CargoFailures:
    tests: tuple = ()  # failing test names, sorted
    targets: tuple = ()  # every failed target's selector ("-p crate --lib"), in cargo's order
    unnamed: tuple = ()  # failed targets no printed name accounts for
    crashes: dict = field(default_factory=dict)  # {selector: "exit code: ..."}
    unbuilt: tuple = ()  # targets cargo could not compile or link, by crate name (incremental/<name>-*)
    lib_packages: tuple = ()  # packages whose library cargo could not compile, as cargo names them
    linked: tuple = ()  # crates a link error names: its archive/object owners and unresolved symbols' crates

    @property
    def found(self) -> bool:
        return bool(self.tests or self.unnamed)

    @property
    def build_failed(self) -> bool:
        """A target did not build and no test ran to a failure: the output is a build or link error, not a red."""
        return bool(self.unbuilt) and not self.found

    def keys(self) -> list:
        """Plain strings for JSON (a name never starts with "-")."""
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
    # an unaccounted target cannot be pinned: keep them all
    short = len(targets) - len(crashes) > len(RESULT_FAILED.findall(out))
    unnamed = tuple(t for t in targets if short or t in crashes)
    unbuilt_at = UNBUILT.findall(out)
    unbuilt = tuple(dict.fromkeys((t or c).replace("-", "_") for c, t in unbuilt_at))
    libs = tuple(dict.fromkeys(LIB_UNBUILT.findall(out)))
    return CargoFailures(tuple(sorted(names)), targets, unnamed, crashes, unbuilt, libs, linked_crates(out))


def linked_crates(out: str) -> tuple:
    """Crate names a link error blames, in order: the archive or object owning each unresolved reference, then each
    unresolved symbol's instantiating crate (v0 `Cs<hash>_<len><name>` suffix). Std and registry crates may appear;
    the caller keeps the ones it can map to its workspace's packages."""
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
