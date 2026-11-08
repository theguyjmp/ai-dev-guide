"""entry_lint.py: acceptance tests may drive the product only through its public entry.

Every .py file under tests/acceptance/ may import only these modules (a name also allows its submodules):
    subprocess, urllib, http.client, json, pathlib, tempfile, os, sys, time, socket, re, shutil, signal,
    threading, concurrent.futures (for concurrent tests), pytest, and the local helper _entry
    (tests/acceptance/_entry.py)
It may not use unittest.mock, MagicMock, patch or monkeypatch (nor Mock, AsyncMock, MonkeyPatch, mocker), and it may
not import any product package. Drive the product as a user does: a CLI through subprocess, a server over HTTP.
Rust (.rs files there, read by rust_src.py): no mock crate (mockall, mockito, wiremock, httpmock, faux) and no
`automock`, no `#[path = ..]` and no `include!`, and no use of a product crate (a package or lib name from a Cargo.toml
in the repo, `-` read as `_`). One exception: a file holding a `lib:` row's test (spec/FUNCTIONS.md) may use that
row's crate, since a library's public entry is its API.
    py -3 entry_lint.py [--repo R] [path ...]      repo-relative files or dirs; default tests/acceptance
Prints `file:line: message` per violation. Exit 0 clean, 1 violations.
"""
import argparse
import ast
import os
import posixpath
import re
import sys
import tomllib

import rust_src  # beside this script

ALLOWED = ("subprocess", "urllib", "http.client", "json", "pathlib", "tempfile", "os", "sys", "time", "socket", "re",
           "shutil", "signal", "threading", "concurrent.futures", "pytest", "_entry")
BANNED = frozenset(("MagicMock", "Mock", "AsyncMock", "patch", "monkeypatch", "MonkeyPatch", "mocker"))
BANNED_TEXT = frozenset(("monkeypatch", "mocker", "unittest.mock"))
DEFAULT = "tests/acceptance"
NOT_PRODUCT = ("tests", "test", "node_modules", "__pycache__")
RUST_MOCKS = frozenset(("mockall", "mockito", "wiremock", "httpmock", "faux", "automock"))
CARGO_SKIP = {"target", "node_modules", "tests", "test", "__pycache__"}
FUNCTIONS = "spec/FUNCTIONS.md"


def allowed(name):
    """True when the dotted module name is one of ALLOWED or a submodule of one."""
    return any(name == a or name.startswith(a + ".") for a in ALLOWED)


def has_py(path, depth=2):
    """True when the dir holds a .py file, looking `depth` levels down."""
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except OSError:
        return False
    return any((e.is_file() and e.name.endswith(".py"))
               or (depth > 1 and e.is_dir() and not e.name.startswith(".") and e.name != "node_modules"
                   and has_py(e.path, depth - 1))
               for e in entries)


def product_names(repo):
    """The top-level names the repo defines at its root and in src/: its product packages, tests aside."""
    names = set()
    for base in (repo, os.path.join(repo, "src")):
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            if e.name.startswith(".") or e.name in NOT_PRODUCT:
                continue
            if e.is_file() and e.name.endswith(".py"):
                names.add(e.name[:-3])
            elif e.is_dir() and has_py(e.path):
                names.add(e.name)
    return names


def why(mod, names, product):
    """The message for a module an acceptance test may not import."""
    root = mod.split(".")[0]
    if root == "mock" or mod == "unittest.mock" or mod.startswith("unittest.mock.") \
            or (mod == "unittest" and "mock" in names):
        return f"imports `{mod}`: mocks, fakes and patching are banned in acceptance tests"
    if root in product:
        return (f"imports product package `{mod}`: drive the product through its entry point "
                f"(a CLI via subprocess, a server over HTTP), never import it")
    return f"imports `{mod}`, which acceptance tests may not use; allowed: {', '.join(ALLOWED)}"


def import_problems(node, product):
    """The messages for one import statement."""
    if isinstance(node, ast.Import):
        return [why(a.name, (), product) for a in node.names if not allowed(a.name)]
    names, mod = [a.name for a in node.names], node.module or ""
    if node.level > 1:
        return [f"relative import `{'.' * node.level}{mod}` reaches out of tests/acceptance/, into product code"]
    if node.level == 1:
        return [f"relative import `.{t}` is not allowed; only the local helper `_entry` may be imported"
                for t in ([mod] if mod else names) if t.split(".")[0] != "_entry"]
    if allowed(mod):
        return []
    bad = [n for n in names if not allowed(f"{mod}.{n}")]
    if not bad:
        return []
    if len(bad) < len(names):           # a prefix such as http, where only some names are allowed
        return [why(f"{mod}.{n}", (), product) for n in bad]
    return [why(mod, names, product)]


def lint_source(rel, text, product):
    """[(line, message)] for one acceptance test file."""
    try:
        tree = ast.parse(text, rel)
    except (SyntaxError, ValueError) as err:
        return [(getattr(err, "lineno", None) or 1, f"cannot parse: {getattr(err, 'msg', err)}")]
    out = []
    for node in ast.walk(tree):
        line, name = getattr(node, "lineno", 1), None
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out += [(line, msg) for msg in import_problems(node, product)]
        elif isinstance(node, ast.alias):
            name = next((n for n in (node.name.split(".")[-1], node.asname) if n in BANNED), None)
        elif isinstance(node, ast.Name):
            name = node.id if node.id in BANNED else None
            if node.id == "__import__":
                out.append((line, "`__import__` loads a module the lint cannot see; import it by name"))
        elif isinstance(node, ast.Attribute):
            name = node.attr if node.attr in BANNED else None
        elif isinstance(node, ast.arg):
            name = node.arg if node.arg in BANNED else None
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name if node.name in BANNED else None
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in BANNED_TEXT:
            name = node.value
        if name:
            out.append((line, f"`{name}` is banned: acceptance tests use no mocks, fakes or patching; "
                              f"drive the real entry point"))
    return sorted(set(out))


