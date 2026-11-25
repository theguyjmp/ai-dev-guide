"""Ratcheted rule checks for the land train: rules no lint config holds. No build, no LLM.

Run by harness.quality_problems (train, pre-push) and cloud sessions. Per product file (test code out): swallow (a
`// default: <why>` or `// best-effort: <why>` comment exempts), expect, panic, allow, owner (a public type named like
another module's `Forbidden types:` stem; `Overlaps: <path>::<Name>` exempts), text (overhaul.toml [text] caps). A file
fails above its count in <state>/rules-debt.json; debt is per file and never moves. Duplicates: an exported name
another module declares, a function grown into a near copy of a sibling (debt `dups`; overhaul.toml [rules]
name_conventions exempts names). The plan check adds no plan_validate line.

    py -3 rules_gate.py check   [--dir D] [--base main] [--state S] [--lang L]   exit 1 on any problem
    py -3 rules_gate.py count   [--dir D] [--files] [--lang L]
    py -3 rules_gate.py init    [--dir D] [--state S] [--lang L]   add the debt of every check the file lacks
    py -3 rules_gate.py tighten [--dir D] [--state S] [--lang L]   lower each debt entry to D's counts (D = main)
"""
from __future__ import annotations

import difflib, functools, json, os, re, subprocess, sys, threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import rules_langs as rl

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11: the text caps are off
    tomllib = None

DEBT = "rules-debt.json"
PLAN_CACHE = "plan-validate-base.json"
ARCH = "spec/ARCHITECTURE.md"
DECISIONS = "spec/DECISIONS.md"
FUNCTIONS = "spec/FUNCTIONS.md"
MARKER = re.compile(r"//\s*(?:default|best-effort)\s*:\s*\S")
TEXT_FILE = re.compile(r"^(?:spec/(?:packets/)?[^/]+\.md|CLAUDE\.md)$")
TEXT_GENERATED = {"spec/INDEX.md"}
DECISION_LINE = re.compile(r"^D\d+[a-z]?:")
LINE_NO = re.compile(r"(\.(?:md|rs|toml|py|[mc]?[jt]sx?)):\d+(?::\d+)?")
STUB_LINE = re.compile(r"^\S+ (\S+): no stub in ")
PLAN_MODES = ("off", "spec", "always")  # the gate: always
CLONE_MIN_TOKENS = 40
CLONE_COVER = 0.8


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    fix: str


RULES = (
    Rule("swallow", "dropped error or silent default on damaged data",
         "return the typed error (or log and refuse); a real default for an OPTIONAL input takes a same-line "
         "`// default: <why>`, a best-effort cleanup `// best-effort: <why>`"),
    Rule("expect", "`.expect(` in product code", "return a typed error; tests may expect"),
    Rule("panic", "`panic!` in product code", "return a typed error"),
    Rule("allow", "a silenced lint in product code (never silence a lint)",
         "fix what the lint reports; dead code is deleted, not allowed"),
    Rule("text", "text over its [text] cap in overhaul.toml (it floods agent context)",
         "say it shorter; history goes in the commit, the contract in the test, a work packet in spec/packets/; "
         "never raise a cap"),
    Rule("owner", "type named like another module's `Forbidden types:` stem (a second owner)",
         "extend the owning module (ARCHITECTURE `Forbidden types:`) and call it; a true overlap needs an "
         "`Overlaps: <path>::<Name>` entry under the owner, by plan ruling"),
)
RULE = {r.id: r for r in RULES}


@dataclass
class Hit:
    rule: str
    line: int
    text: str


@dataclass
class Arch:
    """The owner map: module -> its path prefixes and forbidden type stems, plus the Overlaps exemptions."""
    paths: dict = field(default_factory=dict)
    stems: dict = field(default_factory=dict)
    overlaps: set = field(default_factory=set)  # {"<path>::<Name>"}

    def module_of(self, rel: str) -> str | None:
        best = max(((m, p) for m, ps in self.paths.items() for p in ps if rel.startswith(p)),
                   key=lambda mp: len(mp[1]), default=(None, ""))
        return best[0]


@dataclass(frozen=True)
class TextCaps:
    """The [text] caps of overhaul.toml, in words; 0 = that cap is off."""
    line_words: int = 0
    decision_line_words: int = 0
    module_section_words: int = 0
    functions_prose_words: int = 0
    always_read_words: int = 0

    @classmethod
    def parse(cls, toml_text: str) -> "TextCaps":
        t = toml_table(toml_text, "text")
        return cls(**{f.name: int(t.get(f.name) or 0) for f in fields(cls)})

    @classmethod
    def of(cls, cwd) -> "TextCaps":
        p = Path(cwd) / "overhaul.toml"
        return cls.parse(p.read_text(encoding="utf-8", errors="replace") if p.exists() else "")

    def on(self) -> bool:
        return any(getattr(self, f.name) for f in fields(self))

    def loosened(self, base: "TextCaps") -> list:
        """The caps this one raises or turns off against `base`."""
        return [f.name for f in fields(self) if getattr(base, f.name)
                and not 0 < getattr(self, f.name) <= getattr(base, f.name)]


