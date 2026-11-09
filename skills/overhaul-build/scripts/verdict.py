"""verdict.py: the overhaul v2 heartbeat. Pure measurement over files and commands, no judgement left to an agent.

GOOD only when all of these hold:
  1 proven rose since the previous verdict, or every function is proven;
  2 the whole-tree commands in overhaul.toml [commands] (test, lint, dup, cycles, entry_lint) pass on main, run from the
    repo root (pytest exit 5, "no tests collected", is a pass, R-c; --no-checks skips them);
  3 no function has waited in needs-plan for more than 1 cycle;
  4 no log event since the previous verdict shows a builder over its call budget;
  5 once every function is proven and state.json has a polish phase, its checklist run left 0 verified P0 or P1
    NOs, i.e. every blocking checklist item is YES (references/checklist.md);
  6 when the log has probe_mutation events, each module's latest mutation score is at or above its floor
    (the event's floor, else [limits] mutation_floor, default 0.9).
Nits (capped at 5) and the open-audit trend (the latest open_audit event: SCORES s1..s7 | ISSUES n) are reported
and never gate.
Otherwise BAD, with at most 5 reds ranked by harm, each naming its automatic action (R in an action is the repo).
Reads spec/FUNCTIONS.md (the committed truth for proven, R-a), .overhaul/state.json and .overhaul/log.jsonl; the
paths follow overhaul.toml [paths]. Writes .overhaul/verdict.json, appends a "verdict" event to .overhaul/log.jsonl
and prints one line:
  VERDICT GOOD|BAD proven N/T rate X/h [issues K nits J] [trend S] reds: ...
(issues K is the polish phase's verified blocking NO count and J its nits, shown once it has started; S is the
latest open-audit trend, shown once one ran.)
The rate is proven per hour over the last 3 h of state.json proven_history, measured up to now.
  py -3 verdict.py --repo R [--no-checks] [--timeout S]     exit 0 GOOD, 1 BAD, 2 no repo
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE).replace("\\", "/")
UTC = dt.timezone.utc
CHECKS = ("test", "lint", "dup", "cycles", "entry_lint")
STATUSES = ("planned", "building", "review", "landing", "proven", "needs-plan")
ACTIVE = ("building", "review", "landing")
MAX_REDS = 5
RATE_WINDOW_H = 3.0
MUTATION_FLOOR = 0.9  # probe floor when neither the event nor [limits] mutation_floor names one
BUDGET_RE = re.compile(r"over[ _-]?budget", re.I)
SEPARATOR_RE = re.compile(r":?-+:?")


PROBE_DETAIL = re.compile(r"^(\S+): score ([\d.]+) floor ([\d.]+)")

def now():
    return dt.datetime.now(UTC)


def iso(t):
    return t.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_t(s):
    """ISO time to an aware datetime; a naive time is read as this machine's local time."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        t = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone(UTC)


def as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def short(s, n=80):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 3] + "..."


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def mini_toml(text):
    """Enough TOML for overhaul.toml when tomllib is missing: [section] lines and key = "s" | 's' | int."""
    data, sec = {}, None
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^\[([\w.-]+)\]", line)
        if m:
            sec = data.setdefault(m.group(1), {})
            continue
        m = re.match(r"""^([\w-]+)\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*'|-?\d+)""", line)
        if m and sec is not None:
            v = m.group(2)
            sec[m.group(1)] = json.loads(v) if v[0] == '"' else v[1:-1] if v[0] == "'" else int(v)
    return data


def load_toml(path):
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return {}
    if tomllib:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            pass
    return mini_toml(text)


def read_log(path):
    """Events from log.jsonl in file order; unreadable lines are skipped."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    out.append(e)
    except OSError:
        pass
    return out


def split_row(line):
    cells = re.split(r"(?<!\\)\|", line.strip())
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return [c.strip().replace("\\|", "|") for c in cells]


def read_functions(path):
    """Rows of every table in FUNCTIONS.md whose header has id and status, in plan order (None if no file)."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    rows, cols = [], None
    for line in lines:
        if not line.strip().startswith("|"):
            cols = None
            continue
        cells = split_row(line)
        if cols is None:
            low = [c.lower() for c in cells]
            if "id" in low and "status" in low:
                cols = low
            continue
        if all(SEPARATOR_RE.fullmatch(c) for c in cells):
            continue
        row = dict(zip(cols, cells))
        row["id"] = row.get("id", "").strip("`* ")
        row["status"] = row.get("status", "").strip("`* ").lower()
        if row["id"]:
            rows.append(row)
    return rows


