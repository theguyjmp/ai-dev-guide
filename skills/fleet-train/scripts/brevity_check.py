"""Brevity budgets: what agents read at start may not grow until it floods a context.

  py -3 brevity_check.py             # BREVITY <file> <measure> <now> > <cap> per breach; exit 1 if any
  py -3 brevity_check.py --tighten   # lower the code ratchets to today's values (never raises)

Measures <HARNESS_HOME>: state file, notes/ (briefs, handbacks, rulings, RUNBOOK, live words), code copies,
events.jsonl, comment share of *.py. Caps: DEFAULTS, overridden by brevity_budget.json there; they only go down.
Files in `baseline_commit` skip the new-file caps; `code_ratchet` grandfathers a file over the comment cap.
"""
from __future__ import annotations

import ast, io, json, os, re, subprocess, sys, tokenize
from pathlib import Path

DEFAULTS = {
    "baseline_commit": "",
    "state": {"file": "STATE.md", "max_words": 800, "max_lines": 60, "max_line_chars": 200},
    "brief_words": 700,
    "handback_words": 400,
    "handback_first_line_words": 30,
    "ruling_words": 400,
    "runbook_heading_words": 400,
    "live_notes_words": 60000,
    "read_whole_max_lines": 300,
    "read_whole_max_words": 3000,
    "events_max_bytes": 5_000_000,
    "comment_share_max": 0.2,
    "copy_globs": ["*-next.py", "*.bak*", "state/*.py", "state/*.bak*"],
    "code_ratchet": {},
}
DATED = re.compile(r"\b20\d\d-\d\d-\d\d\b")
DATED_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:20\d\d-\d\d-\d\d|\d\d:\d\d(?::\d\d)?Z)")
READ_WHOLE = re.compile(r"\b[Rr]ead(?: first:| whole)? ([A-Za-z0-9_./-]+\.(?:md|py|json|txt))\b")


def words(text: str) -> int:
    return len(text.split())


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def default_home() -> Path:
    return Path(os.environ.get("HARNESS_HOME", str(Path(__file__).parent)))


def load_budget(home: Path) -> dict:
    p = home / "brevity_budget.json"
    own = json.loads(read(p)) if p.exists() else {}
    return {**DEFAULTS, **own, "state": {**DEFAULTS["state"], **own.get("state", {})}}


def baseline_files(home: Path, commit: str) -> set:
    """Paths tracked in the baseline commit (old files are exempt from the new-file caps); none without one."""
    if not commit:
        return set()
    out = subprocess.run(["git", "ls-tree", "-r", "--name-only", commit], cwd=home, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"git ls-tree {commit}: {out.stderr.strip()[:200]}")
    return set(out.stdout.splitlines())


def comment_stats(src: str) -> tuple:
    """(share of words in comments and docstrings, comment lines carrying a date) of one Python file."""
    comment_words, dated = 0, 0
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            comment_words += words(tok.string.lstrip("#"))
            dated += bool(DATED.search(tok.string))
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                comment_words += words(doc)
                dated += sum(bool(DATED.search(line)) for line in doc.splitlines())
    total = words(src)
    return (comment_words / total if total else 0.0), dated


def measure_ratchets(home: Path) -> dict:
    """{file: {share, dated}} of every *.py file in the coordinator folder."""
    out = {}
    for p in sorted(home.glob("*.py")):
        try:
            share, dated = comment_stats(read(p))
        except (tokenize.TokenError, SyntaxError) as e:
            raise ValueError(f"{p.name} does not parse: {e}") from e
        out[p.name] = {"share": round(share, 3), "dated": dated}
    return out


def headings(text: str) -> list:
    return [(p.splitlines()[0].strip(), p) for p in re.split(r"^## ", text, flags=re.M)[1:]]


