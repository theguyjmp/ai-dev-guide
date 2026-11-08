"""shape_check.py: Python code shape that ruff does not measure. Long functions mix jobs, constants defined between
functions show append-per-function growth, and a fixture copied into many test files drifts apart.
Fails on:
  - a product function longer than --max-lines lines (default 30), counted from `def` to its last line;
  - a module-level UPPER_CASE constant assigned after the first def or class of a product file (put it at the top);
  - a pytest fixture (decorated `fixture` or `pytest.fixture`) defined identically in two or more test files
    (define it once in the nearest shared conftest.py).
Product files are .py files outside test dirs (tests/, test/) and test files (test_*.py, *_test.py, conftest.py).
Rust (.rs, read by rust_src.py): a product fn longer than --max-lines (outside test dirs, tests.rs / *_test.rs files
and `#[cfg(test)]` mods), and a file-level const or static after the first file-level fn or impl, unless its value
names a fn above it. Rust has no fixtures, so no fixture check.
    py -3 shape_check.py [--repo R] [--max-lines 30] [path ...]     repo-relative files or dirs; default src tests
Prints `file:line: message` per problem. Exit 0 clean, 1 problems. Python standard library only.
"""
import argparse
import ast
import os
import re
import sys

import rust_src  # beside this script

MAX_LINES = 30
SKIP_DIRS = {"node_modules", "__pycache__", "target", "dist", "build", ".venv", "venv"}
TEST_DIRS = {"tests", "test"}
TEST_FILE = re.compile(r"(^test_.*\.py$|_test\.py$|^conftest\.py$)")
CONSTANT = re.compile(r"^_?[A-Z][A-Z0-9_]*$")
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
RS_TEST_FILE = re.compile(r"(^tests\.rs$|_tests?\.rs$)")
ITEM_START = {None, ";", "}", "]", "pub", ")"}  # the token before a file-level item


def py_files(repo, paths, ext=".py"):
    """Repo-relative posix files ending in ext under the paths."""
    out = []
    for top in paths:
        full = os.path.join(repo, top)
        if os.path.isfile(full):
            if top.endswith(ext):
                out.append(top.replace("\\", "/"))
            continue
        for root, dirs, names in os.walk(full):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            rel = os.path.relpath(root, repo).replace(os.sep, "/")
            out += [f"{rel}/{n}" for n in sorted(names) if n.endswith(ext)]
    return out


def is_test(rel):
    """True for a test file or a file under a test dir."""
    parts = rel.split("/")
    return bool(TEST_FILE.search(parts[-1])) or any(p in TEST_DIRS for p in parts[:-1])


def long_functions(rel, tree, max_lines):
    """Problems for functions (nested and methods too) longer than max_lines."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, FUNCS):
            size = node.end_lineno - node.lineno + 1
            if size > max_lines:
                out.append(f"{rel}:{node.lineno}: function {node.name} is {size} lines, over {max_lines}; "
                           "split it into named steps")
    return out


def late_constants(rel, tree):
    """Problems for UPPER_CASE module constants assigned after the first def or class, unless the value uses one
    of the defs or classes above it (a dispatch table must follow its handlers)."""
    out, defined = [], set()
    for node in tree.body:
        if isinstance(node, (*FUNCS, ast.ClassDef)):
            defined.add(node.name)
            continue
        if not defined or not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        if any(isinstance(n, ast.Name) and n.id in defined for n in ast.walk(node.value)):
            continue
        targets = getattr(node, "targets", None) or [node.target]
        names = [t.id for t in targets if isinstance(t, ast.Name) and CONSTANT.match(t.id)]
        out += [f"{rel}:{node.lineno}: constant {name} is defined after the first def or class; "
                "move it to the top of the module" for name in names]
    return out


def is_fixture(node):
    """True for a function decorated with fixture, pytest.fixture or a call of either."""
    for dec in node.decorator_list:
        dec = dec.func if isinstance(dec, ast.Call) else dec
        if getattr(dec, "id", None) == "fixture" or getattr(dec, "attr", None) == "fixture":
            return True
    return False


def fixtures(tree):
    """[(key, line, name)] for module-level fixtures; the key is the whole function's code."""
    return [(ast.dump(node, include_attributes=False), node.lineno, node.name)
            for node in tree.body if isinstance(node, FUNCS) and is_fixture(node)]