def toml_table(toml_text: str | None, name: str) -> dict:
    """Table `name` of overhaul.toml; {} when absent or unreadable (plan_validate reports a broken file)."""
    if tomllib is None or not (toml_text or "").strip():
        return {}
    try:
        return tomllib.loads(toml_text).get(name) or {}
    except tomllib.TOMLDecodeError:
        return {}


def git(cwd, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def git_out(cwd, *args, ok=(0,)) -> str:
    """stdout of a git call; RuntimeError on another exit code (no verdict is never a pass)."""
    r = git(cwd, *args)
    if r.returncode not in ok:
        raise RuntimeError(f"git {' '.join(args[:3])} failed in {cwd}: {r.stderr.strip()[-300:]}")
    return r.stdout


class Snapshot:
    """File reads of one tree: the work tree at `cwd` (rev "") or commit `rev`."""

    def __init__(self, cwd, rev: str = ""):
        self.cwd = Path(cwd)
        self.rev = git_out(cwd, "rev-parse", "--verify", f"{rev}^{{commit}}").strip() if rev else ""

    def files(self, roots) -> list:
        if self.rev:
            return git_out(self.cwd, "ls-tree", "-r", "--name-only", self.rev, "--", *roots).splitlines()
        return [f for f in git_out(self.cwd, "ls-files", "--", *roots).splitlines() if (self.cwd / f).is_file()]

    def grep(self, words, roots) -> list:
        """Files under `roots` holding any of `words` as a whole word."""
        words, out = sorted(words), set()
        for k in range(0, len(words), 100):
            pats = [a for w in words[k:k + 100] for a in ("-e", w)]
            lines = git_out(self.cwd, "grep", "-l", "-w", "-F", *pats, *([self.rev] if self.rev else []), "--", *roots,
                            ok=(0, 1)).splitlines()
            out.update(x[len(self.rev) + 1:] if self.rev else x for x in lines)
        return sorted(out)

    def texts(self, rels) -> dict:
        """{rel: text, None when absent}."""
        rels = list(rels)
        if not self.rev:
            return {f: (self.cwd / f).read_text(encoding="utf-8", errors="replace") if (self.cwd / f).is_file()
                    else None for f in rels}
        r = subprocess.run(["git", "cat-file", "--batch"], cwd=str(self.cwd), capture_output=True, check=False,
                           input="".join(f"{self.rev}:{f}\n" for f in rels).encode("utf-8"))
        if r.returncode:
            raise RuntimeError(f"git cat-file failed in {self.cwd}: {r.stderr.decode(errors='replace')[-300:]}")
        out, pos, data = {}, 0, r.stdout
        for f in rels:
            nl = data.index(b"\n", pos)
            head, pos = data[pos:nl].split(), nl + 1
            if head[-1] == b"missing":
                out[f] = None
                continue
            size = int(head[2])
            out[f] = data[pos:pos + size].decode("utf-8", errors="replace") if head[1] == b"blob" else None
            pos += size + 1
        return out

    @functools.cached_property
    def arch(self) -> Arch:
        text = self.texts([ARCH])[ARCH]
        return read_arch(text) if text is not None else Arch()


def default_state() -> Path:
    return Path(os.environ.get("HARNESS_HOME", str(Path(__file__).parent))) / "state"


# ------------------------------------------------------------------ scanning
class TreeCode:
    """Product/test classification of the tree at `cwd` (the file rule plus Lang.tree), built per gate call."""

    def __init__(self, lang: rl.Lang, cwd):
        self.lang, self.tree = lang, lang.tree(Path(cwd))

    def product(self, rel: str) -> bool:
        return self.lang.is_product(rel) and not self.tree.test(rel)


def scan(lang: rl.Lang, rel: str, text: str, arch: Arch | None, rules=None) -> list:
    """Every Hit of `rules` (default all) in product file `rel` with contents `text`."""
    if not lang.is_product(rel):
        return []
    want = set(rules or RULE)
    hits, lines = [], text.splitlines()
    for s, a, b in rl.statements(lang, text):
        for rid, pats in lang.code.items():
            n = max(len(re.findall(p, s)) for p in pats) if rid in want else 0
            if n and rid == "swallow" and any(MARKER.search(lines[k - 1]) for k in range(a, min(b, len(lines)) + 1)):
                continue
            hits += [Hit(rid, a, s[:160])] * n
    for c, line in lang.comments(text) if lang.comment else ():
        hits += [Hit(rid, line, c[:160]) for rid, pats in lang.comment.items() if rid in want
                 and any(re.search(p, c) for p in pats)]
    if "owner" in want and arch is not None and arch.stems:
        pv = rl.qs_import("plan_validate")
        mine = arch.module_of(rel)
        for name, line in lang.items(lang.tokens(text)):
            if f"{rel}::{name}" in arch.overlaps:
                continue
            for other, stems in arch.stems.items():
                if other != mine and stems and pv.stem_hits(stems, name):
                    hits.append(Hit("owner", line, f"{name} matches module {other}'s {', '.join(pv.stem_hits(stems, name))}"))
                    break
    return hits


def is_text(rel: str) -> bool:
    return bool(TEXT_FILE.match(rel)) and rel not in TEXT_GENERATED


def text_hits(rel: str, text: str, caps: TextCaps) -> list:
    """Every Hit of the text rule in text file `rel`."""
    if not is_text(rel):
        return []
    lines, hits = text.splitlines(), []
    words = [len(x.split()) for x in lines]
    if rel == "CLAUDE.md":
        if caps.always_read_words and sum(words) > caps.always_read_words:
            hits.append(Hit("text", 1, f"{sum(words)} words; every agent reads it whole (always_read_words "
                                       f"{caps.always_read_words})"))
        return hits
    for n, (line, w) in enumerate(zip(lines, words), 1):
        cap = caps.decision_line_words if rel == DECISIONS and DECISION_LINE.match(line) else caps.line_words
        if cap and w > cap:
            hits.append(Hit("text", n, f"line of {w} words (cap {cap}): {line[:100]}"))
    if rel == ARCH and caps.module_section_words:
        heads = [n for n, x in enumerate(lines) if re.match(r"^##?\s", x)] + [len(lines)]
        for a, b in zip(heads, heads[1:]):
            w = sum(words[a:b])
            if lines[a].startswith("## Module") and w > caps.module_section_words:
                hits.append(Hit("text", a + 1, f"{lines[a][3:].strip()} section of {w} words (cap "
                                               f"{caps.module_section_words}): work packets go in spec/packets/"))
    if rel == FUNCTIONS and caps.functions_prose_words:
        w = sum(x for line, x in zip(lines, words) if not line.lstrip().startswith("|"))
        if w > caps.functions_prose_words:
            hits.append(Hit("text", 1, f"{w} words of prose outside the tables (cap {caps.functions_prose_words}); "
                                       "a proven row's prose is dead text: its acceptance test is the contract"))
    return hits


def text_files(cwd) -> list:
    r = git(cwd, "ls-files", "-co", "--exclude-standard", "--", "spec", "CLAUDE.md")
    return sorted(f for f in r.stdout.split() if is_text(f) and (Path(cwd) / f).exists())


def read_arch(text: str) -> Arch:
    mods = rl.qs_import("plan_validate").parse_architecture(text)
    over = set(re.findall(r"`?([\w./-]+\.\w+::\w+)`?", "\n".join(
        line for line in text.splitlines() if re.match(r"^[\s>*-]*\**\s*Overlaps\s*\**\s*:", line, re.I))))
    return Arch({m: list(v["paths"] or []) for m, v in mods.items()}, {m: list(v["stems"]) for m, v in mods.items()},
                over)


def tree_arch(cwd) -> Arch:
    return Snapshot(cwd).arch


def product_files(code: TreeCode, cwd, roots=None) -> list:
    r = git(cwd, "ls-files", "--", *(roots or code.lang.roots))
    return sorted(f for f in r.stdout.split() if code.product(f) and (Path(cwd) / f).exists())


def file_hits(code: TreeCode, cwd, rel, arch, caps, rules=None) -> list:
    """Every Hit of `rules` (default all) in the product or text file `rel` of the tree at `cwd`."""
    if not is_text(rel) and not code.product(rel):
        return []
    text = (Path(cwd) / rel).read_text(encoding="utf-8", errors="replace")
    if is_text(rel):
        return text_hits(rel, text, caps) if "text" in (rules or RULE) else []
    return scan(code.lang, rel, text, arch, rules)


def counts(lang: rl.Lang, cwd, files=None, rules=None) -> dict:
    """{rule: {file: n}} over `files` (default every tracked product and text file) of the tree at `cwd`."""
    arch, caps, code = tree_arch(cwd), TextCaps.of(cwd), TreeCode(lang, cwd)  # no metadata: stricter, never looser
    out = {r: {} for r in (rules or RULE)}
    for rel in files if files is not None else product_files(code, cwd) + text_files(cwd):
        for h in file_hits(code, cwd, rel, arch, caps, rules):
            out[h.rule][rel] = out[h.rule].get(rel, 0) + 1
    return out


# ------------------------------------------------------------------ the debt file (shrink only)
def load_debt(st) -> dict:
    p = Path(st) / DEBT
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"rules": {}}


