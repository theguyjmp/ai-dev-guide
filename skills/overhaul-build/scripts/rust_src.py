"""rust_src.py: a small Rust lexer for the plan checks (entry_match, entry_lint, shape_check, plan_validate).

Best effort, Python standard library only: comments are dropped; string, raw string, byte string and char literals
become one `str` token; lifetimes are not chars. Each token is (kind, text, line) with kind ident, str, num or punct;
`::` is one punct token. Enough to find fn bodies, #[test] attributes, calls and their string arguments.
"""
import re

TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<line>//[^\n]*)
  | (?P<raw>b?r(?P<hashes>\#*)")
  | (?P<str>b?"(?:[^"\\]|\\.)*")
  | (?P<char>b?'(?:[^'\\\n]|\\.[^']*)')
  | (?P<life>'[A-Za-z_]\w*)
  | (?P<ident>[A-Za-z_]\w*)
  | (?P<num>\d[\w.]*)
  | (?P<punct>::|->|=>|\.\.=?|.)
""", re.X | re.S)
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "'": "'"}


def unescape(body):
    return re.sub(r"\\(u\{[0-9a-fA-F]+\}|x[0-9a-fA-F]{2}|\n\s*|.)", lambda m: _esc(m.group(1)), body, flags=re.S)


def _esc(e):
    if e.startswith("u{"):
        return chr(int(e[2:-1], 16))
    if e.startswith("x") and len(e) == 3:
        return chr(int(e[1:], 16))
    return "" if e.startswith("\n") else ESCAPES.get(e, e)


def tokens(text):
    """[(kind, text, line)]; a string token's text is its value."""
    out, pos, line, n = [], 0, 1, len(text)
    while pos < n:
        if text.startswith("/*", pos):
            depth, i = 0, pos
            while i < n:
                if text.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif text.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                    if not depth:
                        break
                else:
                    i += 1
            line += text.count("\n", pos, i)
            pos = i
            continue
        m = TOKEN.match(text, pos)
        kind = m.lastgroup if m.lastgroup != "hashes" else "raw"
        end = m.end()
        if kind == "raw":
            close = '"' + m.group("hashes")
            stop = text.find(close, end)
            stop = n if stop < 0 else stop
            out.append(("str", text[end:stop], line))
            end = min(n, stop + len(close))
        elif kind == "str":
            body = m.group(kind)
            out.append(("str", unescape(body[body.index('"') + 1:-1]), line))
        elif kind == "char":
            out.append(("str", unescape(m.group(kind).split("'", 1)[1][:-1]), line))
        elif kind in ("ident", "num", "punct", "life"):
            out.append((kind if kind != "life" else "punct", m.group(kind), line))
        line += text.count("\n", pos, end)
        pos = end
    return out


def match_close(toks, i):
    """Index of the bracket closing the one at toks[i] ( ( [ { ), or len(toks)."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = []
    for j in range(i, len(toks)):
        k, t, _ = toks[j]
        if k != "punct":
            continue
        if t in pairs:
            stack.append(pairs[t])
        elif stack and t == stack[-1]:
            stack.pop()
            if not stack:
                return j
    return len(toks)


def is_test_attr(toks, i):
    """True when toks[i] starts `#[test]` or `#[<path>::test]` (tokio::test and the like)."""
    if not (toks[i][1] == "#" and i + 1 < len(toks) and toks[i + 1][1] == "["):
        return False
    j = match_close(toks, i + 1)
    inner = [t for _, t, _ in toks[i + 2:j]]
    return bool(inner) and inner[-1] == "test" and all(t == "::" or re.fullmatch(r"\w+", t) for t in inner)


def functions(toks):
    """[{name, line, end_line, body: (start, end) token span or None, test: bool, start}] for every `fn` item."""
    out = []
    for i, (k, t, ln) in enumerate(toks):
        if not (k == "ident" and t == "fn" and i + 1 < len(toks) and toks[i + 1][0] == "ident"):
            continue
        test, j = False, i - 1
        while j >= 0 and toks[j][0] == "ident" and toks[j][1] in ("pub", "async", "unsafe", "const", "extern"):
            j -= 1
        while j >= 0 and toks[j][1] == "]":  # walk back over attributes
            depth, a = 0, j
            while a >= 0:
                depth += {"]": 1, "[": -1}.get(toks[a][1], 0) if toks[a][0] == "punct" else 0
                if depth == 0:
                    break
                a -= 1
            if a > 0 and toks[a - 1][1] == "#":
                test = test or is_test_attr(toks, a - 1)
                j = a - 2
            else:
                break
        depth, body, m = 0, None, i + 2
        while m < len(toks):
            kk, tt, _ = toks[m]
            if kk == "punct" and tt in "([":
                m = match_close(toks, m) + 1
                continue
            if kk == "punct" and tt == ";":
                break
            if kk == "punct" and tt == "{":
                body = (m, match_close(toks, m))
                break
            m += 1
        end_line = toks[min(body[1], len(toks) - 1)][2] if body else ln
        out.append({"name": toks[i + 1][1], "line": ln, "end_line": end_line, "body": body, "test": test, "start": i})
    return out


def cfg_test_spans(toks):
    """Token spans of `#[cfg(test)] mod x { ... }` blocks (in-crate unit tests)."""
    spans = []
    for i in range(len(toks) - 6):
        if [t for _, t, _ in toks[i:i + 6]] == ["#", "[", "cfg", "(", "test", ")"]:
            j = i + 7
            while j < len(toks) and toks[j][1] != "{" and toks[j][1] != ";":
                j += 1
            if j < len(toks) and toks[j][1] == "{":
                spans.append((i, match_close(toks, j)))
    return spans


def macro_spans(toks, name):
    """Token spans of `name! { ... }` / `name!( ... )` invocations."""
    spans = []
    for i in range(len(toks) - 2):
        if toks[i][1] == name and toks[i + 1][1] == "!" and toks[i + 2][1] in "({[":
            spans.append((i, match_close(toks, i + 2)))
    return spans


def calls(toks, start, end):
    """(path, args, texts) for every call, macro call, array literal and `.arg(..)`/`.args([..])` chain in a span.
    args: each top-level item, a str for a lone string literal, else None (any expression); texts: every string
    literal in the call, `{..}` format holes as \\x01."""
    out, i = [], start
    while i < end:
        k, t, _ = toks[i]
        if k == "punct" and t in "([":
            close = min(match_close(toks, i), end)
            items, cur = [], []
            for j in range(i + 1, close):
                if toks[j][1] == "," and toks[j][0] == "punct" and _depth(toks, i + 1, j) == 0:
                    items.append(cur)
                    cur = []
                else:
                    cur.append(toks[j])
            if cur:
                items.append(cur)
            args = [it[0][1] if len(it) == 1 and it[0][0] == "str" else None for it in items]
            texts = [re.sub(r"\{[^{}]*\}", "\x01", tk[1]) for tk in toks[i + 1:close] if tk[0] == "str"]
            out.append((_path_before(toks, i), args, texts))
        i += 1
    return out + _arg_chains(toks, start, end)


def _depth(toks, a, b):
    d = 0
    for k, t, _ in toks[a:b]:
        if k == "punct":
            d += 1 if t in "([{" else -1 if t in ")]}" else 0
    return d


def _path_before(toks, i):
    """`a::b::c` written just before toks[i] (a `!` of a macro call is skipped), else ''."""
    j = i - 1
    if j >= 0 and toks[j][1] == "!":
        j -= 1
    parts = []
    while j >= 0 and toks[j][0] == "ident":
        parts.append(toks[j][1])
        if j >= 1 and toks[j - 1][1] == "::":
            j -= 2
        else:
            break
    return "::".join(reversed(parts))


def _arg_chains(toks, start, end):
    """Command-builder chains `.arg("a").arg(x).args(["b", "c"])` as one argument list."""
    out, i = [], start
    while i < end - 2:
        if toks[i][1] == "." and toks[i + 1][1] in ("arg", "args") and toks[i + 2][1] == "(":
            args, texts = [], []
            while i < end - 2 and toks[i][1] == "." and toks[i + 1][1] in ("arg", "args") and toks[i + 2][1] == "(":
                close = min(match_close(toks, i + 2), end)
                inner = toks[i + 3:close]
                if toks[i + 1][1] == "arg":
                    args.append(inner[0][1] if len(inner) == 1 and inner[0][0] == "str" else None)
                else:
                    lst = [x for x in inner if x[1] not in ("&", "[", "]", "vec", "!")]
                    parts = [[]]
                    for x in lst:
                        if x[1] == ",":
                            parts.append([])
                        else:
                            parts[-1].append(x)
                    args += [p[0][1] if len(p) == 1 and p[0][0] == "str" else None for p in parts if p]
                texts += [x[1] for x in inner if x[0] == "str"]
                i = close + 1
            out.append(("", args, texts))
        else:
            i += 1
    return out


def mod_decls(toks):
    """Names of `mod x;` declarations (out-of-line modules)."""
    return [toks[i + 1][1] for i in range(len(toks) - 2)
            if toks[i][1] == "mod" and toks[i + 1][0] == "ident" and toks[i + 2][1] == ";"]
