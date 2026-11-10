"""dup_literals.py: one string literal, one home. A message or format written twice drifts apart.

Fails when the same string literal of --min characters or more (default 20) appears in two or more places in product
code, compared without surrounding spaces and punctuation: Python (ast, exact, f-string text included, docstrings
left out), and Rust, JS and TS string literals (regex, best effort). Test files and dirs (tests/, test/, test_*.py, *_test.py, *.test.*, *.spec.*) are left out. Fix by
defining the literal once, as a named constant or a helper in the module that owns it, and importing that.
    py -3 dup_literals.py [--repo R] [--min 20] [path ...]     repo-relative files or dirs; default src
Prints `file:line: message` per repeated literal. Exit 0 clean, 1 repeats.
"""
import argparse
import ast
import os
import re
import sys

PY = (".py",)
OTHER = (".rs", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")
SKIP_DIRS = {"tests", "test", "node_modules", "__pycache__", "target", "dist", "build"}
TEST_FILE = re.compile(r"(^test_.*\.py$|_test\.(py|rs)$|\.(test|spec)\.\w+$)")
STRIP = " \t\n:;,.!?-"
STR_RE = re.compile(r'"((?:[^"\\\n]|\\.)*)"' + r"|'((?:[^'\\\n]|\\.)*)'")


def files_under(repo, paths):
    """Repo-relative posix product source files under the paths."""
    out = []
    for top in paths:
        full = os.path.join(repo, top)
        if os.path.isfile(full):
            out.append(top.replace("\\", "/"))
            continue
        for root, dirs, names in os.walk(full):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            rel = os.path.relpath(root, repo).replace(os.sep, "/")
            out += [f"{rel}/{n}" for n in sorted(names)
                    if n.endswith(PY + OTHER) and not TEST_FILE.search(n)]
    return out


def py_literals(text):
    """[(line, literal)] for string constants, f-string text included, docstrings left out."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    skip = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and body:
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip]


def other_literals(text):
    """[(line, literal)] for quoted strings in Rust, JS or TS, comments not stripped (best effort)."""
    out = []
    for m in STR_RE.finditer(text):
        lit = m.group(1) if m.group(1) is not None else m.group(2)
        out.append((text.count("\n", 0, m.start()) + 1, lit))
    return out


def check(repo, paths, minimum):
    """(problems as 'file:line: message', files scanned)."""
    seen = {}
    files = files_under(repo, paths)
    for rel in files:
        with open(os.path.join(repo, rel), encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
        found = py_literals(text) if rel.endswith(PY) else other_literals(text)
        for line, lit in found:
            key = lit.strip(STRIP)
            if len(key) >= minimum:
                seen.setdefault(key, []).append(f"{rel}:{line}")
    out = []
    for lit, places in seen.items():
        if len(places) > 1:
            shown = lit if len(lit) <= 60 else lit[:57] + "..."
            out.append(f"{places[0]}: literal {shown!r} is repeated at {', '.join(places[1:])}; "
                       "define it once and import it")
    return sorted(out), len(files)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=["src"])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--min", type=int, default=20, help="shortest literal that counts (default 20)")
    args = ap.parse_args(argv)
    out, nfiles = check(args.repo, args.paths, args.min)
    sys.stdout.reconfigure(errors="replace")
    last = f"LITERALS FAIL {len(out)} problems" if out else f"LITERALS OK {nfiles} files"
    print("\n".join(out + [last]))
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main())