def function_view(rows, state):
    """One dict per function in plan order. Only FUNCTIONS.md can make a function proven (R-a); any other status
    comes from state.json when it has one. With no plan table, state.json is all there is."""
    sfns = state.get("functions") if isinstance(state.get("functions"), dict) else {}
    if not rows:
        rows = [{"id": k, "status": str(v.get("status") or "").lower()} for k, v in sfns.items() if isinstance(v, dict)]
    view, seen = [], set()
    for r in rows:
        fid = r["id"]
        if fid in seen:
            continue
        seen.add(fid)
        s = sfns.get(fid) if isinstance(sfns.get(fid), dict) else {}
        st = str(s.get("status") or "").strip().lower()
        md = r.get("status") or ""
        status = md if md in ("proven", "deferred") else (st if st and st != "proven" else (md or "planned"))
        view.append({"id": fid, "module": r.get("module", ""), "phase": r.get("phase", ""), "status": status,
                     "attempts": as_int(s.get("attempts")), "last_reason": str(s.get("last_reason") or ""),
                     "updated": s.get("updated"), "cycle": s.get("cycle")})
    return view


def load_project(repo):
    """Everything the verdict and the dash read, measured once: config, state, the function view and counts."""
    cfg = load_toml(os.path.join(repo, "overhaul.toml"))
    paths = cfg.get("paths") if isinstance(cfg.get("paths"), dict) else {}
    state_path = os.path.join(repo, paths.get("state") or ".overhaul/state.json")
    odir = os.path.dirname(state_path)
    state = load_json(state_path, {})
    state = state if isinstance(state, dict) else {}
    fpath = paths.get("functions") or "spec/FUNCTIONS.md"
    every = function_view(read_functions(os.path.join(repo, fpath)), state)
    view = [f for f in every if f["status"] != "deferred"]  # planned later: neither proven nor missing
    return {"repo": repo, "cfg": cfg, "fpath": fpath, "state": state, "view": view, "total": len(view),
            "deferred": len(every) - len(view),
            "proven": sum(f["status"] == "proven" for f in view), "odir": odir,
            "log": os.path.join(odir, "log.jsonl"), "verdict": os.path.join(odir, "verdict.json")}


def rate_per_h(history, proven, t_now, window_h=RATE_WINDOW_H):
    """Proven per hour over the last window_h hours of proven_history ([[iso, n], ...]), measured up to now."""
    if not isinstance(history, list):
        return 0.0
    pts = []
    for item in history:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            t, n = parse_t(item[0]), as_int(item[1], None)
            if t and n is not None:
                pts.append((t, n))
    if not pts:
        return 0.0
    pts.sort(key=lambda pt: pt[0])
    start = t_now - dt.timedelta(hours=window_h)
    before = [pt for pt in pts if pt[0] <= start]
    t0, n0 = (start, before[-1][1]) if before else pts[0]
    return (proven - n0) / max((t_now - t0).total_seconds() / 3600, 0.25)


def run_checks(repo, commands, timeout):
    """Run each whole-tree command from the repo root; {name: {cmd, exit, ok, seconds, tail}}."""
    out = {}
    for name in CHECKS:
        cmd = commands.get(name)
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        cmd = cmd.replace("<skill>", SKILL_DIR)
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout)
            code, text = p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            code, text = "timeout", f"timed out after {timeout} s"
        tail = [ln.strip() for ln in text.splitlines() if ln.strip()][-3:]
        out[name] = {"cmd": cmd, "exit": code, "ok": code == 0 or (code == 5 and "pytest" in cmd),
                     "seconds": round(time.monotonic() - t0, 1), "tail": short(" | ".join(tail), 300)}
    return out