def duplicate_fixtures(found):
    """Problems for a fixture whose code repeats in another test file."""
    by_key = {}
    for rel, line, name, key in found:
        by_key.setdefault(key, []).append((rel, line, name))
    out = []
    for places in by_key.values():
        files = sorted({rel for rel, _, _ in places})
        if len(files) > 1:
            rel, line, name = places[0]
            others = ", ".join(f"{r}:{n}" for r, n, _ in places[1:])
            out.append(f"{rel}:{line}: fixture {name} is copied at {others}; define it once in a shared conftest.py")
    return out


def rs_is_test(rel):
    """True for a Rust test file (tests.rs, *_test.rs, *_tests.rs) or a file under a test dir."""
    parts = rel.split("/")
    return bool(RS_TEST_FILE.search(parts[-1])) or any(p in TEST_DIRS for p in parts[:-1])


def rs_long_functions(rel, toks, max_lines):
    """Problems for Rust fns with a body longer than max_lines, `#[cfg(test)]` mods and #[test] fns aside."""
    spans = rust_src.cfg_test_spans(toks)
    out = []
    for f in rust_src.functions(toks):
        if not f["body"] or f["test"] or any(a <= f["start"] <= b for a, b in spans):
            continue
        size = f["end_line"] - f["line"] + 1
        if size > max_lines:
            out.append(f"{rel}:{f['line']}: function {f['name']} is {size} lines, over {max_lines}; "
                       "split it into named steps")
    return out


def rs_late_constants(rel, toks):
    """Problems for file-level const/static items after the first file-level fn or impl, unless the value names
    one of the fns above it (a dispatch table must follow its handlers)."""
    out, fns, depth, seen = [], set(), 0, False
    for i, (kind, tok, line) in enumerate(toks):
        if kind == "punct" and tok in "{}":
            depth += 1 if tok == "{" else -1
            continue
        if depth or kind != "ident":
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else ("", "", 0)
        if tok == "fn" and nxt[0] == "ident":
            seen = True
            fns.add(nxt[1])
        elif tok == "impl" and (toks[i - 1][1] if i else None) in ITEM_START | {"unsafe"}:
            seen = True
        elif seen and tok in ("const", "static") and (toks[i - 1][1] if i else None) in ITEM_START:
            j = i + 1 + (nxt[1] == "mut")
            if j >= len(toks) or toks[j][0] != "ident" or toks[j][1] == "fn":
                continue
            end = next((k for k in range(j, len(toks)) if toks[k][1] == ";"), len(toks))
            if any(t[0] == "ident" and t[1] in fns for t in toks[j + 1:end]):
                continue
            out.append(f"{rel}:{line}: {tok} {toks[j][1]} is defined after the first fn or impl; "
                       "move it to the top of the file")
    return out


def rs_check(repo, files, max_lines):
    """Problems for the Rust product files."""
    out = []
    for rel in files:
        if rs_is_test(rel):
            continue
        with open(os.path.join(repo, rel), encoding="utf-8-sig", errors="replace") as fh:
            toks = rust_src.tokens(fh.read())
        out += rs_long_functions(rel, toks, max_lines) + rs_late_constants(rel, toks)
    return out


def check(repo, paths, max_lines):
    """(problems as 'file:line: message', files scanned)."""
    out, found = [], []
    files = py_files(repo, paths)
    for rel in files:
        with open(os.path.join(repo, rel), encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
        try:
            tree = ast.parse(text, rel)
        except (SyntaxError, ValueError) as err:
            out.append(f"{rel}:{getattr(err, 'lineno', None) or 1}: cannot parse")
            continue
        if is_test(rel):
            found += [(rel, line, name, key) for key, line, name in fixtures(tree)]
        else:
            out += long_functions(rel, tree, max_lines) + late_constants(rel, tree)
    rs = py_files(repo, paths, ".rs")
    return sorted(out + duplicate_fixtures(found) + rs_check(repo, rs, max_lines)), len(files) + len(rs)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=["src", "tests"])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--max-lines", type=int, default=MAX_LINES, help="longest function allowed (default 30)")
    args = ap.parse_args(argv)
    paths = [p for p in args.paths if os.path.exists(os.path.join(args.repo, p))]
    out, nfiles = check(args.repo, paths, args.max_lines)
    sys.stdout.reconfigure(errors="replace")
    last = f"SHAPE FAIL {len(out)} problems" if out else f"SHAPE OK {nfiles} files"
    print("\n".join(out + [last]))
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main())
