"""Per-language scanners for rules_gate.py. A new language is one more Lang in LANGS."""
from __future__ import annotations

import os, re, sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


def qs() -> Path:
    return Path(os.environ.get("HARNESS_QS", str(Path.home() / ".claude/skills/overhaul-build/scripts")))


def qs_import(name):
    if str(qs()) not in sys.path:
        sys.path.insert(0, str(qs()))
    return __import__(name)


@dataclass(frozen=True)
class Lang:
    name: str
    roots: tuple  # git ls-files pathspecs
    is_product: Callable[[str], bool]
    tokens: Callable[[str], list]  # [(kind ident/num/str/punct, text, line)]
    comments: Callable[[str], list]  # [(text, line)]
    items: Callable[[list], list]  # [(public type name, line)]
    code: dict = field(default_factory=dict)  # rule id -> regexes over statements
    comment: dict = field(default_factory=dict)  # rule id -> regexes over comments
    keep_empty_braces: bool = False  # JS: an empty catch is one statement
    skip_spans: Callable[[list], list] = lambda toks: []  # test code inside a product file


def statements(lang: Lang, text: str) -> list:
    """[(compact text, first line, last line)] of the product part of a file."""
    toks = lang.tokens(text)
    skip = {k for a, b in lang.skip_spans(toks) for k in range(a, b + 1)}
    out, cur = [], []

    def flush():
        if cur:
            s = ""
            for i, (kind, t, _) in enumerate(cur):
                if i and kind in ("ident", "num") and cur[i - 1][0] in ("ident", "num"):
                    s += " "
                s += '""' if kind == "str" else t
            out.append((s, cur[0][2], cur[-1][2]))
            cur.clear()

    depth = 0  # ( and [ nesting: the `;` of `[f64; 3]` ends no statement
    for i, tok in enumerate(toks):
        if i in skip:
            flush()
            continue
        t = tok[1]
        if tok[0] == "punct" and t in "([":
            depth += 1
        elif tok[0] == "punct" and t in ")]":
            depth = max(0, depth - 1)
        empty = lang.keep_empty_braces and (
            (t == "{" and i + 1 < len(toks) and toks[i + 1][1] == "}") or (t == "}" and i and toks[i - 1][1] == "{"))
        if t in ("{", "}") and not empty:
            flush()
            depth = 0
        elif (t == ";" or (empty and t == "}")) and not depth:  # an empty block ends its statement
            cur.append(tok)
            flush()
        else:
            cur.append(tok)
    flush()
    return out


# ------------------------------------------------------------------ Rust
RUST_TEST_NAME = re.compile(r"(^tests\.rs$|_tests?\.rs$)")
DECODE = r"\b(?:parse|from_value|from_str|from_slice|from_reader)(?:::<.*?>)?\([^()]*(?:\([^()]*\)[^()]*)*\)"


def rust_product(rel: str) -> bool:
    parts = rel.split("/")
    if not rel.endswith(".rs") or "tests" in parts[:-1] or RUST_TEST_NAME.search(parts[-1]):
        return False
    return parts[0] == "src" or (parts[0] == "crates" and len(parts) > 3 and parts[2] == "src")


def rust_items(toks: list) -> list:
    rs = qs_import("rust_src")
    skip = {k for a, b in rs.cfg_test_spans(toks) for k in range(a, b + 1)}
    out = []
    for i, (_, t, line) in enumerate(toks):
        if t != "pub" or i in skip:
            continue
        j = i + 1
        if j < len(toks) and toks[j][1] == "(":
            j = rs.match_close(toks, j) + 1
        if j + 1 < len(toks) and toks[j][1] in ("struct", "enum", "trait", "type", "union") and toks[j + 1][0] == "ident":
            out.append((toks[j + 1][1], line))
    return out


RUST = Lang(
    "rust", ("crates", "src"), rust_product,
    tokens=lambda text: qs_import("rust_src").tokens(text),
    comments=lambda text: [],
    items=rust_items,
    skip_spans=lambda toks: qs_import("rust_src").cfg_test_spans(toks),
    code={
        "swallow": (r"^let _=(?!write(?:ln)?!\().*\(",  # writeln! into a String cannot fail
                    r"^(?!let |return\b)[^=]*\.ok\(\);$",  # a statement that only drops the error
                    r"(?<!::)\bErr\(_\)=>(?!Err\(|return Err|Refusal|.*\?)",  # Val::Err(_) is a value
                    r"\.unwrap_or_else\(\|_\|(?!Err\(|Refusal)",
                    DECODE + r"(?:\.ok\(\))?\)*\.unwrap_or(?:_default|_else)?\(",
                    r"\.as_(?:array|object)(?:_mut)?\(\).*\.(?:unwrap_or(?:_default|_else)?|map_or(?:_else)?)\("),
        "expect": (r"\.expect\(",),
        "panic": (r"\bpanic!\(",),
        "allow": (r"#!?\[allow\(",),
    })


# ------------------------------------------------------------------ JavaScript / TypeScript
JS_EXTS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts")
JS_TEST = re.compile(r"(^|/)(tests?|__tests__|node_modules|dist|build|coverage)/|[._-](test|spec)\.[mc]?[jt]sx?$|\.d\.ts$")
JS_TOKEN = re.compile(r"""(?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<str>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)
  | (?P<ident>[A-Za-z_$][\w$]*) | (?P<num>\d[\w.]*) | (?P<punct>=>|\?\.|[^\s\w])""", re.X | re.S)


def js_lex(text: str) -> list:
    """[(kind, text, line)] with comments; regex literals are not recognised."""
    out, line, pos = [], 1, 0
    for m in JS_TOKEN.finditer(text):
        line += text.count("\n", pos, m.start())
        out.append((m.lastgroup, m.group(), line))
        line += m.group().count("\n")
        pos = m.end()
    return out


def js_items(toks: list) -> list:
    out = []
    for i, (_, t, _) in enumerate(toks):
        if t != "export":
            continue
        j = i + 1
        while j < len(toks) and toks[j][1] in ("default", "abstract", "declare"):
            j += 1
        if j + 1 < len(toks) and toks[j][1] in ("class", "interface", "type", "enum") and toks[j + 1][0] == "ident":
            out.append((toks[j + 1][1], toks[j + 1][2]))
    return out


NODE = Lang(
    "node", (".",), lambda rel: rel.endswith(JS_EXTS) and not JS_TEST.search(rel),
    tokens=lambda text: [t for t in js_lex(text) if t[0] != "comment"],
    comments=lambda text: [(t, n) for k, t, n in js_lex(text) if k == "comment"],
    items=js_items,
    keep_empty_braces=True,
    code={"swallow": (r"\bcatch(?:\(\w*\))?\{\}",  # an empty catch block
                      r"\.catch\(\(?[\w,]*\)?=>(?:\{\}|null|undefined|\[\]|\"\"|false)\)")},
    comment={"allow": (r"eslint-disable|@ts-ignore|@ts-nocheck",)})

LANGS = {lang.name: lang for lang in (RUST, NODE)}


def detect(cwd) -> str:
    """node with a package.json and no Cargo.toml, else rust."""
    return "node" if (Path(cwd) / "package.json").exists() and not (Path(cwd) / "Cargo.toml").exists() else "rust"