def crate_names(repo):
    """Package and lib names (`-` read as `_`) of every Cargo.toml in the repo, test and build dirs aside."""
    names = set()
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in CARGO_SKIP and not d.startswith("."))
        if "Cargo.toml" not in files:
            continue
        try:
            with open(os.path.join(root, "Cargo.toml"), "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        for table in ("package", "lib"):
            name = (data.get(table) or {}).get("name")
            if isinstance(name, str):
                names.add(name.replace("-", "_"))
    return names


def lib_crates(repo):
    """{acceptance file: {crate}} for the `lib: <crate>::..` rows of spec/FUNCTIONS.md."""
    try:
        with open(os.path.join(repo, FUNCTIONS), encoding="utf-8-sig", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return {}
    out, cols = {}, None
    for line in lines:
        if not line.lstrip().startswith("|"):
            cols = None
            continue
        cells = [c.strip().strip("`").strip() for c in re.split(r"(?<!\\)\|",line.strip().strip("|"))]
        if cols is None:
            low = [c.lower().strip("*") for c in cells]
            cols = (low.index("entry"), low.index("acceptance")) if {"entry", "acceptance"} <= set(low) else None
            continue
        if len(cells) <= max(cols):
            continue
        m = re.match(r"lib\s*:\s*`?\s*([\w-]+)", cells[cols[0]], re.I)
        for item in cells[cols[1]].split(";") if m else ():
            path = item.strip().strip("`").split("::")[0].strip()
            if path:
                out.setdefault(posixpath.normpath(path), set()).add(m.group(1).replace("-", "_"))
    return out


def lint_rust(text, crates, allowed_crates=()):
    """[(line, message)] for one Rust acceptance file."""
    toks, out = rust_src.tokens(text), []
    for i, (kind, tok, line) in enumerate(toks):
        nxt = toks[i + 1][1] if i + 1 < len(toks) else ""
        prev = toks[i - 1][1] if i else ""
        if kind != "ident":
            continue
        if tok in RUST_MOCKS:
            out.append((line, f"`{tok}` is banned: acceptance tests use no mocks, fakes or patching; "
                              "drive the real entry point"))
        elif tok == "path" and prev == "[" and nxt == "=" and i > 1 and toks[i - 2][1] in ("#", "!"):
            out.append((line, "`#[path = ..]` pulls a file in by path, maybe product code; use `mod` on a helper "
                              "beside the test"))
        elif tok == "include" and nxt == "!":
            out.append((line, "`include!` pastes in source the lint cannot see; drive the product through its entry"))
        elif tok in crates and tok not in allowed_crates and (nxt == "::" or prev == "use"
                                                              or (prev == "crate" and i > 1
                                                                  and toks[i - 2][1] == "extern")):
            out.append((line, f"uses product crate `{tok}`: drive the product through its entry point "
                              "(the built binary via env!(\"CARGO_BIN_EXE_<bin>\"), a server over HTTP), never "
                              "link it; only a `lib:` row's test may use its crate"))
    return sorted(set(out))


def collect(repo, paths):
    """(repo-relative .py and .rs files, problems) for repo-relative files and dirs."""
    files, probs = [], []
    for p in paths:
        rel = posixpath.normpath(p.replace("\\", "/")) if p else "."
        full = os.path.join(repo, rel)
        if os.path.isdir(full):
            for root, dirs, names in os.walk(full):
                dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith("."))
                files += [os.path.relpath(os.path.join(root, n), repo).replace(os.sep, "/")
                          for n in sorted(names) if n.endswith((".py", ".rs"))]
        elif os.path.isfile(full):
            files.append(os.path.relpath(full, repo).replace(os.sep, "/"))
        else:
            probs.append(f"{rel}:1: not found")
    return list(dict.fromkeys(files)), probs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help=f"repo-relative files or dirs to lint (default {DEFAULT})")
    ap.add_argument("--repo", default=".")
    args = ap.parse_args(argv)
    repo = os.path.abspath(args.repo)
    default = [DEFAULT] if os.path.isdir(os.path.join(repo, DEFAULT)) else []
    files, out = collect(repo, args.paths or default)
    product = product_names(repo)
    rust = [rel for rel in files if rel.endswith(".rs")]
    crates, libs = (crate_names(repo), lib_crates(repo)) if rust else (set(), {})
    for rel in files:
        with open(os.path.join(repo, rel), encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
        found = (lint_rust(text, crates, libs.get(rel, ())) if rel.endswith(".rs")
                 else lint_source(rel, text, product))
        out += [f"{rel}:{line}: {msg}" for line, msg in found]
    sys.stdout.reconfigure(errors="replace")
    print("\n".join(out + [f"ENTRY LINT FAIL {len(out)} problems" if out else f"ENTRY LINT OK {len(files)} files"]))
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main())