def breaches(home: Path | None = None, repo: Path | None = None, budget: dict | None = None) -> list:
    home = Path(home or default_home())
    repo = Path(repo or os.environ.get("HARNESS_REPO", "."))
    b = budget or load_budget(home)
    old = baseline_files(home, b["baseline_commit"])
    out = []

    def over(path: str, measure: str, now: float, cap: float) -> None:
        if now > cap:
            out.append(f"BREVITY {path} {measure} {now:g} > {cap:g}")

    st = b["state"]
    state_path = home / st["file"]
    if state_path.exists():
        lines = read(state_path).splitlines()
        over(st["file"], "words", sum(map(words, lines)), st["max_words"])
        over(st["file"], "lines", len(lines), st["max_lines"])
        over(st["file"], "longest-line", max((len(x) for x in lines), default=0), st["max_line_chars"])
        over(st["file"], "dated-lines", sum(bool(DATED_LINE.match(x)) for x in lines), 0)

    def new(d: str) -> list:
        return [p for p in sorted((home / d).glob("*.md")) if p.relative_to(home).as_posix() not in old]

    for p in new("notes/briefs"):
        if p.name == "INDEX.md":
            continue
        text, rel = read(p), p.relative_to(home).as_posix()
        over(rel, "words", words(text), b["brief_words"])
        for ref in READ_WHOLE.findall(text):
            target = next((r / ref for r in (home, repo) if (r / ref).is_file()), None)
            if target:
                t = read(target)
                over(f"{rel}->{ref}", "lines", len(t.splitlines()), b["read_whole_max_lines"])
                over(f"{rel}->{ref}", "words", words(t), b["read_whole_max_words"])
    for p in new("notes/handbacks"):
        text, rel = read(p), p.relative_to(home).as_posix()
        over(rel, "words", words(text), b["handback_words"])
        over(rel, "first-line-words", words(text.splitlines()[0]) if text.strip() else 0, b["handback_first_line_words"])
    for p in new("notes/rulings"):
        over(p.relative_to(home).as_posix(), "words", words(read(p)), b["ruling_words"])

    runbook = home / "notes" / "RUNBOOK.md"
    if runbook.exists():
        for name, body in headings(read(runbook)):
            over(f"notes/RUNBOOK.md#{name}", "words", words(body), b["runbook_heading_words"])
    notes = home / "notes"
    live = sum(words(read(p)) for p in notes.rglob("*.md") if "archive" not in p.relative_to(notes).parts) \
        if notes.is_dir() else 0
    over("notes/", "live-words", live, b["live_notes_words"])

    copies = sorted({p.relative_to(home).as_posix() for g in b["copy_globs"] for p in home.glob(g)})
    if copies:
        out.append(f"BREVITY copies {len(copies)} > 0 ({', '.join(copies[:3])}): delete them, git history keeps them")
    events = home / "state" / "events.jsonl"
    if events.exists():
        over("state/events.jsonl", "bytes", events.stat().st_size, b["events_max_bytes"])

    for name, now in measure_ratchets(home).items():
        rec = b["code_ratchet"].get(name, {})
        over(name, "comment-share", now["share"], max(b["comment_share_max"], rec.get("share", 0)))
        over(name, "dated-comment-lines", now["dated"], rec.get("dated", 0))
    return out


def tighten(home: Path | None = None) -> int:
    """Lower each code ratchet to its current value when lower; drop entries back within the cap. Never raises one."""
    home = Path(home or default_home())
    p = home / "brevity_budget.json"
    own = json.loads(read(p)) if p.exists() else {}
    ratchet = own.get("code_ratchet", {})
    cap, changed = own.get("comment_share_max", DEFAULTS["comment_share_max"]), 0
    for name, now in measure_ratchets(home).items():
        rec = ratchet.get(name)
        if rec is None:
            continue
        for k in ("share", "dated"):
            if now[k] < rec[k]:
                rec[k], changed = now[k], changed + 1
        if rec["share"] <= cap and rec["dated"] == 0:
            del ratchet[name]
    if own:
        p.write_text(json.dumps(own, indent=1) + "\n", encoding="utf-8")
    return changed


def main() -> None:
    if sys.argv[1:] == ["--tighten"]:
        print(f"tightened {tighten()} ratchet values")
        return
    found = breaches()
    print("\n".join(found) if found else "BREVITY OK")
    sys.exit(1 if found else 0)


if __name__ == "__main__":
    main()