def progress_action(view):
    active = [f["id"] for f in view if f["status"] in ACTIVE]
    failed = sorted((f for f in view if f["status"] != "proven" and f["last_reason"]), key=lambda f: -f["attempts"])
    if not active and not failed:
        return "start drive.py --repo R: nothing is in flight"
    if failed:
        f = failed[0]
        return (f"fix the top failure {f['id']} ({short(f['last_reason'], 60)}): plan commit, "
                f"then drive.py --repo R --reset {f['id']}")
    return f"keep drive.py running: {len(active)} in flight ({', '.join(active[:3])})"


def as_fraction(v):
    """A score as a fraction: 0.93, 93 and "93%" are all 0.93; None when it is not a number."""
    try:
        f = float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    return f / 100 if f > 1 else f


def probe_floors(events, limits):
    """[(module, score, floor)] from each module's latest probe_mutation event."""
    default = as_fraction(limits.get("mutation_floor"))
    latest = {}
    for e in events:
        if e.get("event") != "probe_mutation":
            continue
        m = PROBE_DETAIL.match(str(e.get("detail") or ""))  # drive.py writes "<module>: score S floor F ..."
        score = as_fraction(e.get("score") if e.get("score") is not None else m and m.group(2))
        if score is not None:
            floor = as_fraction(e.get("floor") if e.get("floor") is not None else m and m.group(3))
            latest[str(e.get("module") or (m and m.group(1)) or e.get("fn") or "?")] = (
                score, floor if floor is not None else default if default is not None else MUTATION_FLOOR)
    return [(m, s, f) for m, (s, f) in sorted(latest.items())]


def open_audit_trend(events):
    """The latest open_audit event's detail (SCORES s1..s7 | ISSUES n), or None. A trend, never a gate."""
    for e in reversed(events):
        if e.get("event") == "open_audit" and str(e.get("detail") or "").strip():
            return short(str(e["detail"]).strip(), 80)
    return None


