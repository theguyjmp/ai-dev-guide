"""entry_match.py: each function's acceptance tests must invoke the exact entry its FUNCTIONS row names.

Used by plan_validate.py. An entry is `cli: <prog> <words>` or `http: <METHOD> <route>` (the `cli:`/`http:` prefix
may be left out). Grammar of the words: a literal word or flag, `<name>` or an all-caps `NAME` (one argument), `[...]` (optional),
`...` after an item (repeat it). A trailing `(...)` note is ignored.

CLI: some call in the named tests must have positional string arguments (from the first string literal on) that
match the grammar, with or without the program name. A non-literal argument matches one placeholder or word.
HTTP: some call must hold a string matching the route (`<id>` matches one path segment; a `?query` may follow) and,
for a method other than GET, the method as a string literal.
Rust acceptance tests (`tests/acceptance/<f>.rs::<fn>`, read by rust_src.py, best effort): a call is any call, macro
call or array literal in the named `fn`s, and a `.arg(..)`/`.args([..])` chain counts as one argument list, so
`run_cli(&dir, &["cart", "add", "MUG1", "2"])` and `Command::new(bin).arg("cart").arg("add")` both match. A CLI row's
test file, or a `mod x;` helper beside it, must also run the built binary: `env!("CARGO_BIN_EXE_<bin>")` (or
assert_cmd's `cargo_bin(`). Library entry `lib: <crate>::<path>::<fn>` (Rust only): some call's path must be a
suffix of it by whole `::` segments, and a call shorter than the full path needs a `use` of the crate in the file.
"""
import ast
import re

import rust_src  # beside this script

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
ANY = object()  # a call argument that is not a string literal


def tokens(entry):
    """(kind, tokens) of an entry cell; kind is cli, http or '' (not checkable)."""
    text = re.sub(r"\([^)]*\)\s*$", "", entry.strip().strip("`")).strip()
    m = re.match(r"(cli|http|ui|gui|lib)\s*:\s*", text, re.I)
    kind = m.group(1).lower() if m else ""
    text = text[m.end():] if m else text
    parts = re.findall(r"\[|\]|<[^>]*>|[^\s\[\]]+", text)
    if not kind:
        kind = "http" if parts and parts[0].upper() in HTTP_METHODS else "cli"
    return (kind if kind in ("cli", "http", "lib") else ""), parts


def grammar_regex(parts):
    """A regex over a token stream (each argument followed by NUL) for the CLI grammar."""
    out = []
    for tok in parts:
        if tok == "[":
            out.append("(?:")
        elif tok == "]":
            out.append(")?")
        elif tok == "...":
            if out:
                out[-1] = ")*" if out[-1] == ")?" else f"(?:{out[-1]})+"
        elif tok.startswith("<") or re.fullmatch(r"[A-Z][A-Z0-9_]*", tok):
            out.append("(?:[^\0]*)\0")
        else:
            out.append(f"(?:{re.escape(tok)}|\x01)\0")
    return re.compile("".join(out))


def call_args(node):
    """Positional arguments of a call from its first string literal on: str for a literal, ANY otherwise."""
    if isinstance(node, tuple):  # a Rust call: (path, args, texts), None for a non-literal argument
        args = node[1]
    else:
        if any(isinstance(a, ast.Starred) for a in node.args):
            return None
        args = [a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None for a in node.args]
    first = next((n for n, a in enumerate(args) if a is not None), len(args))
    return [a if a is not None else ANY for a in args[first:]]


def stream(args):
    return "".join(("\x01" if a is ANY else a.replace("\0", "")) + "\0" for a in args)


def test_calls(tree, names):
    """Every call inside the named test functions."""
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in names:
            yield from (n for n in ast.walk(fn) if isinstance(n, ast.Call))


def cli_ok(calls, parts):
    full, short = grammar_regex(parts), grammar_regex(parts[1:]) if len(parts) > 1 else None
    seen = []
    for call in calls:
        args = call_args(call)
        if not args:
            continue
        s = stream(args)
        if full.fullmatch(s) or (short and short.fullmatch(s)):
            return True, ""
        heads = {parts[0]} | ({parts[1]} if len(parts) > 1 else set())
        if args[0] in heads and not seen:
            seen.append(" ".join("<expr>" if a is ANY else a for a in args))
    return False, (f"; it runs `{seen[0]}`" if seen else "")


def route_regex(route):
    body = "".join("[^/?]+" if p.startswith("<") or p.startswith("{") else re.escape(p)
                   for p in re.split(r"(<[^>]*>|\{[^}]*\})", route))
    return re.compile(body + r"(?:\?.*)?")


def strings(call):
    """String literals and f-string patterns among a call's arguments, as regex-ready text and literals."""
    if isinstance(call, tuple):
        return list(call[2])
    out = []
    for a in [*call.args, *(k.value for k in call.keywords)]:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            out.append(a.value)
        elif isinstance(a, ast.JoinedStr):
            out.append("".join(v.value if isinstance(v, ast.Constant) else "\x01" for v in a.values))
    return out


