"""Compare a rebuilt project (NEW) with the original (OLD) for the overhaul-difference report.

Usage:
  py -3 -I compare.py --old <OLD> --new <NEW> [--fleet <FLEET>] --out <DIR>
                      [--old-measure <json>] [--new-measure <json>] [--old-bugs <ledger>]

Reads OLD's OVERHAUL-CHECK.md and OVERHAUL-CHECK-data/measure.json, NEW's spec/ and FLEET/dash/,
runs overhaul-check's measure.py where a measurement is missing, and writes one JSON dataset per
report section into DIR: verdict, scorecard, metrics, parity, functions, patterns, bugs, cost,
survived. Every value is measured or read; anything missing is null with a reason in its note.
Standard library only. Prints a 6-line summary.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

MEASURE = Path(__file__).resolve().parents[2] / "overhaul-check" / "scripts" / "measure.py"
SKIP = {".git", "node_modules", "target", "dist", "build", "out", ".next", ".venv", "venv",
        "__pycache__", ".cache", "coverage", "vendor"}
DIMS = [("A", "Architecture"), ("G", "Organization"), ("Q", "Size and complexity"),
        ("U", "Duplication"), ("H", "Hygiene"), ("T", "Tests and gate"), ("D", "Dependencies"),
        ("S", "Security"), ("B", "Bug history"), ("R", "Build and delivery"),
        ("F", "Roadmap fit"), ("W", "Wordiness"), ("O", "Owner rules")]
NOT_BUILT = "not built: NEW has no code yet"


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(p):
    try:
        return Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def load_json(p):
    t = read(p)
    if t is None:
        return None
    try:
        return json.loads(t)
    except ValueError:
        return None


def run_measure(repo, out):
    if not MEASURE.exists():
        return None, f"measure.py not found at {MEASURE}"
    r = subprocess.run([sys.executable, "-I", str(MEASURE), str(repo), "--out", str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not Path(out).exists():
        return None, "measure.py failed: " + (r.stderr.strip().splitlines() or ["no output"])[-1]
    return load_json(out), f"measure.py run on {Path(repo).name}"


def get(d, path, default=None):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def md_table(text, first_header):
    """Rows of the first markdown table whose first header cell is first_header."""
    rows, on = [], False
    for line in (text or "").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            if on and rows:
                break
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not on:
            on = cells and cells[0].lower() == first_header.lower()
            continue
        if all(re.fullmatch(r":?-+:?", c) for c in cells if c):
            continue
        rows.append(cells)
    return rows


def section(text, heading_re):
    """Body of the first markdown section whose heading matches heading_re."""
    out, on = [], False
    for line in (text or "").splitlines():
        if line.startswith("#"):
            if on:
                break
            on = bool(re.search(heading_re, line, re.I))
            continue
        if on:
            out.append(line)
    return "\n".join(out).strip()


def files_of(root, skip_top=()):
    found = []
    root = Path(root)
    for base, dirs, names in os.walk(root):
        rel = Path(base).relative_to(root)
        dirs[:] = [d for d in dirs if d not in SKIP and not (rel == Path(".") and d in skip_top)]
        found.extend(str((rel / n).as_posix()) for n in names)
    return found


# ---------- scoring (overhaul-check Step 5, the parts a script can measure) ----------

def band(v, t1, t2, t3):
    return 3 if v >= t3 else 2 if v >= t2 else 1 if v >= t1 else 0


def score(m, has_ci, patterns_count=None):
    """{dim: (score or None, numbers, note)} from a measure.json."""
    s = {}
    cyc = get(m, "module_graph.cycle_count", 0)
    big_cycle = max((len(c) for c in get(m, "module_graph.cycles", []) or []), default=0)
    god = max((r[2] for r in m.get("modules", []) if len(r) > 2), default=0)
    a = 3 if cyc > 5 or big_cycle >= 6 else 2 if cyc >= 3 or god > 4000 else 1 if cyc >= 1 else 0
    s["A"] = (a, f"{cyc} module cycles, largest module {god:,} lines",
              "duplicate capabilities need an auditor's confirmation and are not scored here")
    nfiles = get(m, "loc.files", 0) or 0
    f500 = 100 * get(m, "files_over.500", 0) / nfiles if nfiles else 0
    f75 = 100 * (get(m, "functions.share_over_75", 0) or 0)
    s["Q"] = (max(band(f500, 5, 10, 20), band(f75, 2, 5, 10)),
              f"{f500:.1f}% of files over 500 lines, {f75:.1f}% of functions over 75", "")
    dup = get(m, "duplication.pct_lines", 0) or 0
    s["U"] = (band(dup, 3, 6, 12), f"{dup}% duplicated lines", "")
    sup = m.get("suppressions_per_kloc", 0) or 0
    s["H"] = (band(sup, 2, 5, 15), f"{sup} suppressions per kLOC",
              "lint and type errors need the project's own tools and are not counted")
    tr = get(m, "loc.test_ratio", 0) or 0
    t = 3 if not get(m, "loc.test", 0) else 2 if tr < 0.2 else 1 if tr < 0.5 or not has_ci else 0
    s["T"] = (t, f"test ratio {tr}, CI gate {'found' if has_ci else 'not found'}",
              "red and flaky tests need a test run and are not counted")
    fs = 100 * (get(m, "git.fix_share", 0) or 0)
    b = band(fs, 25, 40, 60)
    note = "recurring patterns need the bug history"
    if patterns_count is not None:
        b = max(b, 3 if patterns_count > 5 else 2 if patterns_count >= 3 else 1 if patterns_count else 0)
        note = f"{patterns_count} recurring patterns from PATTERNS.md"
    s["B"] = (b, f"git fix share {fs:.0f}%" + (f", {patterns_count} recurring patterns" if patterns_count else ""), note)
    fk = get(m, "slop.filler_per_kloc", 0) or 0
    cr = get(m, "slop.comment_ratio", 0) or 0
    dw = get(m, "slop.doc_words_per_kloc", 0) or 0
    w = 3 if fk >= 10 or dw > 5000 else 2 if fk >= 5 or cr >= 0.5 or dw > 2000 else 1 if fk >= 2 or cr >= 0.35 else 0
    s["W"] = (w, f"filler {fk}/kLOC, comment ratio {cr}, docs {dw:,} words/kLOC", "")
    nocache = len(get(m, "llm.files_without_cache_control", []) or [])
    rawhex, tokens = get(m, "style.raw_hex_colors", 0) or 0, get(m, "style.css_var_tokens", 0) or 0
    o = 2 if nocache or (rawhex and tokens) else 0
    s["O"] = (o, f"{nocache} LLM call files without caching, {rawhex} raw hex vs {tokens} tokens",
              "modesty, consent and secrets need a review")
    for d in "GDSRF":
        s[d] = (None, "", "needs an auditor or the project's own tools; not scored by script")
    hits = m.get("secret_hits", 0)
    if hits:
        s["S"] = (None, f"{hits} secret leads (unconfirmed)", "leads need a review before scoring")
    return s


def old_scores_from_report(text):
    out = {}
    names = {n.lower(): d for d, n in DIMS}
    for line in (text or "").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
        dim = None
        for i, c in enumerate(cells[:2]):
            key = c.split("(")[0].strip()
            if key in dict(DIMS):
                dim = key
            elif key.lower() in names:
                dim = names[key.lower()]
            if dim:
                rest = cells[i + 1:]
                break
        if not dim or dim in out:
            continue
        for j, c in enumerate(rest):
            if re.fullmatch(r"[0-3]", c):
                out[dim] = (int(c), rest[j + 1] if j + 1 < len(rest) else "")
                break
    return out


# ---------- metrics ----------

def metric_rows(om, nm, built):
    def pct(a, b):
        return round(100 * a / b, 1) if b else 0

    def vals(m):
        nf = get(m, "loc.files", 0) or 0
        hexr, tok = get(m, "style.raw_hex_colors", 0) or 0, get(m, "style.css_var_tokens", 0) or 0
        return {
            "loc_code": get(m, "loc.code"), "loc_test": get(m, "loc.test"), "files": nf,
            "files_500_pct": pct(get(m, "files_over.500", 0), nf),
            "files_1000": get(m, "files_over.1000"), "files_2000": get(m, "files_over.2000"),
            "fn_75_pct": round(100 * (get(m, "functions.share_over_75", 0) or 0), 1),
            "fn_150": get(m, "functions.over_150"),
            "dup_pct": get(m, "duplication.pct_lines"),
            "cycles": get(m, "module_graph.cycle_count"),
            "largest_module": max((r[2] for r in m.get("modules", []) if len(r) > 2), default=0),
            "dup_cap_names": len(m.get("duplicate_capability_names", {}) or {}),
            "suppress_kloc": m.get("suppressions_per_kloc"), "markers_kloc": m.get("markers_per_kloc"),
            "test_ratio": get(m, "loc.test_ratio"),
            "filler_kloc": get(m, "slop.filler_per_kloc"), "comment_ratio": get(m, "slop.comment_ratio"),
            "docs_kloc": get(m, "slop.doc_words_per_kloc"), "emoji": get(m, "slop.emoji"),
            "llm_nocache": len(get(m, "llm.files_without_cache_control", []) or []),
            "raw_hex": hexr, "raw_hex_pct": pct(hexr, hexr + tok),
            "secret_hits": m.get("secret_hits"),
            "fix_share": round(100 * (get(m, "git.fix_share", 0) or 0), 1),
        }

    spec = [  # key, label, unit, better (lower/higher/neutral), ranked as an improvement
        ("loc_code", "Lines of code", "lines", "neutral", False),
        ("loc_test", "Lines of tests", "lines", "neutral", False),
        ("files", "Source files", "files", "neutral", False),
        ("files_500_pct", "Files over 500 lines", "% of files", "lower", True),
        ("files_1000", "Files over 1,000 lines", "files", "lower", False),
        ("files_2000", "Files over 2,000 lines", "files", "lower", False),
        ("fn_75_pct", "Functions over 75 lines", "% of functions", "lower", True),
        ("fn_150", "Functions over 150 lines", "functions", "lower", False),
        ("dup_pct", "Duplicated lines", "%", "lower", True),
        ("cycles", "Module cycles", "cycles", "lower", True),
        ("largest_module", "Largest module", "lines", "lower", True),
        ("dup_cap_names", "Duplicate-capability name leads", "names", "lower", True),
        ("suppress_kloc", "Lint suppressions", "per kLOC", "lower", True),
        ("markers_kloc", "TODO and FIXME markers", "per kLOC", "lower", True),
        ("test_ratio", "Test lines per code line", "ratio", "higher", True),
        ("filler_kloc", "Filler phrases", "per kLOC", "lower", True),
        ("comment_ratio", "Comment lines per code line", "ratio", "lower", True),
        ("docs_kloc", "Docs words", "per kLOC", "lower", True),
        ("emoji", "Emoji", "count", "lower", False),
        ("llm_nocache", "LLM call files without caching", "files", "lower", True),
        ("raw_hex", "Raw hex colours", "count", "lower", False),
        ("raw_hex_pct", "Raw hex share of colour uses", "%", "lower", True),
        ("secret_hits", "Secret leads", "hits", "lower", True),
        ("fix_share", "Fix commits share (git)", "%", "lower", True),
    ]
    ov = vals(om) if om else {}
    nv = vals(nm) if nm and built else {}
    rows = []
    for key, label, unit, better, ranked in spec:
        o, n = ov.get(key), nv.get(key)
        note = ""
        if not om:
            note = "OLD not measured"
        elif not built:
            note = NOT_BUILT
        rows.append({"metric": label, "unit": unit, "old": o, "new": n, "better": better,
                     "change": change(o, n, better), "rel": rel(o, n, better), "ranked": ranked,
                     "note": note})
    return rows


def change(o, n, better):
    if o is None or n is None:
        return "not measured"
    if better == "neutral":
        return "neutral"
    if o == n:
        return "same"
    return "better" if (n < o) == (better == "lower") else "worse"


def rel(o, n, better):
    """Signed improvement as a fraction of OLD (positive = better)."""
    if o is None or n is None or better == "neutral" or o == n:
        return None
    if o == 0:
        return -1.0 if (n > o) == (better == "lower") else 1.0
    r = (o - n) / abs(o) if better == "lower" else (n - o) / abs(o)
    return round(max(min(r, 10.0), -10.0), 3)


# ---------- functions, patterns, bugs, cost, survived ----------

STATUS = {"landed": "proven", "proven": "proven", "done": "proven", "building": "building",
          "review": "building", "blocked": "blocked"}


def functions(new, fleet):
    rows = md_table(read(Path(new) / "spec" / "FUNCTIONS.md"), "Id")
    dash = load_json(Path(fleet) / "dash" / "functions.json") or []
    st = {str(r.get("id")): str(r.get("status", "")).lower() for r in dash if isinstance(r, dict)}
    out = []
    for r in rows:
        if len(r) < 4 or not re.fullmatch(r"[A-Z]+\d+", r[0]):
            continue
        raw = st.get(r[0])
        out.append({"id": r[0], "system": r[1], "phase": r[2], "function": r[3],
                    "status": STATUS.get(raw, "missing"),
                    "dash_status": raw if raw is not None else "not in dashboard"})
    return out, bool(dash)


def parity(fns):
    by = {}
    for f in fns:
        s = by.setdefault(f["system"], {"system": f["system"], "total": 0, "proven": 0,
                                        "building": 0, "blocked": 0, "missing": 0})
        s["total"] += 1
        s[f["status"]] += 1
    return sorted(by.values(), key=lambda r: (-r["total"], r["system"]))


def standards(new):
    rules = {}
    for line in (read(Path(new) / "spec" / "STANDARDS.md") or "").splitlines():
        m = re.match(r"\s*(?:[-*]\s*)?\**(S\d+)\**[.:]\s*(.*)", line)
        if m:
            lint = re.search(r"Lint:\s*([^.]+)", m.group(2))
            names = [x.strip() for x in lint.group(1).split(",")] if lint else []
            rules[m.group(1)] = [x for x in names if x and x.lower() != "review"]
    return rules


def lint_index(new, built):
    if not built:
        return None
    paths = files_of(new, skip_top={"spec"})
    low = [p.lower() for p in paths]
    content = []
    for p in paths:
        if "lint" in p.lower() and len(content) < 400:
            t = read(Path(new) / p)
            if t and len(t) < 400_000:
                content.append(t.lower())
    return low, "\n".join(content)


def lint_present(name, idx):
    pat = re.compile("[-_ ]".join(re.escape(p) for p in re.split(r"[-_ ]", name.lower())))
    low, content = idx
    return any(pat.search(p) for p in low) or bool(pat.search(content))


def git_subjects(repo):
    try:
        r = subprocess.run(["git", "-C", str(repo), "log", "--format=%s"], capture_output=True,
                           text=True, timeout=120)
        return r.stdout.splitlines() if r.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        return []


def find_ledger(root, given=None):
    if given:
        return Path(given) if Path(given).exists() else None
    if not root or not Path(root).exists():
        return None
    root = Path(root)
    for base, dirs, names in os.walk(root):
        depth = len(Path(base).relative_to(root).parts)
        dirs[:] = [d for d in dirs if d not in SKIP and depth < 4
                   and not d.lower().startswith(("scratch", "wt", "worktree"))]
        for n in names:
            if n.lower() in ("bugs.tsv", "bugs.md", "bugs.csv"):
                return Path(base) / n
    return None


def ledger_counts(p):
    """(rows, open, pattern-id counts) of a bug ledger."""
    text = read(p) or ""
    if p.suffix.lower() == ".md":
        rows = [r for r in md_table_any(text)]
        header = rows[0] if rows else []
        body = rows[1:]
    else:
        sep = "\t" if p.suffix.lower() == ".tsv" else ","
        lines = [l for l in text.splitlines() if l.strip()]
        header = lines[0].split(sep) if lines else []
        body = [l.split(sep) for l in lines[1:]]
    col = next((i for i, h in enumerate(header) if h.strip().lower() in ("status", "state")), None)
    open_n = None
    if col is not None:
        open_n = sum(1 for r in body if len(r) > col and r[col].strip().lower()
                     not in ("closed", "fixed", "done", "landed", "resolved", "wontfix", "dup", "duplicate"))
    pats = {}
    for r in body:
        for pid in set(re.findall(r"\bP\d{2,3}\b", " ".join(r))):
            pats[pid] = pats.get(pid, 0) + 1
    return len(body), open_n, pats


def md_table_any(text):
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not all(re.fullmatch(r":?-+:?", c) for c in cells if c):
                rows.append(cells)
    return rows


def cost_rows(new, fleet):
    rows = []
    status = load_json(Path(fleet) / "dash" / "status.json")
    s = status[0] if isinstance(status, list) and status else status if isinstance(status, dict) else None
    labels = {"proven": "Functions proven", "total": "Functions total", "rate_per_hour": "Proven per hour",
              "finish_hour": "Projected finish hour", "plan_finish_hour": "Plan finish hour",
              "agents": "Agents running"}
    if s:
        for k, lab in labels.items():
            if k in s:
                rows.append({"metric": lab, "old": None, "new": s[k], "source": "dash/status.json", "note": ""})
    rates = load_json(Path(fleet) / "dash" / "rates.json")
    if isinstance(rates, dict):
        rates = [dict(rates, metric=rates.get("metric", "fleet"))]
    if isinstance(rates, list) and rates:
        # health.py rows: {metric, per_hour, per_hour_total, done, total, peak_per_hour, measured_at, per_function?}
        fields = [("done", "done"), ("per_hour", "per hour"),
                  ("peak_per_hour", "peak per hour"), ("per_function", "per proven function")]
        for r in rates:
            if not isinstance(r, dict):
                continue
            name = str(r.get("metric", "fleet"))
            for k, lab in fields:
                if k not in r:
                    continue
                v = r[k]
                ok = isinstance(v, (int, float)) and not isinstance(v, bool)
                rows.append({"metric": f"{name.capitalize()} {lab}", "old": None, "new": v if ok else None,
                             "source": "dash/rates.json",
                             "note": "" if ok else "not measured: health.py has not measured it yet"})
    else:
        rows.append({"metric": "Tokens per proven function", "old": None, "new": None, "source": "",
                     "note": "not measured: no FLEET/dash/rates.json yet (health.py writes it)"})
    health = read(Path(fleet) / "HEALTH.tsv")
    if health is not None:
        lines = [l for l in health.splitlines() if l.strip()][1:]
        reds = sum(1 for l in lines if re.search(r"\bred\b", l, re.I))
        rows.append({"metric": "Health runs logged", "old": None, "new": len(lines), "source": "HEALTH.tsv", "note": ""})
        rows.append({"metric": "Red lines logged", "old": None, "new": reds, "source": "HEALTH.tsv", "note": ""})
    else:
        rows.append({"metric": "Health runs logged", "old": None, "new": None, "source": "",
                     "note": "not measured: no FLEET/HEALTH.tsv yet"})
    plan = read(Path(new) / "spec" / "PLAN.md") or ""
    for head in (r"throughput", r"token budget"):
        body = section(plan, head)
        for sent in re.split(r"(?<=[.;])\s+(?=[A-Z])|\n", body):
            if re.search(r"\bmeasured\b", sent, re.I) and re.search(r"\d", sent):
                rows.append({"metric": "Old fleet (as the plan recorded it)", "old": sent.strip(" -"),
                             "new": None, "source": "spec/PLAN.md", "note": ""})
        first = next((l for l in body.splitlines() if l.strip()), "")
        if head == "token budget" and first:
            rows.append({"metric": "Plan token budget", "old": None, "new": first.split(";")[0].strip(" -"),
                         "source": "spec/PLAN.md", "note": "plan target, not measured"})
    if not any(r["source"] == "spec/PLAN.md" and r["old"] for r in rows):
        rows.append({"metric": "Old fleet rates", "old": None, "new": None, "source": "",
                     "note": "not measured: the plan recorded no measured old-fleet rate"})
    return rows


def survived(old, new, fns, check_text, built):
    rows = []
    newpaths = files_of(new, skip_top={"spec"}) if built else []
    newfiles = {Path(p).name.lower() for p in newpaths}
    newstems = {Path(p).stem.lower() for p in newpaths}
    def rewritten_status(item):
        if not built:
            return "not built"
        stem = Path(item.split(" ")[0]).stem.lower()
        return "in NEW" if stem in newstems else "not in NEW yet"

    plan = read(Path(new) / "spec" / "PLAN.md") or ""
    body = section(plan, r"salvage")
    for line in body.splitlines():
        s = line.strip()
        if s.startswith(("- ", "* ")):
            part = s[2:].split(":", 1)[-1]
            for item in re.split(r"[,;]", re.sub(r"\([^)]*\)", "", part)):
                item = item.strip(" .`")
                if re.search(r"\.[A-Za-z0-9]{1,5}$", item):
                    name = Path(item).name.lower()
                    st = "not built" if not built else "in NEW" if name in newfiles else "not in NEW yet"
                    rows.append({"kind": "salvaged file", "item": item, "status": st, "source": "spec/PLAN.md"})
    for lead, kind in (("kept as data", "kept data"), ("rewritten, not salvaged", "rewritten")):
        m = re.search(lead + r"[^:]*:\s*(.+)", body, re.I)
        if m:
            for item in re.split(r",\s*(?![^()]*\))", re.split(r"\.\s+", m.group(1))[0]):
                item = re.sub(r"^(and|the)\s+", "", item.strip(" .")).strip()
                if item:
                    rows.append({"kind": kind, "item": item[:120],
                                 "status": "outside the repo" if kind == "kept data" else rewritten_status(item),
                                 "source": "spec/PLAN.md"})
    must = section(check_text or "", r"surviv")
    for line in must.splitlines():
        if line.strip().startswith(("- ", "* ", "1", "2", "3", "4", "5", "6", "7", "8", "9")):
            rows.append({"kind": "must survive (OVERHAUL-CHECK)", "item": line.strip(" -*")[:160],
                         "status": "not checked by script", "source": "OVERHAUL-CHECK.md"})
    arch = read(Path(new) / "spec" / "ARCHITECTURE.md") or ""
    for r in md_table(arch, "Id"):
        if len(r) >= 3 and re.fullmatch(r"C\d+", r[0]):
            owner = r[2].split(",")[0].strip(" `")
            crate = owner.split("::")[0]
            st = "not built" if not built else ("owner found" if crate.lower() in
                                                {p.lower() for p in os.listdir(new)} | owners_dirs(new)
                                                else "owner missing")
            rows.append({"kind": "owner capability", "item": f"{r[0]} {r[1]}", "status": st,
                         "source": "spec/ARCHITECTURE.md"})
    for s in parity(fns):
        if s["proven"] == 0:
            rows.append({"kind": "old capability NEW lacks", "item": s["system"],
                         "status": f"0 of {s['total']} functions proven", "source": "FUNCTIONS.md + dash"})
    return rows


_dirs_cache = {}


def owners_dirs(new):
    if new not in _dirs_cache:
        found = set()
        for base, dirs, _ in os.walk(new):
            dirs[:] = [d for d in dirs if d not in SKIP]
            if len(Path(base).relative_to(new).parts) < 3:
                found.update(d.lower() for d in dirs)
        _dirs_cache[new] = found
    return _dirs_cache[new]


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--fleet")
    ap.add_argument("--out", required=True)
    ap.add_argument("--old-measure")
    ap.add_argument("--new-measure")
    ap.add_argument("--old-bugs")
    a = ap.parse_args()
    old, new = Path(a.old), Path(a.new)
    fleet = Path(a.fleet) if a.fleet else new.parent / (new.name + "-fleet")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    check_text = read(old / "OVERHAUL-CHECK.md")
    om, osrc = None, ""
    for cand in filter(None, [a.old_measure, old / "OVERHAUL-CHECK-data" / "measure.json"]):
        om = load_json(cand)
        if om:
            osrc = "given measure.json" if cand == a.old_measure else "OVERHAUL-CHECK-data/measure.json"
            break
    if not om:
        om, osrc = run_measure(old, out / "old-measure.json")
    if a.new_measure:
        nm, nsrc = load_json(a.new_measure), "given measure.json"
    else:
        nm, nsrc = run_measure(new, out / "new-measure.json")
    built = bool(nm and (get(nm, "loc.code", 0) or 0) > 0)

    pat_rows = md_table(read(new / "spec" / "PATTERNS.md"), "Id")
    pat_rows = [r for r in pat_rows if len(r) >= 3 and re.fullmatch(r"P\d+", r[0])]

    # scorecard
    old_report = old_scores_from_report(check_text)
    new_report = old_scores_from_report(read(new / "OVERHAUL-CHECK.md"))
    def has_ci(m, fallback):
        root = Path(m.get("repo") or fallback) if m else Path(fallback)
        return (root / ".github" / "workflows").exists() or (root / ".gitlab-ci.yml").exists()
    old_mech = score(om, has_ci(om, old), len(pat_rows) or None) if om else {}
    new_mech = score(nm, has_ci(nm, new)) if built else {}
    scorecard = []
    for d, name in DIMS:
        if d in old_report:
            o, onum, osource = old_report[d][0], old_report[d][1], "OVERHAUL-CHECK.md"
        elif d in old_mech:
            o, onum, osource = old_mech[d][0], old_mech[d][1], "scored by script"
        else:
            o, onum, osource = None, "", "OLD not measured"
        if built and d in new_report:
            n, nnum, nnote = new_report[d][0], new_report[d][1], "NEW OVERHAUL-CHECK.md"
        elif built:
            n, nnum, nnote = new_mech[d]
            nnote = nnote if n is None else "scored by script" + (f"; {nnote}" if nnote else "")
        else:
            n, nnum, nnote = None, "", NOT_BUILT
        if d in old_mech and old_mech[d][0] is None and d not in old_report:
            osource = old_mech[d][2]
        if built and (d in old_report) != (d in new_report) and o is not None and n is not None:
            nnote += "; OLD and NEW scored from different sources"
        ch = "not measured" if o is None or n is None else "same" if o == n else "better" if n < o else "worse"
        scorecard.append({"dim": d, "name": name, "old": o, "new": n, "old_numbers": onum,
                          "new_numbers": nnum, "change": ch, "old_source": osource, "note": nnote})

    metrics = metric_rows(om, nm, built)
    fns, has_dash = functions(new, fleet)
    par = parity(fns)

    # patterns and bugs
    rules = standards(new)
    idx = lint_index(new, built)
    subjects = git_subjects(new) if built else []
    new_ledger = find_ledger(new) or find_ledger(fleet)
    new_counts = ledger_counts(new_ledger) if new_ledger else (None, None, {})
    patterns = []
    for r in pat_rows:
        rids = re.findall(r"S\d+", r[2])
        lints = sorted({l for rid in rids for l in rules.get(rid, [])})
        if not built:
            st = "not built"
        elif not lints:
            st = "review only"
        else:
            missing = [l for l in lints if not lint_present(l, idx)]
            st = "lint present" if not missing else "lint missing: " + ", ".join(missing)
        nb = sum(1 for s in subjects if re.search(rf"\b{r[0]}\b", s)) + new_counts[2].get(r[0], 0)
        patterns.append({"id": r[0], "pattern": r[1], "rules": " ".join(rids), "lints": ", ".join(lints),
                         "lint_status": st, "new_bugs": nb if built or new_ledger else None})

    old_ledger = find_ledger(old, a.old_bugs)
    oc = ledger_counts(old_ledger) if old_ledger else (None, None, {})
    okloc = (get(om, "loc.code", 0) or 0) / 1000 if om else 0
    nkloc = (get(nm, "loc.code", 0) or 0) / 1000 if nm else 0
    no_old = "not measured: no bug ledger found in OLD (pass --old-bugs)"
    no_new = "not measured: no bug ledger in NEW or FLEET"
    bugs = [
        {"measure": "Bug ledger rows", "old": oc[0], "new": new_counts[0],
         "note": ("" if old_ledger else no_old) + ("" if new_ledger else ("; " if not old_ledger else "") + no_new)},
        {"measure": "Open bugs", "old": oc[1], "new": new_counts[1],
         "note": "" if (oc[1] is not None and new_counts[1] is not None) else "not measured: ledger has no status column or is missing"},
        {"measure": "Bugs per kLOC", "old": round(oc[0] / okloc, 2) if oc[0] and okloc else None,
         "new": round(new_counts[0] / nkloc, 2) if new_counts[0] and nkloc else None,
         "note": "" if oc[0] and okloc else "not measured: needs a ledger and code"},
        {"measure": "Fix commits (git, measure window)", "old": get(om, "git.fix_commits") if om else None,
         "new": get(nm, "git.fix_commits") if nm else None, "note": "commits whose subject reads as a fix"},
        {"measure": "Commits citing a pattern id", "old": None,
         "new": sum(1 for s in subjects if re.search(r"\bP\d{2,3}\b", s)) if built else None,
         "note": "OLD predates the pattern ids" if built else NOT_BUILT},
        {"measure": "Recurring patterns (PATTERNS.md)", "old": len(pat_rows) or None, "new": None,
         "note": "NEW patterns are counted per row in the patterns table"},
    ]

    cost = cost_rows(new, fleet)
    seen = {}
    for r in cost:  # the page keys rows by metric, so names must be unique
        seen[r["metric"]] = seen.get(r["metric"], 0) + 1
        if seen[r["metric"]] > 1:
            r["metric"] = f"{r['metric']} {seen[r['metric']]}"
    surv = survived(old, new, fns, check_text, built)
    seen = set()
    surv = [r for r in surv if (r["kind"], r["item"]) not in seen and not seen.add((r["kind"], r["item"]))]

    # verdict
    total = len(fns)
    proven = sum(1 for f in fns if f["status"] == "proven")
    ranked = [m for m in metrics if m["ranked"] and m["rel"] is not None]
    imps = sorted([m for m in ranked if m["rel"] > 0], key=lambda m: -m["rel"])[:3]
    regs = sorted([m for m in ranked if m["rel"] < 0], key=lambda m: m["rel"])[:1]

    def say(m):
        return f"{m['metric']} {fmt(m['old'])} to {fmt(m['new'])} {m['unit']}"
    if not built:
        headline = f"NEW is not built yet: {proven} of {total} functions proven. Only plan parity is shown."
    else:
        headline = f"{proven} of {total} functions proven on NEW."
    verdict = [{
        "old": old.name, "new": new.name, "built": built, "proven": proven, "total": total,
        "pct": round(100 * proven / total, 1) if total else 0, "headline": headline,
        "improvement_1": say(imps[0]) if len(imps) > 0 else ("not measured" if not built else "none"),
        "improvement_2": say(imps[1]) if len(imps) > 1 else ("not measured" if not built else "none"),
        "improvement_3": say(imps[2]) if len(imps) > 2 else ("not measured" if not built else "none"),
        "regression": say(regs[0]) if regs else ("not measured" if not built else "none"),
        "dims_better": sum(1 for s in scorecard if s["change"] == "better"),
        "dims_worse": sum(1 for s in scorecard if s["change"] == "worse"),
        "dims_measured": sum(1 for s in scorecard if s["change"] != "not measured"),
        "old_verdict": next((m.group(0) for m in [re.search(r"OVERHAUL|PARTIAL REWRITE|TARGETED REFACTOR|NO ACTION",
                                                             check_text or "")] if m), "no OVERHAUL-CHECK.md"),
        "old_loc": get(om, "loc.code") if om else None, "new_loc": get(nm, "loc.code") if nm else None,
        "old_measure": osrc, "new_measure": nsrc, "functions_source": "dash/functions.json" if has_dash else "no dashboard data: all missing",
        "generated_at": now(),
    }]

    data = {"verdict": verdict, "scorecard": scorecard, "metrics": metrics, "parity": par,
            "functions": fns, "patterns": patterns, "bugs": bugs, "cost": cost, "survived": surv}
    for k, v in data.items():
        (out / f"{k}.json").write_text(json.dumps(v, indent=1, ensure_ascii=False), encoding="utf-8")

    sc = {c: sum(1 for s in scorecard if s["change"] == c) for c in ("better", "worse", "same", "not measured")}
    cnt = {c: sum(1 for f in fns if f["status"] == c) for c in ("proven", "building", "blocked", "missing")}
    lp = {}
    for p in patterns:
        k = p["lint_status"].split(":")[0]
        lp[k] = lp.get(k, 0) + 1
    print(f"{new.name} vs {old.name}: NEW {'built' if built else 'not built'}, code {fmt(verdict[0]['new_loc'])} vs {fmt(verdict[0]['old_loc'])} lines")
    print(f"functions: {proven} of {total} proven; building {cnt['building']}, blocked {cnt['blocked']}, missing {cnt['missing']}")
    print(f"scorecard: {sc['better']} better, {sc['worse']} worse, {sc['same']} same, {sc['not measured']} not measured")
    print(f"improvements: {verdict[0]['improvement_1']}; {verdict[0]['improvement_2']}; {verdict[0]['improvement_3']}; regression: {verdict[0]['regression']}")
    print(f"patterns: {len(patterns)} ({', '.join(f'{k} {v}' for k, v in lp.items()) or 'none'}); old ledger rows {fmt(oc[0])}, new {fmt(new_counts[0])}")
    print(f"wrote {len(data)} datasets to {out}")


def fmt(v):
    if v is None:
        return "not measured"
    if isinstance(v, float):
        return f"{v:,.1f}".rstrip("0").rstrip(".")
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


if __name__ == "__main__":
    main()
