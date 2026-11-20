"""keyed_merge.py merges the repo's keyed spec files unit by unit (2026-11-20: after every land 13-58 queued rows
conflicted with main in spec/FUNCTIONS.md, ROADMAP.md, ARCHITECTURE.md and Cargo.toml `[[test]]` entries while each
row only added or edited its own rows, bullets or entries; each land cost a restack pass). Unit tests of the merge,
then the driver through git merge-tree in a temp repo, registered by the harness under test.

  py -3 tests/test_keyed_merge.py [harness file, default harness.py]
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOME))
import keyed_merge as km  # noqa: E402

HARNESS = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HOME / "harness.py"
failures = 0


def check(what: str, ok: bool, *detail) -> None:
    global failures
    print(("ok   " if ok else "FAIL ") + what + ("" if ok else f"  {detail}"))
    failures += not ok


def merged(path: str, base: str, ours: str, theirs: str) -> tuple[str, int]:
    out, n = km.merge(*(km.split(path, s.splitlines()) for s in (base, ours, theirs)))
    return "\n".join(out) + "\n", n


TABLE = "# Phase 47\n\n| id | phase | status |\n|---|---|---|\n| R1 | 47 | open |\n| R2 | 47 | open |\n\n# Phase 48\n\nend\n"

# table rows keyed by id
out, n = merged("f.md", TABLE, TABLE.replace("| R1 | 47 | open |", "| R1 | 47 | proven |"),
                TABLE.replace("| R2 | 47 | open |", "| R2 | 47 | proven |"))
check("adjacent rows edited on each side merge clean", n == 0 and "| R1 | 47 | proven |" in out and "| R2 | 47 | proven |" in out, out)
out, n = merged("f.md", TABLE, TABLE.replace("| R2 | 47 | open |\n", "| R2 | 47 | open |\n| R3 | 47 | a |\n"),
                TABLE.replace("| R2 | 47 | open |\n", "| R2 | 47 | open |\n| R4 | 47 | b |\n"))
check("rows both sides append stay in their table, ours first",
      n == 0 and "| R2 | 47 | open |\n| R3 | 47 | a |\n| R4 | 47 | b |\n\n# Phase 48" in out, out)
out, n = merged("f.md", TABLE, TABLE.replace("| R1 | 47 | open |", "| R1 | 47 | proven |"),
                TABLE.replace("| R1 | 47 | open |", "| R1 | 47 | held |"))
check("one row edited differently on both sides is a conflict for that row only",
      n == 1 and out.count("<<<<<<< ours") == 1 and "| R2 | 47 | open |" in out
      and "<<<<<<< ours\n| R1 | 47 | proven |\n=======\n| R1 | 47 | held |\n>>>>>>> theirs" in out, out)
out, n = merged("f.md", TABLE, TABLE.replace("| R1 | 47 | open |", "| R1 | 47 | proven |"),
                TABLE.replace("| R1 | 47 | open |", "| R1 | 47 | proven |"))
check("one row edited the same on both sides is taken once", n == 0 and out.count("| R1 |") == 1, out)
out, n = merged("f.md", TABLE, TABLE.replace("| R1 | 47 | open |\n", ""), TABLE.replace("| R1 | 47 | open |", "| R1 | 47 | x |"))
check("a row deleted on one side and edited on the other is a conflict", n == 1 and "| R1 | 47 | x |" in out, out)
out, n = merged("f.md", TABLE, TABLE.replace("| R2 | 47 | open |\n", "| R2 | 47 | open |\n| R9 | 47 | a |\n"),
                TABLE.replace("| R1 | 47 | open |\n", "| R1 | 47 | open |\n| R9 | 47 | b |\n"))
check("one id added on both sides with different text is a conflict, never two rows",
      n == 1 and out.count("| R9 | 47 | a |") == 1 and out.count("| R9 | 47 | b |") == 1 and "<<<<<<<" in out, out)
out, n = merged("f.md", TABLE, TABLE.replace("| R2 | 47 | open |\n", "| R2 | 47 | open |\n| R9 | 47 | a |\n"),
                TABLE.replace("| R1 | 47 | open |\n", "| R1 | 47 | open |\n| R9 | 47 | a |\n"))
check("one id added on both sides with the same text is held once", n == 0 and out.count("| R9 |") == 1, out)
dup = TABLE.replace("| R2 | 47 | open |\n", "| R2 | 47 | open |\n| R2 | 47 | again |\n")
out, n = merged("f.md", dup, dup.replace("| R1 | 47 | open |", "| R1 | 47 | proven |"),
                dup.replace("| R2 | 47 | again |", "| R2 | 47 | twice |"))
check("an id the base lists twice is keyed by occurrence and never multiplied",
      n == 0 and out.count("| R2 |") == 2 and "| R2 | 47 | twice |" in out and "| R1 | 47 | proven |" in out, out)

# bullets with continuation lines
ARCH = ("## Module a\nDepends on: core\n- `x::f` [R1]\n  continued [S53]\n- `y::g` [R2]\n\n## Module b\n- `z` [R3]\n")
out, n = merged("a.md", ARCH, ARCH.replace("continued [S53]", "continued [S53]; edited (R4636)"),
                ARCH.replace("  continued [S53]\n", "  continued [S53]\n- `w::new` [R5223d]\n"))
check("a bullet's continuation edited beside a bullet added after it merges clean (kernel20 vs main)",
      n == 0 and "  continued [S53]; edited (R4636)\n- `w::new` [R5223d]\n- `y::g` [R2]" in out, out)
out, n = merged("a.md", ARCH, ARCH.replace("- `y::g` [R2]\n", "- `y::g` [R2]\n- `o` [R8]\n"),
                ARCH.replace("- `y::g` [R2]\n", "- `y::g` [R2]\n- `t` [R9]\n"))
check("bullets both sides add at one spot are both kept, ours first", n == 0 and "- `o` [R8]\n- `t` [R9]\n" in out, out)
out, n = merged("a.md", ARCH, ARCH.replace("- `x::f` [R1]", "- `x::f, h` [R1]"), ARCH.replace("- `x::f` [R1]", "- `x::f, k` [R1]"))
check("one bullet edited differently on both sides is a conflict for it only", n == 1 and out.count("- `y::g` [R2]") == 1, out)
out, n = merged("a.md", ARCH, ARCH.replace("Depends on: core", "Depends on: core, testkit"),
                ARCH.replace("- `y::g` [R2]", "- `y::g, h` [R2]"))
check("a field line and a bullet edited on each side merge clean",
      n == 0 and "Depends on: core, testkit" in out and "- `y::g, h` [R2]" in out, out)
out, n = merged("a.md", ARCH, ARCH.replace("- `y::g` [R2]\n", "- `y::one` [R2]\n- `y::two` [R2]\n"),
                ARCH.replace("- `y::g` [R2]\n", "- `y::three` [R2]\n- `y::four` [R2]\n"))
check("both sides rewriting one bullet into different bullets is a conflict, not four bullets", n == 1, out)

# Cargo [[test]] blocks keyed by name
CARGO = '[package]\nname = "app"\n\n[[test]]\nname = "a"\npath = "a.rs"\n\n[[test]]\nname = "b"\npath = "b.rs"\n'
out, n = merged("Cargo.toml", CARGO, CARGO + '\n[[test]]\nname = "c"\npath = "c.rs"\n', CARGO + '\n[[test]]\nname = "d"\npath = "d.rs"\n')
check("[[test]] entries both sides append are both kept", n == 0 and out.count("[[test]]") == 4 and out.index('"c"') < out.index('"d"'), out)
out, n = merged("Cargo.toml", CARGO, CARGO + '\n[[test]]\nname = "c"\npath = "c.rs"\n', CARGO + '\n[[test]]\nname = "c"\npath = "x.rs"\n')
check("one [[test]] name added on both sides with different paths is a conflict", n == 1 and out.count('name = "c"') == 2 and "<<<<<<<" in out, out)

# fallbacks
for what, path, text in (("another extension", "f.rs", "x\n"), ("conflict markers in an input", "f.md", "<<<<<<< ours
"),
                         ("an inner merge's longer markers", "f.md", "- X
<<<<<<<<< ours
- Y
=========
- Z
>>>>>>>>> theirs
")):
    try:
        km.split(path, text.splitlines())
        check(f"{what} is unmergeable", False)
    except km.Unmergeable:
        check(f"{what} is unmergeable", True)


def git(cwd, *args, ok=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if ok and r.returncode:
        raise SystemExit(f"git {args}: {r.stderr}")
    return r


def commit(root, branch, parent, files, msg):
    git(root, "checkout", "-q", "-B", branch, *([parent] if parent else []))
    for name, text in files.items():
        (root / name).write_bytes(text.encode("utf-8"))
        git(root, "add", name)
    git(root, "commit", "-q", "-m", msg)
    return git(root, "rev-parse", "HEAD").stdout.strip()


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    os.environ.update(HARNESS_HOME=str(HOME), HARNESS_STATE=str(root / "state"), HARNESS_WT=str(root / "wt"), HARNESS_REPO=str(root))
    (root / "state").mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    spec = importlib.util.spec_from_file_location("harness_under_test", HARNESS)
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    h.ensure_merge_drivers()
    check("keyed driver registered in the repo config",
          git(root, "config", "merge.keyed.driver").stdout.strip() == h.MERGE_DRIVERS["keyed"][1])
    base = commit(root, "main", None, {".gitattributes": "spec.md merge=keyed\n", "spec.md": TABLE}, "base")
    ours = commit(root, "ours", base, {"spec.md": TABLE.replace("| R1 | 47 | open |", "| R1 | 47 | proven |")}, "ours")
    theirs = commit(root, "theirs", base, {"spec.md": TABLE.replace("| R2 | 47 | open |", "| R2 | 47 | proven |")}, "theirs")
    r = git(root, "merge-tree", "--write-tree", ours, theirs, ok=False)
    text = git(root, "show", f"{r.stdout.split()[0]}:spec.md").stdout if r.returncode == 0 else ""
    check("git merge-tree merges adjacent row edits clean through the driver",
          r.returncode == 0 and "| R1 | 47 | proven |" in text and "| R2 | 47 | proven |" in text, r.stdout)
    clash = commit(root, "clash", base, {"spec.md": TABLE.replace("| R1 | 47 | open |", "| R1 | 47 | held |")}, "clash")
    r = git(root, "merge-tree", "--write-tree", ours, clash, ok=False)
    check("git merge-tree leaves a same-row clash as a conflict", r.returncode == 1, r.stdout)
    git(root, "checkout", "-q", "ours")
    landed = git(root, "merge", "-q", "--no-edit", "theirs", ok=False)
    check("git merge (the land and restack path) merges it too", landed.returncode == 0, landed.stderr)

print("FAILED" if failures else "PASSED", failures)
sys.exit(1 if failures else 0)