def http_ok(calls, parts):
    method = parts[0].upper() if parts and parts[0].upper() in HTTP_METHODS else "GET"
    route = next((p for p in parts if p.startswith("/")), None)
    if not route:
        return True, ""
    rx = route_regex(route)
    for call in calls:
        texts = strings(call)
        hit = any(rx.fullmatch(t.replace("\x01", "x")) or rx.fullmatch(re.sub(r"^[a-z]+://[^/]*", "", t))
                  for t in texts)
        if hit and (method == "GET" or any(t.upper() == method for t in texts)):
            return True, ""
    return False, ""


def lib_ok(calls, parts, uses=()):
    """A Rust call whose path is a whole-segment suffix of the `lib:` entry path; a partial path needs a `use` of
    the entry's crate (uses: the crate names the test files `use`)."""
    want = parts[0].replace("-", "_").split("::") if parts else []
    for call in calls:
        if not isinstance(call, tuple) or not call[0]:
            continue
        have = call[0].split("::")
        if len(have) <= len(want) and want[-len(have):] == have and (len(have) == len(want) or want[0] in uses):
            return True, ""
    return False, ""


def matches(kind, calls, parts, uses=()):
    if kind == "lib":
        return lib_ok(calls, parts, uses)
    return (cli_ok if kind == "cli" else http_ok)(calls, parts)


def rust_uses(toks):
    """Crate names `use`d at the top of a path (`use shop_core::cart;` -> shop_core)."""
    return {toks[i + 1][1] for i in range(len(toks) - 1) if toks[i][1] == "use" and toks[i + 1][0] == "ident"}


def read_calls(repo, r):
    """(every call inside a row's acceptance tests, crates the Rust files `use`, the text of each Rust test file
    and of the `mod x;` helpers beside it)."""
    calls, uses, rust_text = [], set(), []
    names = {name for _, name in r.get("tests") or []}
    for path in sorted({item.split("::")[0] for item, _ in r.get("tests") or []}):
        try:
            text = (repo / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.endswith(".rs"):
            toks = rust_src.tokens(text)
            for f in rust_src.functions(toks):
                if f["name"] in names and f["body"]:
                    calls += rust_src.calls(toks, *f["body"])
            uses |= rust_uses(toks)
            rust_text.append(text)
            here = (repo / path).parent
            for mod in rust_src.mod_decls(toks):
                for helper in (here / f"{mod}.rs", here / mod / "mod.rs"):
                    if helper.is_file():
                        rust_text.append(helper.read_text(encoding="utf-8", errors="replace"))
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        calls += list(test_calls(tree, names))
    return calls, uses, rust_text


RUNS_BINARY = re.compile(r"CARGO_BIN_EXE_|cargo_bin\s*\(")


def entry_problems(repo, rows, flabel):
    """Rows whose acceptance tests never invoke the row's entry, as plan_validate problem lines."""
    problems = []
    for r in rows:
        kind, parts = tokens(r.get("entry", ""))
        if not kind or not parts or not r.get("tests"):
            continue
        calls, uses, rust_text = read_calls(repo, r)
        if kind == "lib" and not rust_text:
            continue  # a library entry is checked in Rust tests only
        where = f"{flabel}:{r.get('line', '?')}: {r.get('id', '?')}"
        ok, hint = matches(kind, calls, parts, uses)
        if not ok:
            problems.append(f"{where} acceptance never invokes its entry "
                            f"`{r['entry'].strip().strip('`')}`{hint}: drive the exact entry, or fix the row")
        if kind == "cli" and rust_text and not any(RUNS_BINARY.search(t) for t in rust_text):
            problems.append(f"{where} acceptance does not run the built binary: drive the CLI through "
                            'env!("CARGO_BIN_EXE_<bin>") in the test file or its `mod` helper')
    return problems


def row_calls(repo, r):
    """Every call inside a row's acceptance tests."""
    return read_calls(repo, r)[0]


def cross_problems(repo, rows, flabel, waits_on):
    """Rows whose acceptance tests drive another row's entry without waiting on that row's behaviour: the builder
    cannot make such a test green inside its own paths (trial 8: F02's test ran F05's `stock price`).
    waits_on(id) is the set of rows whose real behaviour the row's build waits for, transitively."""
    problems = []
    entries = [(r.get("id", "?"), *tokens(r.get("entry", ""))) for r in rows]
    for r in rows:
        calls, uses, _ = read_calls(repo, r)
        if not calls:
            continue
        mine, before, own = r.get("id", "?"), waits_on(r.get("id", "?")), tokens(r.get("entry", ""))[1]
        for oid, kind, parts in entries:
            if oid == mine or oid in before or not kind or not parts or parts == own:  # same entry: not a cross call
                continue
            ok, _ = matches(kind, calls, parts, uses)
            if ok:
                problems.append(f"{flabel}:{r.get('line', '?')}: {mine} acceptance drives {oid}'s entry "
                                f"`{' '.join(parts)}` but {mine} does not wait on {oid}'s behaviour: add "
                                f"{oid} to deps with needs {oid}:behaviour, or set the state up another way")
    return problems