def save_debt(st, new: dict, old: dict) -> None:
    """Write `new`; refused when any count of a rule `old` already has grows (a rule `old` lacks may be added)."""
    grown = [f"{r}:{f} {n} > {old['rules'][r].get(f, 0)}" for r, fs in new["rules"].items() if r in old["rules"]
             for f, n in fs.items() if n > old["rules"][r].get(f, 0)]
    if "dups" in old:
        was, now = old["dups"], new.get("dups", {})
        grown += [f"dups:names:{n} {c} > {was.get('names', {}).get(n, 1)}" for n, c in now.get("names", {}).items()
                  if c > was.get("names", {}).get(n, 1)]
        grown += [f"dups:clones:{k} added" for k in now.get("clones", []) if k not in was.get("clones", [])]
    if grown:
        raise ValueError("rules debt may only shrink; refused: " + "; ".join(grown[:10]))
    p = Path(st) / DEBT
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(new, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def init_debt(lang: rl.Lang, st, cwd, note: str) -> dict:
    """Add the current counts of every rule the debt file lacks (existing rules untouched)."""
    old = load_debt(st)
    missing = [r for r in RULE if r not in old["rules"] and (r != "text" or TextCaps.of(cwd).on())]
    new = {**old, "rules": dict(old["rules"])}
    if missing:
        c = counts(lang, cwd, rules=missing)
        new["rules"].update({r: dict(sorted(c[r].items())) for r in missing})
    if "dups" not in old:
        new["dups"] = dup_debt(lang, cwd)
    if missing or "dups" not in old:
        new.setdefault("history", []).append(note)
        save_debt(st, new, old)
    return new


def tighten(lang: rl.Lang, st, cwd, files=None) -> list:
    """Lower each debt entry to its count in `cwd` (main after a land), over `files` (default all); returns the
    'rule:file old->new' changes."""
    old = load_debt(st)
    if not old["rules"] and "dups" not in old:
        return []
    text_on = TextCaps.of(cwd).on()
    if files is not None and text_on and "text" in old["rules"]:
        files = sorted(set(files) | set(text_files(cwd)))  # a few files: always recounted after a land
    c = counts(lang, cwd, files, rules=list(old["rules"])) if old["rules"] else {}
    changed, new = [], {**old, "rules": {}}
    for r, fs in old["rules"].items():
        if r == "text" and not text_on:  # caps off on this tree: its zero counts say nothing
            new["rules"][r] = dict(fs)
            continue
        new["rules"][r] = {}
        for f, n in fs.items():
            now = c[r].get(f, 0) if (files is None or f in files) else n
            if now < n:
                changed.append(f"{r}:{f} {n}->{now}")
            if min(n, now):
                new["rules"][r][f] = min(n, now)
    if "dups" in old and (files is None or any(lang.is_product(f) for f in files)):
        try:
            changed += tighten_dups(lang, cwd, old["dups"], new)
        except RuntimeError as e:
            raise ValueError(f"dups debt not tightened: {e}") from e
    if changed:
        save_debt(st, new, old)
    return changed


def tighten_dups(lang: rl.Lang, cwd, was: dict, new: dict) -> list:
    """Lower `was` names to their module counts in `cwd`, drop listed pairs no longer close; sets new["dups"]."""
    code, snap = TreeCode(lang, cwd), Snapshot(cwd)
    now = pub_names(lang, code, snap, set(was.get("names", {})))
    names = {n: min(c, len(now.get(n, ()))) for n, c in was.get("names", {}).items()}
    pairs = {k: [x.rsplit("::", 1) for x in k.split(" ~ ")] for k in was.get("clones", [])}
    bodies = bodies_of(lang, snap, {f for p in pairs.values() for f, _ in p})

    def near(p):
        b = [bodies[f].get(n) for f, n in p]
        return all(b) and cover(*b) >= CLONE_COVER
    new["dups"] = {**was, "names": {n: c for n, c in names.items() if c > 1},
                   "clones": [k for k, p in pairs.items() if near(p)]}
    return ([f"dups:names:{n} {c}->{names[n]}" for n, c in was.get("names", {}).items() if names[n] < c]
            + [f"dups:clones:{k} gone" for k in pairs if k not in new["dups"]["clones"]])


# ------------------------------------------------------------------ duplicate items
class Body(NamedTuple):
    toks: tuple
    bag: Counter


def conventions(now: Snapshot, was: Snapshot) -> frozenset:
    """overhaul.toml [rules] name_conventions of both trees: names every module declares by design."""
    sets = [set(toml_table(s.texts(["overhaul.toml"])["overhaul.toml"], "rules").get("name_conventions") or ())
            for s in (now, was)]
    return frozenset(sets[0] & sets[1])


def pub_names(lang: rl.Lang, code: TreeCode, snap: Snapshot, words=None) -> dict:
    """{name: {module}} of the exported items in the product files of `snap` (only `words`, when given)."""
    files = snap.grep(words, lang.roots) if words is not None else snap.files(lang.roots)
    out = {}
    for rel, text in snap.texts(f for f in files if code.product(f) and snap.arch.module_of(f)).items():
        for name in lang.exported(lang.tokens(text or "")):
            if words is None or name in words:
                out.setdefault(name, set()).add(snap.arch.module_of(rel))
    return out


def name_problems(lang: rl.Lang, code: TreeCode, now: Snapshot, was: Snapshot, mine, allowed: dict, conv) -> list:
    """An exported name a module adds while another declares it, past both debt `names` and base's count."""
    words = {n for t in now.texts(mine).values() for n in lang.exported(lang.tokens(t or ""))} - conv
    if not words:
        return []
    have, had, out = pub_names(lang, code, now, words), pub_names(lang, code, was, words), []
    for name in sorted(words):
        mods, before = have.get(name, set()), had.get(name, set())
        cap = min(allowed.get(name, 1), max(1, len(before)))
        if mods - before and len(mods) > cap:
            out.append(f"duplicate name: public `{name}` added in {', '.join(sorted(mods - before))}; declared in "
                       f"{len(mods)} modules ({', '.join(sorted(mods))}), allowed {cap} ({DEBT} dups.names); reuse "
                       "or extend the owner, or name what differs")
    return out


def fn_bodies(lang: rl.Lang, text: str | None) -> dict:
    """{function name: Body} of a file's product functions, strings blanked."""
    toks, out = lang.tokens(text or ""), {}
    for name, a, b in lang.functions(toks):
        body = tuple('""' if k == "str" else t for k, t, _ in toks[a:b + 1])
        out.setdefault(name, Body(body, Counter(body)))
    return out


def bodies_of(lang: rl.Lang, snap: Snapshot, files) -> dict:
    return {f: fn_bodies(lang, t) for f, t in snap.texts(files).items()}


def cover(a: Body, b: Body) -> float:
    """Share of the shorter body the longer holds in order (difflib matching blocks); 0 under CLONE_MIN_TOKENS."""
    short, long_ = sorted((a, b), key=lambda x: len(x.toks))
    if len(short.toks) < CLONE_MIN_TOKENS or sum((short.bag & long_.bag).values()) < CLONE_COVER * len(short.toks):
        return 0.0
    sm = difflib.SequenceMatcher(None, short.toks, long_.toks, autojunk=False)
    return sum(m.size for m in sm.get_matching_blocks()) / len(short.toks)


def close_pairs(bodies: dict, arch: Arch, mine=None) -> dict:
    """{"<file>::<fn> ~ <file>::<fn>": cover} of the pairs in one module at least CLONE_COVER close; with `mine`
    ({(file, fn)}) only the pairs holding one of them."""
    by = {}
    for rel, fns in sorted(bodies.items()):
        by.setdefault(arch.module_of(rel), []).extend((rel, n, b) for n, b in fns.items())
    out = {}
    for m, fns in by.items():
        for i, (rel, name, body) in enumerate(fns if m else ()):
            if mine is not None and (rel, name) not in mine:
                continue
            for orel, oname, other in fns if mine is not None else fns[i + 1:]:
                if (orel, oname) != (rel, name) and (share := cover(body, other)) >= CLONE_COVER:
                    out[" ~ ".join(sorted((f"{rel}::{name}", f"{orel}::{oname}")))] = share
    return out


def clone_problems(lang: rl.Lang, code: TreeCode, now: Snapshot, was: Snapshot, mine, listed) -> list:
    """A function the diff adds or changes now CLONE_COVER close to a sibling in its module, unless the pair was that
    close at base or debt `clones` lists it."""
    mods = {now.arch.module_of(f) for f in mine}
    files = [f for f in now.files(lang.roots) if code.product(f) and now.arch.module_of(f) in mods]
    have, had = bodies_of(lang, now, files), bodies_of(lang, was, files)
    grown = {(f, n) for f in mine for n, b in have.get(f, {}).items() if had.get(f, {}).get(n) != b}
    out = []
    for key, share in sorted(close_pairs(have, now.arch, grown).items() if grown else ()):
        old = [had.get(f, {}).get(n) for f, n in (x.rsplit("::", 1) for x in key.split(" ~ "))]
        if key not in listed and not (all(old) and cover(*old) >= CLONE_COVER):
            out.append(f"near copy: {key} ({share:.0%} of the shorter body); make one function (a flag or a shared "
                       "step) and call it from both")
    return out


def dup_problems(lang: rl.Lang, code: TreeCode, cwd, base, st, changed) -> list:
    """Duplicate names and near copies the diff against `base` adds; a failed git call is a problem line."""
    debt = load_debt(st)
    dups, hint = debt.get("dups", {}), "" if "dups" in debt else " (no dups debt: run rules_gate.py init on main)"
    try:
        now = Snapshot(cwd)
        mine = [f for f in changed if code.product(f) and now.arch.module_of(f)]
        if not mine:
            return []
        was = Snapshot(cwd, base)
        out = name_problems(lang, code, now, was, mine, dups.get("names", {}), conventions(now, was)) \
            + clone_problems(lang, code, now, was, mine, set(dups.get("clones", ())))
    except RuntimeError as e:
        return [f"rules gate: the duplicate check has no verdict: {e}"]
    return [x + hint for x in out]


def dup_debt(lang: rl.Lang, cwd) -> dict:
    """The doubles of the tree at `cwd`: sweep work the debt lists, never room for more."""
    code, snap = TreeCode(lang, cwd), Snapshot(cwd)
    conv = conventions(snap, snap)
    files = [f for f in snap.files(lang.roots) if code.product(f) and snap.arch.module_of(f)]
    return {"names": {n: len(m) for n, m in sorted(pub_names(lang, code, snap).items()) if len(m) > 1 and n not in conv},
            "clones": sorted(close_pairs(bodies_of(lang, snap, files), snap.arch))}


# ------------------------------------------------------------------ the gate
def changed_files(cwd, base) -> list | None:
    r = git(cwd, "diff", "--name-only", "--no-renames", "--diff-filter=AM", base)
    return None if r.returncode else r.stdout.split()


def added_lines(cwd, base, rel) -> set:
    r = git(cwd, "diff", "-U0", "--no-renames", base, "--", rel)
    out = set()
    for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", r.stdout, re.M):
        start, n = int(m.group(1)), int(m.group(2) or 1)
        out.update(range(start, start + n))
    return out


def flipped(code: TreeCode, cwd, base, scratch) -> tuple:
    """(product files of the packages test-only at `base` but not in `cwd`, problem lines)."""
    sha = git(cwd, "rev-parse", base).stdout.strip()
    with tree_at(cwd, sha, scratch) as wt:
        was = code.lang.tree(wt) if wt is not None else None
    if was is None or was.error:
        return [], [f"rules gate: no test-only packages of {base}: {was.error if was else 'cannot add its worktree'}"]
    gone = tuple(d for d in was.packages if d not in code.tree.packages)
    return (product_files(code, cwd, gone) if gone else []), []


def count_problems(lang: rl.Lang, code: TreeCode, cwd, base, st, changed, scratch) -> list:
    debt = load_debt(st)["rules"]
    arch, caps = tree_arch(cwd), TextCaps.of(cwd)
    files = product_files(code, cwd) if ARCH in changed else [f for f in changed if code.product(f)]
    out = [f"rules gate: {code.tree.error} (it classifies product and test packages)"] if code.tree.error else []
    flip = []
    if any(f.rsplit("/", 1)[-1] in lang.manifests for f in changed):  # flipped files: checked in full, as if changed
        flip, err = flipped(code, cwd, base, scratch)
        out += err
    files = sorted(set(files) | set(flip)) + [f for f in changed if is_text(f)]
    if "overhaul.toml" in changed:
        loose = caps.loosened(TextCaps.parse(git(cwd, "show", f"{base}:overhaul.toml").stdout))
        if loose:
            out.append(f"overhaul.toml: [text] {', '.join(loose)} raised or removed against {base}; a text cap may "
                       "only be tightened")
    hint = "" if debt else "; a new project records main's counts once: rules_gate.py init"
    for rel in files:
        by = {}
        for h in file_hits(code, cwd, rel, arch, caps, None if rel in changed or rel in flip else ["owner"]):
            by.setdefault(h.rule, []).append(h)
        for rid, hs in by.items():
            allowed = debt.get(rid, {}).get(rel, 0)
            if len(hs) <= allowed:
                continue
            new = added_lines(cwd, base, rel)
            show = [h for h in hs if h.line in new] or hs
            r = RULE[rid]
            out.append(f"{rel}: {len(hs)} x {r.title} (allowed {allowed}, {DEBT}{hint}); fix: {r.fix}"
                       + "".join(f"\n  {rel}:{h.line}: {h.text}" for h in show[:6]))
    return out


def plan_lines(repo) -> list | None:
    """plan_validate problem lines (no lint) of the tree at `repo`, line numbers removed; None on a crash."""
    try:
        r = subprocess.run([sys.executable, str(rl.qs() / "plan_validate.py"), "--repo", str(repo), "--no-lint"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    except subprocess.TimeoutExpired:
        return None
    lines = r.stdout.splitlines()
    if not lines or not lines[-1].startswith("PLAN "):
        return None
    return sorted(LINE_NO.sub(r"\1", x) for x in lines[:-1] if x.strip())


def tree_id(cwd) -> str | None:
    """Content id of the tree at `cwd`, uncommitted included, ignored out: the plan cache key. None when git fails."""
    import tempfile
    fd, idx = tempfile.mkstemp(prefix="pv-idx-")
    os.close(fd)
    os.remove(idx)
    try:
        env = {**os.environ, "GIT_INDEX_FILE": idx}
        run = lambda *a: subprocess.run(["git", *a], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8")
        if run("read-tree", "HEAD").returncode or run("add", "-A").returncode:
            return None
        r = run("write-tree")
        return r.stdout.strip() if r.returncode == 0 else None
    finally:
        if os.path.exists(idx):
            os.remove(idx)


_CACHE_LOCK = threading.Lock()


def cached_plan_lines(key: str | None, st, compute) -> list | None:
    """plan_validate lines for tree id `key` from state/PLAN_CACHE (retries and pre-push repeat trees), else `compute()`."""
    p = Path(st) / PLAN_CACHE

    def load():
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except ValueError:
            return {}
    if key and key in (c := load()):
        return c[key]
    lines = compute()
    if lines is not None and key:
        with _CACHE_LOCK:
            cache = {k: v for k, v in list(load().items())[-7:]}
            cache[key] = lines
            tmp = p.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(cache), encoding="utf-8")
            os.replace(tmp, p)
    return lines


@contextmanager
def tree_at(cwd, sha, scratch: Path):
    wt = Path(scratch) / f"pv-{sha[:12]}-{os.getpid()}-{threading.get_ident()}"
    if git(cwd, "worktree", "add", "-q", "--detach", str(wt), sha).returncode:
        yield None
        return
    try:
        yield wt
    finally:
        git(cwd, "worktree", "remove", "--force", str(wt))


def base_plan_lines(cwd, base, st, scratch: Path) -> list | None:
    sha = git(cwd, "rev-parse", base).stdout.strip()
    key = git(cwd, "rev-parse", f"{sha}^{{tree}}").stdout.strip() or None

    def compute():
        with tree_at(cwd, sha, scratch) as wt:
            return None if wt is None else plan_lines(wt)
    return cached_plan_lines(key, st, compute)


def new_plan_lines(now, was, landing=()) -> list:
    """Lines of `now` that `was` lacks, less each `landing` row's own spent stub (it lands proven)."""
    new = list((Counter(now) - Counter(was or [])).elements())
    return [x for x in new if not ((m := STUB_LINE.search(x)) and m.group(1) in set(landing))]


def plan_message(base, new) -> list:
    return [f"plan_validate problems {base} does not have (fix the spec edit, or leave other planned rows' stubs): "
            + "".join(f"\n  {x[:400]}" for x in new[:15])] if new else []


def plan_applies(cwd) -> bool:
    """Only an overhaul.toml repo holds an overhaul plan; another spec format would fail every row."""
    return (Path(cwd) / "overhaul.toml").is_file()


def plan_problems(cwd, base, st, scratch, changed, landing=(), always=False) -> list:
    """New plan_validate lines of `cwd` over `base`, on a spec/*.md change or `always`."""
    if not plan_applies(cwd):
        return []
    if not always and not any(re.fullmatch(r"spec/[^/]+\.md", f) for f in changed):
        return []
    with ThreadPoolExecutor(2) as pool:  # each run takes minutes: side by side the gate waits for one
        was_f = pool.submit(base_plan_lines, cwd, base, st, scratch)
        now, was = cached_plan_lines(tree_id(cwd), st, lambda: plan_lines(cwd)), was_f.result()
    if now is None:
        return ["plan_validate crashed on this tree (run: plan_validate.py --repo . --no-lint)"] if was is not None else []
    return plan_message(base, new_plan_lines(now, was, landing))


# ------------------------------------------------------------------ per-row plan check
# check_stubs is row-local; the other validate() checks stay with the gate (a test fails on an unlisted one).
ROW_PLAN_CHECKS = ("check_stubs",)
ROW_PLAN_LEFT = ("check_architecture", "check_stems", "check_new_owners", "check_roadmap", "check_rows",
                 "check_cargo_targets", "check_phase_size", "check_project_rules", "entry_problems", "check_needs",
                 "check_control_chars", "cross_problems", "check_property", "check_packets", "check_ui_verb",
                 "check_depth", "check_policies", "check_float_roundtrip", "check_traces", "check_ruff", "check_clippy",
                 "check_lint_green")


def planned_rows(text) -> list:
    pv = rl.qs_import("plan_validate")
    return [r for r in pv.parse_functions(text, [], FUNCTIONS) if pv.bare(r["status"]) != pv.DEFERRED]


def stub_lines(rows, repo) -> list:
    out = []
    rl.qs_import("plan_validate").check_stubs(rows, Path(repo), out, FUNCTIONS)
    return sorted(LINE_NO.sub(r"\1", x) for x in out)


def row_plan_problems(cwd, base, scratch, landing=()) -> list:
    """New check_stubs lines of `cwd` over commit `base` on the rows its diff reaches; RuntimeError = no verdict."""
    if not plan_applies(cwd):
        return []
    pv = rl.qs_import("plan_validate")
    r = git(cwd, "diff", "--name-only", "--no-renames", base)  # deletions too
    if r.returncode:
        raise RuntimeError(f"git diff against {base} failed in {cwd}: {r.stderr[-300:]}")
    changed = set(r.stdout.split())
    cells = lambda row: {k: v for k, v in row.items() if k != "line"}  # noqa: E731
    was_rows = {pv.bare(x["id"]): cells(x) for x in planned_rows(git(cwd, "show", f"{base}:{FUNCTIONS}").stdout)} \
        if FUNCTIONS in changed else {}
    p = Path(cwd) / FUNCTIONS
    rows = [x for x in planned_rows(p.read_text(encoding="utf-8", errors="replace") if p.is_file() else "")
            if changed & set(pv.row_files(x)) or (FUNCTIONS in changed and was_rows.get(pv.bare(x["id"])) != cells(x))]
    now = stub_lines(rows, cwd)
    if not new_plan_lines(now, [], landing):
        return []
    ids, sha = {pv.bare(x["id"]) for x in rows}, git(cwd, "rev-parse", base).stdout.strip()
    with tree_at(cwd, sha, scratch) as wt:
        if wt is None:
            raise RuntimeError(f"cannot add a worktree of {base} ({sha[:12]}) from {cwd}")
        f = wt / FUNCTIONS
        was = stub_lines([x for x in planned_rows(f.read_text(encoding="utf-8", errors="replace") if f.is_file() else "")
                          if pv.bare(x["id"]) in ids], wt)
    return plan_message(base[:12] if re.fullmatch(r"[0-9a-f]{40}", base) else base, new_plan_lines(now, was, landing))


def problems(cwd, base="main", st=None, scratch=None, lang=None, plan="spec", landing=()) -> list:
    """Every rule problem of the tree at `cwd` that `base` does not carry (empty = clean)."""
    if plan not in PLAN_MODES:
        raise ValueError(f"plan={plan!r}; want one of {PLAN_MODES}")
    st = Path(st or default_state())
    lg = rl.LANGS[lang or rl.detect(cwd)]
    changed = changed_files(cwd, base)
    if changed is None:
        return [f"rules gate: git diff against {base} failed in {cwd}"]
    code = TreeCode(lg, cwd)
    return count_problems(lg, code, cwd, base, st, changed, Path(scratch or st)) \
        + dup_problems(lg, code, cwd, base, st, changed) + (
        plan_problems(cwd, base, st, Path(scratch or st), changed, landing, always=plan == "always")
        if plan != "off" else [])


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("check", "count", "init", "tighten"))
    ap.add_argument("--dir", default=os.environ.get("HARNESS_REPO", "."))
    ap.add_argument("--base", default="main")
    ap.add_argument("--files", action="store_true")
    ap.add_argument("--state", default="", help="folder of rules-debt.json and the plan cache (default "
                                                "<HARNESS_HOME>/state)")
    ap.add_argument("--lang", choices=sorted(rl.LANGS), default=None)
    a = ap.parse_args(argv)
    d, st = Path(a.dir), Path(a.state) if a.state else default_state()
    lang = rl.LANGS[a.lang or rl.detect(d)]
    if a.cmd == "check":
        p = problems(d, a.base, st, st, lang.name)
        print("\n".join(p) if p else "RULES OK")
        return 1 if p else 0
    if a.cmd == "count":
        for r, fs in counts(lang, d).items():
            print(f"{r}: {sum(fs.values())} in {len(fs)} files")
            if a.files:
                print("".join(f"  {f} {n}\n" for f, n in sorted(fs.items(), key=lambda x: -x[1])), end="")
        return 0
    try:
        if a.cmd == "init":
            head = git(d, "rev-parse", "HEAD").stdout.strip()[:12]
            new = init_debt(lang, st, d, f"init at {head} {datetime.now(timezone.utc):%Y-%m-%dT%H:%MZ}")
            print(json.dumps({r: sum(fs.values()) for r, fs in new["rules"].items()}))
        else:
            print("\n".join(tighten(lang, st, d)) or "no change")
    except (ValueError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
