"""Ratcheted rule checks for the land train: rules no lint config holds, counted per file. No build, no LLM.

Run by harness.quality_problems (train, pre-push) and by cloud sessions (`check --state <qa dir>`). Per product file
(rules_langs.LANGS; test code out): swallow (dropped error or silent default; a `// default: <why>` or
`// best-effort: <why>` comment exempts), expect, panic, allow (a silenced lint), owner (a public type named like
another module's ARCHITECTURE `Forbidden types:` stem; `Overlaps: <path>::<Name>` exempts one), text (spec and
CLAUDE.md over the overhaul.toml [text] caps, which may only be tightened). A file fails above its count in
<state>/rules-debt.json (absent = 0; a new project runs `init` on main once); `tighten` after each land, never growth.
Debt is per file and never moves: code moved into a new file must be clean (fix its findings in the same change).
A tree that changes spec/*.md must add no plan_validate problem line.

    py -3 rules_gate.py check   [--dir D] [--base main] [--state S] [--lang L]   exit 1 on any problem
    py -3 rules_gate.py count   [--dir D] [--files] [--lang L]
    py -3 rules_gate.py init    [--dir D] [--state S] [--lang L]   add the debt of every check the file lacks
    py -3 rules_gate.py tighten [--dir D] [--state S] [--lang L]   lower each debt entry to D's counts (D = main)
"""
from __future__ import annotations

import json, os, re, subprocess, sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

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
        if tomllib is None or not toml_text.strip():
            return cls()
        try:
            t = tomllib.loads(toml_text).get("text") or {}
        except tomllib.TOMLDecodeError:
            return cls()  # plan_validate reports a broken overhaul.toml; the caps are then off
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


def git(cwd, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def default_state() -> Path:
    return Path(os.environ.get("HARNESS_HOME", str(Path(__file__).parent))) / "state"


# ------------------------------------------------------------------ scanning
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
    p = Path(cwd) / ARCH
    return read_arch(p.read_text(encoding="utf-8", errors="replace")) if p.exists() else Arch()


def product_files(lang: rl.Lang, cwd) -> list:
    r = git(cwd, "ls-files", "--", *lang.roots)
    return sorted(f for f in r.stdout.split() if lang.is_product(f) and (Path(cwd) / f).exists())


def file_hits(lang: rl.Lang, cwd, rel, arch, caps, rules=None) -> list:
    """Every Hit of `rules` (default all) in the product or text file `rel` of the tree at `cwd`."""
    text = (Path(cwd) / rel).read_text(encoding="utf-8", errors="replace")
    if is_text(rel):
        return text_hits(rel, text, caps) if "text" in (rules or RULE) else []
    return scan(lang, rel, text, arch, rules)


def counts(lang: rl.Lang, cwd, files=None, rules=None) -> dict:
    """{rule: {file: n}} over `files` (default every tracked product and text file) of the tree at `cwd`."""
    arch, caps = tree_arch(cwd), TextCaps.of(cwd)
    out = {r: {} for r in (rules or RULE)}
    for rel in files if files is not None else product_files(lang, cwd) + text_files(cwd):
        for h in file_hits(lang, cwd, rel, arch, caps, rules):
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
        new.setdefault("history", []).append(note)
        save_debt(st, new, old)
    return new


def tighten(lang: rl.Lang, st, cwd, files=None) -> list:
    """Lower each debt entry to its count in `cwd` (main after a land), over `files` (default all); returns the
    'rule:file old->new' changes."""
    old = load_debt(st)
    if not old["rules"]:
        return []
    text_on = TextCaps.of(cwd).on()
    if files is not None and text_on and "text" in old["rules"]:
        files = sorted(set(files) | set(text_files(cwd)))  # a few files: always recounted after a land
    c = counts(lang, cwd, files, rules=list(old["rules"]))
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
    if changed:
        save_debt(st, new, old)
    return changed


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


def count_problems(lang: rl.Lang, cwd, base, st, changed) -> list:
    debt = load_debt(st)["rules"]
    arch, caps = tree_arch(cwd), TextCaps.of(cwd)
    files = product_files(lang, cwd) if ARCH in changed else [f for f in changed if lang.is_product(f)]
    files += [f for f in changed if is_text(f)]
    out = []
    if "overhaul.toml" in changed:
        loose = caps.loosened(TextCaps.parse(git(cwd, "show", f"{base}:overhaul.toml").stdout))
        if loose:
            out.append(f"overhaul.toml: [text] {', '.join(loose)} raised or removed against {base}; a text cap may "
                       "only be tightened")
    hint = "" if debt else "; a new project records main's counts once: rules_gate.py init"
    for rel in files:
        by = {}
        for h in file_hits(lang, cwd, rel, arch, caps, None if rel in changed else ["owner"]):
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


def base_plan_lines(cwd, base, st, scratch) -> list | None:
    """plan_lines of `base`, cached per commit in <state>/plan-validate-base.json (last 8)."""
    sha = git(cwd, "rev-parse", base).stdout.strip()
    p = Path(st) / PLAN_CACHE
    cache = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    if sha in cache:
        return cache[sha]
    wt = Path(scratch) / f"pv-{sha[:12]}-{os.getpid()}"
    if git(cwd, "worktree", "add", "-q", "--detach", str(wt), sha).returncode:
        return None
    try:
        lines = plan_lines(wt)
    finally:
        git(cwd, "worktree", "remove", "--force", str(wt))
    if lines is not None:
        cache = dict(list(cache.items())[-7:])
        cache[sha] = lines
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, p)
    return lines


def plan_problems(cwd, base, st, scratch, changed) -> list:
    if not any(re.fullmatch(r"spec/[^/]+\.md", f) for f in changed):
        return []
    with ThreadPoolExecutor(2) as pool:  # each run takes minutes: side by side the gate waits for one
        was_f = pool.submit(base_plan_lines, cwd, base, st, scratch)
        now, was = plan_lines(cwd), was_f.result()
    if now is None:
        return ["plan_validate crashed on this tree (run: plan_validate.py --repo . --no-lint)"] if was is not None else []
    new = list((Counter(now) - Counter(was or [])).elements())
    return [f"plan_validate problems {base} does not have (fix the spec edit): "
            + "".join(f"\n  {x[:400]}" for x in new[:15])] if new else []


def problems(cwd, base="main", st=None, scratch=None, lang=None, plan=True) -> list:
    """Every rule problem of the tree at `cwd` that `base` does not carry (empty = clean)."""
    st = Path(st or default_state())
    lg = rl.LANGS[lang or rl.detect(cwd)]
    changed = changed_files(cwd, base)
    if changed is None:
        return [f"rules gate: git diff against {base} failed in {cwd}"]
    return count_problems(lg, cwd, base, st, changed) + (plan_problems(cwd, base, st, Path(scratch or st), changed)
                                                         if plan else [])


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
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
