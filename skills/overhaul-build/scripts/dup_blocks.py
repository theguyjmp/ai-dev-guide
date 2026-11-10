"""dup_blocks.py: one block of Rust code, one home. A block pasted twice gets fixed in one copy and not the other.

Fails when --min lines (default 6) of product Rust code repeat, in the same file or another. Lines are compared
normalised: comments and blank lines dropped, tokens joined by one space (rust_src.py), so layout and comments do not
hide a copy. Trivial lines do not count and break no run: lines of only brackets, punctuation and `pub`, and whole
`use`, `mod x;` and `extern crate` statements. Product code is .rs outside test dirs (tests/, test/), test files (tests.rs, *_test.rs,
*_tests.rs) and `#[cfg(test)]` mods. Python is left to pylint's duplicate-code check. Fix by extracting the block
into one named fn (or a macro_rules! when the types differ) and calling it from both places. jscpd is an optional
second opinion; this script is what the gate runs.
    py -3 dup_blocks.py [--repo R] [--min 6] [path ...]     repo-relative files or dirs; default src
Prints `file:line: message` per repeated block. Exit 0 clean, 1 repeats. Python standard library only.
"""
import argparse
import os
import re
import sys

import rust_src  # beside this script

MIN_LINES = 6
SKIP_DIRS = {"tests", "test", "node_modules", "__pycache__", "target", "dist", "build"}
TEST_FILE = re.compile(r"(^tests\.rs$|_tests?\.rs$)")
VISIBILITY = {"pub", "crate", "super", "self", "in"}


def rs_files(repo, paths):
    """Repo-relative posix product .rs files under the paths."""
    out = []
    for top in paths:
        full = os.path.join(repo, top)
        if os.path.isfile(full):
            out += [top.replace("\\", "/")] if top.endswith(".rs") else []
            continue
        for root, dirs, names in os.walk(full):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            rel = os.path.relpath(root, repo).replace(os.sep, "/")
            out += [f"{rel}/{n}" for n in sorted(names) if n.endswith(".rs") and not TEST_FILE.search(n)]
    return [f for f in out if not SKIP_DIRS & set(f.split("/")[:-1])]


def is_trivial(toks):
    """True for a line of only punctuation and visibility words."""
    return all(kind == "punct" or t in VISIBILITY for kind, t in toks)


def skipped(toks):
    """Token indexes left out: `#[cfg(test)]` mods and whole `use`, `mod x;` and `extern crate` statements."""
    skip = set()
    for a, b in rust_src.cfg_test_spans(toks):
        skip.update(range(a, b + 1))
    for i, (kind, tok, _) in enumerate(toks):
        is_mod = tok == "mod" and i + 2 < len(toks) and toks[i + 2][1] == ";"
        is_crate = tok == "extern" and i + 1 < len(toks) and toks[i + 1][1] == "crate"
        if kind == "ident" and (tok == "use" or is_crate or is_mod):
            end = next((k for k in range(i, len(toks)) if toks[k][1] == ";"), len(toks) - 1)
            skip.update(range(i, end + 1))
    return skip


def norm_lines(text):
    """[(line number, normalised text)] for the non-trivial lines (see the module doc)."""
    toks = rust_src.tokens(text)
    skip = skipped(toks)
    by_line = {}
    for i, (kind, tok, line) in enumerate(toks):
        if i not in skip:
            by_line.setdefault(line, []).append((kind, repr(tok) if kind == "str" else tok))
    return [(line, " ".join(t for _, t in toks_)) for line, toks_ in sorted(by_line.items())
            if not is_trivial(toks_)]


def windows(files_lines, size):
    """{window key: [(file, index)]} with overlapping copies in one file dropped."""
    seen = {}
    for rel, lines in files_lines.items():
        for i in range(len(lines) - size + 1):
            key = "\n".join(t for _, t in lines[i:i + size])
            places = seen.setdefault(key, [])
            if not any(r == rel and i - j < size for r, j in places):
                places.append((rel, i))
    return {k: v for k, v in seen.items() if len(v) > 1}


def check(repo, paths, size):
    """(problems as 'file:line: message', files scanned)."""
    files = rs_files(repo, paths)
    files_lines = {}
    for rel in files:
        with open(os.path.join(repo, rel), encoding="utf-8-sig", errors="replace") as fh:
            files_lines[rel] = norm_lines(fh.read())
    dups = windows(files_lines, size)
    key_at = {place: k for k, places in dups.items() for place in places}
    out, covered = [], set()
    for rel, lines in files_lines.items():
        i = 0
        while i < len(lines):
            if (rel, i) not in key_at:
                i += 1
                continue
            run = i
            while (rel, run + 1) in key_at:
                run += 1
            keys = [key_at[(rel, j)] for j in range(i, run + 1)]
            if not all(k in covered for k in keys):
                first = key_at[(rel, i)]
                others = ", ".join(f"{r}:{files_lines[r][j][0]}" for r, j in dups[first] if (r, j) != (rel, i))
                count = run - i + size
                out.append(f"{rel}:{lines[i][0]}: {count} lines repeat at {others}; "
                           "extract them into one named fn and call it from both places")
                covered.update(keys)
            i = run + 1
    return out, len(files)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=["src"])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--min", type=int, default=MIN_LINES, help="shortest repeated block that counts (default 6)")
    args = ap.parse_args(argv)
    paths = [p for p in args.paths if os.path.exists(os.path.join(args.repo, p))]
    out, nfiles = check(args.repo, paths, max(1, args.min))
    sys.stdout.reconfigure(errors="replace")
    last = f"DUP BLOCKS FAIL {len(out)} problems" if out else f"DUP BLOCKS OK {nfiles} files"
    print("\n".join(out + [last]))
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main())