def judge(p, prev, t_now, checks, events):
    """The verdict document from measurements; checks is None when skipped."""
    view, proven, total, state = p["view"], p["proven"], p["total"], p["state"]
    reds = []  # (harm rank, kind, what, action)
    if not total:
        reds.append((0, "plan", f"no functions in {p['fpath']}", "run overhaul-plan: it writes the function table"))
    if checks is not None:
        if not checks:
            reds.append((1, "check", "overhaul.toml has no whole-tree [commands]",
                         "overhaul-plan adds test, lint, dup, cycles and entry_lint to overhaul.toml"))
        for name, r in checks.items():
            if not r["ok"]:
                reds.append((1, "check", f"{name} red on main (exit {r['exit']})",
                             f"hold landing; a fix builder makes `{r['cmd']}` pass on main"))
    prev_proven = prev.get("proven") if isinstance(prev.get("proven"), int) else None
    if total and proven <= (prev_proven or 0) and proven < total:
        since_txt = f"previous verdict {prev_proven}" if prev_proven is not None else "first verdict"
        reds.append((2, "progress", f"proven stuck at {proven} ({since_txt})", progress_action(view)))

    pol = state.get("polish") if isinstance(state.get("polish"), dict) else None
    issues = pol.get("issues") if pol else None
    if pol and total and proven == total and (pol.get("status") != "done" or issues != 0):
        reds.append((2, "polish", f"polish {pol.get('status')}: {'?' if issues is None else issues} issues "
                     f"(verified P0/P1 checklist NOs) after round {pol.get('round')}",
                     "drive.py --repo R runs the polish audit and fix passes; issues left after [limits] "
                     "polish_rounds are in R/.overhaul/polish/: fix them or raise polish_rounds"))

    limits = p["cfg"].get("limits") if isinstance(p["cfg"].get("limits"), dict) else {}
    for mod, score, floor in probe_floors(events, limits):
        if score < floor:
            reds.append((2, "probe", f"mutation score {score:.0%} < floor {floor:.0%} in {mod}",
                         f"the survivors of {mod} go to one fix pass; see probe_mutation in R/.overhaul/log.jsonl"))
    nits = pol.get("nits") if pol else None
    trend = open_audit_trend(events)

    seq = as_int(prev.get("seq")) + 1
    cycle = as_int(state.get("cycle"), seq)
    prev_since = prev.get("needs_plan_since") if isinstance(prev.get("needs_plan_since"), dict) else {}
    prev_t = parse_t(prev.get("t"))
    since = {}
    for f in view:
        if f["status"] != "needs-plan":
            continue
        fid, upd = f["id"], parse_t(f["updated"])
        if as_int(f["cycle"], None) is not None:
            since[fid] = as_int(f["cycle"])
        elif fid in prev_since and not (prev_t and upd and upd > prev_t):
            since[fid] = as_int(prev_since[fid], cycle)
        else:
            since[fid] = cycle
        wait = cycle - since[fid]
        if wait > 1:
            why = f": {short(f['last_reason'], 60)}" if f["last_reason"] else ""
            reds.append((3, "needs-plan", f"{fid} waiting in needs-plan {wait} cycles{why}",
                         f"plan commit for {fid} (spec/, tests/acceptance/, overhaul.toml) once plan_validate.py "
                         f"prints PLAN OK, then drive.py --repo R --reset {fid}"))

    max_calls = as_int(limits.get("builder_max_calls"), None)
    over = {}
    for e in events:
        t = parse_t(e.get("t"))
        if e.get("event") == "verdict" or (prev_t and t and t <= prev_t):
            continue
        calls = as_int(e.get("calls"), None)
        if BUDGET_RE.search(f"{e.get('event', '')} {e.get('detail', '')}") or (
                max_calls and calls is not None and calls > max_calls):
            over.setdefault(str(e.get("fn") or "?"), short(e.get("detail") or e.get("event") or "", 60))
    for fn, det in over.items():
        reds.append((4, "budget", f"builder {fn} over budget ({det})",
                     f"split {fn} or trim its brief in a plan commit, then drive.py --repo R --reset {fn}"))

    reds.sort(key=lambda r: r[0])
    shown = reds[:MAX_REDS]
    verdict = "BAD" if reds else "GOOD"
    rate = rate_per_h(state.get("proven_history"), proven, t_now)
    text = "; ".join(f"{w} -> {a}" for _, _, w, a in shown) or "none"
    if len(reds) > MAX_REDS:
        text += f" (+{len(reds) - MAX_REDS} more)"
    counts = {}
    for f in view:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    return {"t": iso(t_now), "seq": seq, "cycle": cycle, "verdict": verdict, "proven": proven, "total": total,
            "prev_proven": prev_proven, "rate_per_h": round(rate, 2), "counts": counts,
            "reds": [{"kind": k, "what": w, "action": a} for _, k, w, a in shown], "reds_total": len(reds),
            "checks": checks if checks is not None else "skipped", "needs_plan_since": since,
            "polish_issues": issues, "nits": nits, "trend": trend,
            "deferred": p.get("deferred", 0),
            "line": f"VERDICT {verdict} proven {proven}/{total}"
                    f"{' deferred ' + str(p['deferred']) if p.get('deferred') else ''} rate {rate:.1f}/h"
                    f"{'' if pol is None else ' issues ' + ('?' if issues is None else str(issues))}"
                    f"{'' if pol is None else ' nits ' + str(as_int(nits))}"
                    f"{'' if trend is None else ' trend ' + trend} reds: {text}"}


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def append_event(path, ev):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def say(line):
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="overhaul v2 heartbeat verdict")
    ap.add_argument("--repo", required=True, help="the project repo (main's checkout)")
    ap.add_argument("--no-checks", action="store_true", help="skip the whole-tree commands")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds allowed per command")
    a = ap.parse_args(argv)
    repo = os.path.abspath(a.repo)
    if not os.path.isdir(repo):
        print(f"verdict: no repo at {repo}", file=sys.stderr)
        return 2
    t0, t_now = time.monotonic(), now()
    p = load_project(repo)
    prev = load_json(p["verdict"], {})
    prev = prev if isinstance(prev, dict) else {}
    cmds = p["cfg"].get("commands") if isinstance(p["cfg"].get("commands"), dict) else {}
    checks = None if a.no_checks else run_checks(repo, cmds, a.timeout)
    doc = judge(p, prev, t_now, checks, read_log(p["log"]))
    write_atomic(p["verdict"], json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    append_event(p["log"], {"t": doc["t"], "fn": "-", "event": "verdict", "detail": doc["line"],
                            "seconds": round(time.monotonic() - t0, 1), "tokens": 0})
    say(doc["line"])
    return 0 if doc["verdict"] == "GOOD" else 1


if __name__ == "__main__":
    sys.exit(main())
