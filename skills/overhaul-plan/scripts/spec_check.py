"""Step 8 consistency check for an overhaul-plan spec.

Usage: py -3 spec_check.py <NEW repo dir> <FLEET dir> [--cap 5200]
Prints one line per miss, then a one-line summary. Exit 1 if anything is missed.
"""
import json
import re
import sys
from pathlib import Path

SPEC_FILES = ["CLAUDE.md", "spec/STANDARDS.md", "spec/ARCHITECTURE.md", "spec/PLAN.md",
              "spec/MASTER-AGENT.md", "spec/MACHINES.md"]
TRIPWIRES = [f"H{i:02d}" for i in range(1, 25)]
GATES = ["Phase 0 gate", "stall gate"]
DEADLINE = re.compile(r"by hour|deadline|within \d+ (min|minutes|hours?)\b|minute \d+|hour \d+", re.I)
BRIEF_FIELDS = ["FUNCTIONS id", "Acceptance", "allowed files", "INDEX", "hook", "call cap", "hand-back",
                "hand-off"]
TEMPLATE_KINDS = ["m0-pr", "risky-pr", "post-merge-sample", "design-note", "management", "stub-check",
                  "protected-batch", "replan", "final"]
CLAUDE_MD_MAX_WORDS = 400
BANNED = re.compile(r"\b(weeks?|sprints?|person-days?|\d+(\.\d+)?\s*(-\s*\d+\s*)?days?)\b", re.I)
TABLE_MAX_ROWS = 40

misses = []


def miss(msg):
    misses.append(msg)
    print("MISS", msg)


def table_rows(text):
    """Return data rows of every markdown table as lists of cells."""
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and not re.match(r"^\|[\s:|-]+\|?$", s):
            rows.append([c.strip() for c in s.strip("|").split("|")])
    return rows


def prose_words(text):
    words, fence = 0, False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            fence = not fence
            continue
        if fence or s.startswith("|"):
            continue
        words += len(re.findall(r"[A-Za-z0-9][\w'./-]*", s))
    return words


