"""verdict.py: the one heartbeat verdict (overhaul-build). Pure measurement, no judgement left to an agent.

GOOD only when all four hold:
  1 proven rose: functions proven on deployed main now > proven 3 h ago (dash/proven.tsv, written by tick.py);
  2 land backlog under the brake: PASS PRs waiting <= max(2, brake_factor x gate merges per hour over 3 h);
  3 every scheduled check in fleet.json has a fresh ok row in CHECKS.tsv (fresh = within 2 x its cadence + 5 min);
  4 quality is green (dash/quality.json "green": true, written by the project's quality check).
Otherwise BAD. Prints the verdict line, then at most 5 reds ranked by harm, each naming its automatic fix.
Writes dash/verdict.json (read by burn-guard.py for the brake and by master-stop-guard.py).
  py -3 verdict.py [--fleet <FLEET>] [--json]
"""
import datetime as dt
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
UTC = dt.timezone.utc


def now():
    return dt.datetime.now(UTC)


def parse_t(s):
    try:
        t = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except ValueError:
        return None


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def config(fleet):
    cfg = load_json(os.path.join(fleet, "fleet.json"), {}) or {}
    cfg.setdefault("brake_factor", 2)
    cfg.setdefault("merge_grep", "gate: merge")
    cfg.setdefault("checks", [])
    return cfg


def proven(fleet):
    rows = load_json(os.path.join(fleet, "dash", "pipeline.json"), []) or []
    for r in rows:
        if r.get("bar") == "functions" and r.get("stage") == "proven":
            return int(r.get("count", 0)), int(r.get("total", 0))
    return None, None


def proven_3h_ago(fleet):
    """Newest proven.tsv row at least 3 h old; None when the history is shorter than 3 h."""
    cut, best = now() - dt.timedelta(hours=3), None
    try:
        with open(os.path.join(fleet, "dash", "proven.tsv"), encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                t = parse_t(p[0]) if len(p) > 1 else None
                if t and t <= cut and p[1].isdigit():
                    best = int(p[1])
    except OSError:
        pass
    return best


def merges_per_hour(cfg):
    repo = cfg.get("repo")
    if not repo:
        return None
    try:
        subprocess.run(["git", "-C", repo, "fetch", "-q", "origin"], timeout=120, capture_output=True)
        out = subprocess.run(["git", "-C", repo, "log", "origin/main", "--since=3 hours ago", "--oneline",
                              f"--grep={cfg['merge_grep']}"], timeout=60, capture_output=True, text=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return len([l for l in out.splitlines() if l.strip()]) / 3.0


def last_rows(fleet):
    """Last CHECKS.tsv row per check: name -> (ended, status, detail)."""
    last = {}
    try:
        with open(os.path.join(fleet, "CHECKS.tsv"), encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) >= 5:
                    last[p[0]] = (parse_t(p[2]), p[3], p[4])
    except OSError:
        pass
    return last


def measure(fleet):
    cfg = config(fleet)
    reds = []
    p_now, total = proven(fleet)
    p_old = proven_3h_ago(fleet)
    if p_now is None:
        reds.append((95, "pipeline.json has no proven row", "auto: tick reruns the dashboard generator; second miss files inbox/tool-dashdata"))
    elif p_old is None:
        reds.append((90, f"proven {p_now}: under 3 h of proven history, rise not shown yet", "auto: none needed; tick records proven every run"))
    elif p_now <= p_old:
        reds.append((90, f"proven flat 3 h ({p_old} -> {p_now})", "auto: dispatch orders units nearest proven first; burn-guard brakes new-unit launches until one proves"))
    mph = merges_per_hour(cfg)
    land = load_json(os.path.join(fleet, "dash", "land.json"), {}) or {}
    waiting = land.get("pass_waiting")
    brake_at = max(2.0, cfg["brake_factor"] * (mph or 0.0))
    brake = waiting is None or waiting > brake_at
    if waiting is None:
        reds.append((85, "dash/land.json missing: land backlog unmeasured", "auto: land-keeper writes it every 5 min; brake held on until it does"))
    elif brake:
        reds.append((85, f"land backlog {waiting} PASS waiting > brake {brake_at:.0f} ({mph or 0:.1f} merges/h)",
                     "auto: burn-guard refuses build launches; land-keeper retargets, merges main, retries"))
    last, t = last_rows(fleet), now()
    fresh = 0
    for c in cfg["checks"]:
        name, every = c["name"], float(c.get("every_min", 20))
        row = last.get(name)
        fix = c.get("fix", f"auto: tick reruns {name}; a second fail files inbox/tool-{name}.md as a tool-fix unit")
        if not row or not row[0]:
            reds.append((c.get("harm", 60), f"check {name}: no result ever", fix))
        elif row[1] != "ok":
            reds.append((c.get("harm", 60), f"check {name}: {row[1]} ({row[2][:80]})", fix))
        elif (t - row[0]).total_seconds() / 60 > 2 * every + 5:
            reds.append((c.get("harm", 60), f"check {name}: stale {int((t - row[0]).total_seconds() // 60)} min", fix))
        else:
            fresh += 1
    q = load_json(os.path.join(fleet, "dash", "quality.json"), {}) or {}
    if not q.get("green"):
        reds.append((70, "quality " + (q.get("line") or "unmeasured"),
                     q.get("fix") or "auto: quality check files a fix unit per real bug or duplicate"))
    reds.sort(key=lambda r: -r[0])
    good = not reds
    line = (f"VERDICT {'GOOD' if good else 'BAD'}: proven {p_now}/{total} (3 h ago {p_old}), "
            f"merges/h {mph if mph is None else round(mph, 1)}, backlog {waiting}/{brake_at:.0f} brake {'ON' if brake else 'off'}, "
            f"checks {fresh}/{len(cfg['checks'])} fresh, quality {'GREEN' if q.get('green') else 'RED'}")
    return {"good": good, "line": line, "brake": brake, "proven": p_now, "total": total,
            "measured_at": t.isoformat(timespec="seconds"),
            "reds": [{"harm": h, "red": w, "fix": f} for h, w, f in reds[:5]], "reds_total": len(reds)}


def main():
    a = sys.argv[1:]
    fleet = a[a.index("--fleet") + 1] if "--fleet" in a else os.path.dirname(HERE)
    v = measure(fleet)
    os.makedirs(os.path.join(fleet, "dash"), exist_ok=True)
    tmp = os.path.join(fleet, "dash", "verdict.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(v, f, indent=1)
    os.replace(tmp, os.path.join(fleet, "dash", "verdict.json"))
    if "--json" in a:
        print(json.dumps(v, indent=1))
    else:
        print(v["line"])
        for i, r in enumerate(v["reds"], 1):
            print(f"RED {i} [{r['harm']}] {r['red']} -> {r['fix']}")
    return 0 if v["good"] else 1


if __name__ == "__main__":
    sys.exit(main())
