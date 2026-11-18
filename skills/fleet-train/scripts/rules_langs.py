"""Per-language scanners for rules_gate.py. A new language is one more Lang in LANGS."""
from __future__ import annotations

import json, os, re, subprocess, sys
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
class TreeTests:
    """Test code a language finds from the whole tree, beyond file names: `test(rel)`, its test-only packages (dirs)
    and `error` when the package metadata could not be read (then no package is test-only)."""
    test: Callable[[str], bool] = lambda rel: False
    packages: tuple = ()
    error: str = ""


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
    tree: Callable[[Path], TreeTests] = lambda cwd: TreeTests()  # test code the tree shows (built once per gate call)
    manifests: tuple = ()  # file names whose change can move a package between test and product


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
    if not rel.endswith(".rs") or {"tests", "benches"} & set(parts[:-1]) or RUST_TEST_NAME.search(parts[-1]):
        return False
    return parts[0] == "src" or (parts[0] == "crates" and len(parts) > 3 and parts[2] == "src")


def dev_only_crates(cwd) -> tuple:
    """The crate dirs of the workspace at `cwd` used only as a [dev-dependencies] entry, from `cargo metadata
    --no-deps` (computed, never a hand list; a crate no member depends on is product). No Cargo.toml: (). RuntimeError
    when cargo gives no metadata."""
    root = Path(cwd).resolve()
    if not (root / "Cargo.toml").exists():
        return ()
    try:
        r = subprocess.run(["cargo", "metadata", "--no-deps", "--format-version", "1", "--offline"], cwd=str(root),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        meta = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        raise RuntimeError(f"cargo metadata failed in {root}: {e}") from e
    if meta is None:
        raise RuntimeError(f"cargo metadata failed in {root}: {r.stderr.strip()[-300:]}")
    key = lambda p: os.path.normcase(os.path.realpath(p))  # noqa: E731
    dirs = {key(Path(p["manifest_path"]).parent): Path(p["manifest_path"]).parent for p in meta["packages"]}
    kinds = {}
    for p in meta["packages"]:
        for d in p["dependencies"]:
            if d.get("path") and key(d["path"]) in dirs:
                kinds.setdefault(key(d["path"]), set()).add(d.get("kind") or "normal")
    out = []
    for k, ks in kinds.items():
        try:
            rel = Path(dirs[k]).resolve().relative_to(root).as_posix()
        except ValueError:  # a member outside the tree holds none of its files
            continue
        if ks == {"dev"} and rel != ".":
            out.append(rel + "/")
    return tuple(sorted(out))


class RustModules:
    """Out-of-line test modules of the tree at `cwd`: a `#[cfg(test)] mod x;` file and its submodules."""

    def __init__(self, cwd):
        self.cwd, self._mods, self._test = Path(cwd), {}, {}

    def test_module(self, rel: str) -> bool:
        """Whether every declaration of module file `rel` (in the parent files that exist) is under `#[cfg(test)]`
        or in a test module itself."""
        if rel not in self._test:
            self._test[rel] = False  # a cycle of declarations is no test module
            name, parents = module_parents(rel)
            seen = [test or self.test_module(p) for p in parents for test in self.decls(p).get(name, ())]
            self._test[rel] = bool(seen) and all(seen)
        return self._test[rel]

    def decls(self, rel: str) -> dict:
        """{module name: [under #[cfg(test)]]} of the out-of-line `mod x;` declarations in file `rel`."""
        if rel not in self._mods:
            p, out = self.cwd / rel, {}
            if p.is_file():
                rs = qs_import("rust_src")
                toks = rs.tokens(p.read_text(encoding="utf-8", errors="replace"))
                test = {k for a, b in rs.cfg_test_spans(toks) for k in range(a, b + 1)}
                for i in range(len(toks) - 6):  # `#[cfg(test)] [pub(..)] mod x;`, rust_src's attribute match
                    if [t for _, t, _ in toks[i:i + 6]] == ["#", "[", "cfg", "(", "test", ")"]:
                        j = i + 7
                        while j < len(toks) and toks[j][1] not in ("{", ";"):
                            j += 1
                        if j < len(toks) and toks[j][1] == ";" and toks[j - 2][1] == "mod":
                            test.add(j - 2)
                for i in range(len(toks) - 2):
                    if toks[i][1] == "mod" and toks[i + 1][0] == "ident" and toks[i + 2][1] == ";":
                        out.setdefault(toks[i + 1][1], []).append(i in test)
            self._mods[rel] = out
        return self._mods[rel]


def module_parents(rel: str) -> tuple:
    """(module name, [files that may declare it]) of a .rs file under a src/ dir; ("", []) for a crate root."""
    parts = rel.split("/")
    src = parts.index("src") if "src" in parts else -1
    rest = parts[src + 1:]
    if src < 0 or rest in (["lib.rs"], ["main.rs"]) or rest[:1] == ["bin"]:
        return "", []
    name, up = (parts[-2], parts[:-2]) if rest[-1] == "mod.rs" else (parts[-1][:-3], parts[:-1])
    if len(up) == src + 1:
        return name, ["/".join(up + ["lib.rs"]), "/".join(up + ["main.rs"])]
    return name, ["/".join(up[:-1] + [up[-1] + ".rs"]), "/".join(up + ["mod.rs"])]


def rust_tree(cwd) -> TreeTests:
    """Rust test code beyond file names: out-of-line `#[cfg(test)]` module files, crates only dev-dependencies use."""
    try:
        dev, err = dev_only_crates(cwd), ""
    except RuntimeError as e:
        dev, err = (), str(e)
    mods = RustModules(cwd)
    return TreeTests(lambda rel: rel.startswith(dev) or mods.test_module(rel), dev, err)


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
    tree=rust_tree, manifests=("Cargo.toml",),
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