def tables_in(text):
    out, cur = [], []
    for line in text.splitlines():
        if line.strip().startswith("|"):
            cur.append(line)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    cap = 5200
    if "--cap" in sys.argv:
        cap = int(sys.argv[sys.argv.index("--cap") + 1])
        args = [a for a in args if a != str(cap)]
    if len(args) < 2:
        print(__doc__)
        sys.exit(2)
    repo, fleet = Path(args[0]), Path(args[1])

    texts = {}
    for f in SPEC_FILES:
        p = repo / f
        if not p.exists():
            miss(f"spec file missing: {f}")
            continue
        texts[f] = p.read_text(encoding="utf-8", errors="replace")

    # Word cap, banned time words, table size.
    total = sum(prose_words(t) for t in texts.values())
    if total > cap:
        miss(f"prose words {total} > cap {cap}")
    cw = prose_words(texts.get("CLAUDE.md", ""))
    if cw > CLAUDE_MD_MAX_WORDS:
        miss(f"CLAUDE.md has {cw} words > {CLAUDE_MD_MAX_WORDS}; every agent loads it")
    for f, t in texts.items():
        for n, line in enumerate(t.splitlines(), 1):
            m = BANNED.search(line)
            if m:
                miss(f"{f}:{n} human-time word '{m.group(0)}'")
        for tab in tables_in(t):
            data = len(tab) - 2
            is_tripwire = any(re.search(r"\|\s*H\d\d\s*\|", r) for r in tab)
            if data > TABLE_MAX_ROWS and not is_tripwire:
                miss(f"{f}: a table has {data} rows > {TABLE_MAX_ROWS}")

    std = texts.get("spec/STANDARDS.md", "")
    rule_ids = set(re.findall(r"^\s*(?:[-*]\s*)?\**(S\d+)\b", std, re.M))
    if not rule_ids:
        miss("STANDARDS.md has no rule ids of the form S<n> at line start")

    # Every rule ends with its lint or Review (a rule may run onto a second line).
    blocks, cur = [], None
    for line in std.splitlines():
        m = re.match(r"^\s*(?:[-*]\s*)?\**(S\d+)\b", line)
        if m:
            cur = [m.group(1), line]
            blocks.append(cur)
        elif cur is not None and line.strip() and not line.lstrip().startswith(("#", "|")):
            cur[1] += " " + line
        else:
            cur = None
    for rid, body in blocks:
        if "Lint:" not in body and "Review" not in body:
            miss(f"STANDARDS {rid} ends with neither 'Lint: <name>' nor 'Review'")

    # PLAN's M0 list: paths it names are built in M0, so they may not exist yet.
    plan = texts.get("spec/PLAN.md", "")
    m0_lines, on = [], False
    for line in plan.splitlines():
        if not on and re.search(r"\bM0\b", line):
            on = True
        elif on and re.match(r"^\s*(?:#+|\*\*|[-*]\s+\*\*)\s*Phase 0", line):
            break
        if on:
            m0_lines.append(line)
    m0_text = "\n".join(m0_lines)

    # Every cited rule id exists.
    for f, t in texts.items():
        for n, line in enumerate(t.splitlines(), 1):
            for rid in re.findall(r"\bS\d+\b", line):
                if rule_ids and rid not in rule_ids:
                    miss(f"{f}:{n} cites rule {rid} which STANDARDS does not define")

    # Every cited path exists (backticked, contains a slash or a known extension).
    for f, t in texts.items():
        for n, line in enumerate(t.splitlines(), 1):
            for ref in re.findall(r"`([^`\s]+)`", line):
                if "<" in ref or "*" in ref or ref.startswith(("http", "-")):
                    continue
                if not re.search(r"/|\.(md|toml|json|py|tsv|sh|ps1)$", ref):
                    continue
                if ref.startswith("FLEET/"):
                    p = fleet / ref[len("FLEET/"):]
                else:
                    p = repo / ref
                generated = re.search(r"(INDEX\.md|HEALTH\.tsv|status\.json|phases\.json|functions\.json|DISPATCH\.md|RULINGS\.md|COORD-STATE\.md|PAUSE\.md|MASTER-CARD\.md)$", ref)
                built_in_m0 = bool(m0_text) and (ref in m0_text or Path(ref).name in m0_text)
                if not p.exists() and not generated and not built_in_m0:
                    miss(f"{f}:{n} cites path {ref} which does not exist")

    # FUNCTIONS.md: rows, Acceptance, totals.
    fpath = repo / "spec/FUNCTIONS.md"
    frows = []
    if fpath.exists():
        frows = [r for r in table_rows(fpath.read_text(encoding="utf-8", errors="replace"))
                 if r and r[0].lower() != "id"]
        for r in frows:
            if len(r) < 5 or not r[4]:
                miss(f"FUNCTIONS {r[0] if r else '?'} has no Acceptance")
    else:
        miss("spec/FUNCTIONS.md missing")
    nfun = len(frows)
    if nfun and str(nfun) not in plan and f"{nfun:,}" not in plan:
        miss(f"PLAN.md does not state the function total {nfun}")
    fj = fleet / "dash/functions.json"
    if fj.exists():
        try:
            n = len(json.loads(fj.read_text(encoding="utf-8")))
            if n != nfun:
                miss(f"dash/functions.json has {n} rows, FUNCTIONS.md has {nfun}")
        except Exception as e:  # noqa: BLE001
            miss(f"dash/functions.json unreadable: {e}")
    else:
        miss("FLEET/dash/functions.json missing")

    # PATTERNS.md: every pattern maps to an existing rule.
    ppath = repo / "spec/PATTERNS.md"
    if ppath.exists():
        for r in table_rows(ppath.read_text(encoding="utf-8", errors="replace")):
            if not r or r[0].lower() == "id":
                continue
            ids = re.findall(r"\bS\d+\b", " ".join(r[1:]))
            if not ids:
                miss(f"PATTERNS {r[0]} maps to no rule")
            for rid in ids:
                if rule_ids and rid not in rule_ids:
                    miss(f"PATTERNS {r[0]} maps to missing rule {rid}")
            if r[0] not in std:
                miss(f"PATTERNS {r[0]} id is not cited in STANDARDS")
    else:
        miss("spec/PATTERNS.md missing")

    # Lints named in STANDARDS are all in the M0 list.
    lints = set(re.findall(r"\bLint:\s*`?([A-Za-z0-9][\w-]*)`?", std))
    if not lints:
        miss("STANDARDS names no lint in the form 'Lint: <name>'")
    m0 = plan + texts.get("spec/MASTER-AGENT.md", "")
    for lint in sorted(lints):
        if lint not in m0:
            miss(f"lint '{lint}' named in STANDARDS is not in the M0 list")
    if not re.search(r"duplicate[- ]capabilit", std, re.I):
        miss("STANDARDS names no duplicate-capability lint")

    # Ownership guards and plain words.
    arch = texts.get("spec/ARCHITECTURE.md", "")
    for key in ("forbidden_types", "callers"):
        if key not in arch:
            miss(f"ARCHITECTURE owners.toml schema lacks '{key}'")
    guards = {"catch-all module-name ban (utils, helpers, common)": r"\butils\b.*\bhelpers\b|\bhelpers\b.*\butils\b",
              "import allowlist": r"allow-?list",
              "module header naming the owner": r"header",
              "pasted search before new code": r"search",
              "slop lint": r"Lint:\s*`?slop",
              "hook-first rule (stop and hook before writing the code)": r"hook-first",
              "look-alike lint over private helpers": r"look-?alike[^\n]*private|private[^\n]*look-?alike",
              "LF line endings (.gitattributes)": r"gitattributes"}
    mach = texts.get("spec/MACHINES.md", "")
    if not re.search(r"\bfind\b", mach) or not re.search(r"in-?flight", mach, re.I):
        miss("MACHINES lacks the find command and its in-flight index (exists, planned, in flight)")
    if not re.search(r"\bsupersede\b", mach):
        miss("MACHINES lacks the supersede command (clears a replaced packet's rows, worktree and branch)")
    if not re.search(r"\btop \d+\b", mach):
        miss("MACHINES lacks find's ranking and cap (top N hits)")
    for name, rx in guards.items():
        if not re.search(rx, std, re.I):
            miss(f"STANDARDS lacks the {name}")

    # Tripwires.
    ma = texts.get("spec/MASTER-AGENT.md", "")
    trows = {r[0]: r for r in table_rows(ma) if r and re.fullmatch(r"H\d\d", r[0])}
    for h in TRIPWIRES:
        r = trows.get(h)
        if not r:
            miss(f"tripwire {h} not in the MASTER-AGENT table")
            continue
        body = " ".join(r[1:])
        if not re.search(r"\d", body):
            miss(f"tripwire {h} has no number")
        if len(r) < 4 or not all(c for c in r[1:4]):
            miss(f"tripwire {h} needs threshold, source and action columns")

    for name, rx in {"ruling holds on in-flight packets": r"\bhold", "superseded-packet path": r"supersed",
                     "measured counts in COORD-STATE": r"measured", "clean end state (no stale rows, branches, worktrees)": r"stale|end state"}.items():
        if not re.search(rx, ma, re.I):
            miss(f"MASTER-AGENT lacks the {name}")

    # Phase 0, gates, no time words in brief text, brief fields.
    if not re.search(r"Phase 0", plan):
        miss("PLAN.md has no Phase 0")
    for g in GATES:
        if g.lower() not in ma.lower():
            miss(f"MASTER-AGENT does not name the {g}")
    for n, line in enumerate(ma.splitlines(), 1):
        if "brief" in line.lower() and (m := DEADLINE.search(line)):
            miss(f"MASTER-AGENT:{n} brief text has a time word '{m.group(0)}'")
    for fld in BRIEF_FIELDS:
        if fld.lower() not in ma.lower():
            miss(f"brief template lacks '{fld}'")

    # Auditor templates (Step 4b): one per kind, each with a <TARGET> slot.
    tdir = fleet / "templates"
    names = sorted(p.name for p in tdir.iterdir() if p.is_file()) if tdir.is_dir() else []
    for kind in TEMPLATE_KINDS:
        hit = [n for n in names if kind in n]
        if not hit:
            miss(f"FLEET/templates has no '{kind}' auditor template")
        elif "<TARGET>" not in (tdir / hit[0]).read_text(encoding="utf-8", errors="replace"):
            miss(f"FLEET/templates/{hit[0]} has no <TARGET> slot")
    for n in names:
        for i, line in enumerate((tdir / n).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if m := DEADLINE.search(line):
                miss(f"FLEET/templates/{n}:{i} has a time word '{m.group(0)}'")

    print(f"SUMMARY misses={len(misses)} prose_words={total}/{cap} functions={nfun} rules={len(rule_ids)}")
    sys.exit(1 if misses else 0)


if __name__ == "__main__":
    main()
