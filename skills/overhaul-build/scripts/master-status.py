"""The master's to-do (overhaul-build): run at the start of every wake and act on every line. No LLM, no network.

Installed as FLEET/bin/master-status.py. Reads only FLEET files and the transcripts folder named in FLEET/BUDGET.json
(like burn.py) and prints at most 8 lines, each a concrete action or an ok summary:
  AGENTS  subagent transcripts written in the last 5 min vs FLEET/FLEET-TARGET, and the launch lines still waiting
          (newest LAUNCH-*.md lines and DISPATCH ready rows whose brief no recent transcript names;
          only launchable ones: never a held row, nor a later-phase build packet while the Phase 0 gate
          is unproven (FLEET/PHASE `<phase> [phase0_proven]` from health.py, else DISPATCH ids and statuses))
  WAIT    bin/wait.sh armed: FLEET/.wait-armed (pid and time, written by wait.sh each minute) under 3 min old
  DASH    dash/.uploaded age (red over 35 min) and the newest dash data file's age (red over 20 min)
  HEALTH  HEALTH.tsv age (red over 35 min) and whether the health scheduler entry exists
  REDS    red rows of the latest HEARTBEAT.tsv and HEALTH.tsv run (new since the last run marked *)
  LAND    PRs or QUEUE.md rows that passed review and wait to land

  py -3 master-status.py [--fleet <FLEET>] [--no-mark]
Exit code 0. The stop hook master-stop-guard.py imports status() from here.
"""
import glob, json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
DASH_UPLOAD_MAX, DASH_DATA_MAX, HEALTH_MAX, WAIT_MAX = 25, 20, 25, 3
BEAT_MAX = 25  # owner 2026-11-06: heartbeat incl. management every 20 min, heavily enforced


def age_min(path):
    try:
        return (time.time() - os.path.getmtime(path)) / 60
    except OSError:
        return None


def read(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def budget(fleet):
    try:
        return json.loads(read(os.path.join(fleet, "BUDGET.json")) or "{}")
    except ValueError:
        return {}


def subagent_files(transcripts, minutes):
    if not transcripts:
        return []
    since = time.time() - minutes * 60
    pat = os.path.join(os.path.expanduser(transcripts), "*", "subagents", "*.jsonl")
    out = []
    for f in glob.glob(pat):
        try:
            if os.path.getmtime(f) > since:
                out.append(f)
        except OSError:
            pass
    return out


def launched_briefs(transcripts):
    """Brief file names the prompts of the last 24 h of agents named (first 16 KB of each transcript)."""
    names = set()
    for f in subagent_files(transcripts, 24 * 60):
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                head = fh.read(16384)
        except OSError:
            continue
        names.update(re.findall(r"briefs[/\\]+([\w.\-]+\.md)", head))
    return names


def table_rows(text):
    """Pipe-table rows (header and separator dropped) as lists of stripped cells."""
    rows, header = [], None
    for line in text.splitlines():
        if "|" not in line:
            header = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):
            continue
        if header is None:
            header = [c.lower() for c in cells]
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def brief_name(cell):
    m = re.search(r"briefs[/\\]+([\w.\-]+\.md)", cell or "")
    return m.group(1) if m else None


# A row or LAUNCH line is launchable only if it is not held (or otherwise not startable) and, while the
# Phase 0 gate is not proven, it is not a build packet of a later phase (Acceptance, design-note, review,
# audit, ruling and land packets written ahead still count). The Phase 0 gate is a start gate.
NOT_STARTABLE = ("held", "hold", "blocked", "superseded", "landed", "merged", "done", "closed", "running",
                 "paused", "abandoned", "already holds", "proven")
FINISHED = ("landed", "merged", "done", "superseded", "closed", "already holds", "proven", "abandoned")
AHEAD_OK = re.compile(r"accept|note|\bdn\b|review|audit|ruling|merge|\bland|mechanic|dash-refresh", re.I)


def row_phase(row_id, row=None):
    """Phase number of a packet: its DISPATCH `phase` column, else its id prefix (m0-, m1-, p2-, phase0-)."""
    ph = str((row or {}).get("phase", "")).strip()
    if ph[:1].isdigit():
        return int(re.match(r"\d+", ph).group())
    m = re.match(r"(?:m|p|phase)[-_]?(\d+)(?:[-_.]|$)", (row_id or "").strip().lower())
    return int(m.group(1)) if m else None


def dispatch_rows(fleet):
    return table_rows(read(os.path.join(fleet, "DISPATCH.md")))


def phase_state(fleet, rows):
    """(current phase, Phase 0 gate proven). FLEET/PHASE, written by health.py, is `<phase> [phase0_proven]`;
    without it the current phase is the lowest phase with an unfinished DISPATCH row, and the gate counts as
    proven only when no phase-0 row is unfinished."""
    text = read(os.path.join(fleet, "PHASE")).strip().lower()
    m = re.match(r"\s*(?:phase\s*)?(\d+)", text)
    if m:
        return int(m.group(1)), "phase0_proven" in text
    open_phases = [row_phase(r.get("id"), r) for r in rows
                   if not r.get("status", "").strip().lower().startswith(FINISHED)]
    open_phases = [p for p in open_phases if p is not None]
    return min(open_phases, default=0), 0 not in open_phases


def not_launchable(row_id, text, row, phase0_proven):
    """Why a line may not be launched now, or None if it may."""
    st = (row or {}).get("status", "").strip().lower()
    if st.startswith(NOT_STARTABLE):
        return f"status {st[:30]}"
    ph = row_phase(row_id, row)
    if not phase0_proven and ph is not None and ph > 0 and not AHEAD_OK.search(f"{row_id} {text}"):
        return f"phase {ph} build packet behind the Phase 0 gate"
    return None


def launch_lines(fleet, rows=None, phase0_proven=True):
    """(file, launchable brief names, count of lines not launchable) for the newest LAUNCH-*.md."""
    files = sorted(glob.glob(os.path.join(fleet, "LAUNCH-*.md")), key=os.path.getmtime)
    if not files:
        return None, [], 0
    by_brief = {brief_name(" ".join(r.values())): r for r in (rows or [])}
    by_id = {r.get("id", ""): r for r in (rows or [])}
    out, held = [], 0
    for line in read(files[-1]).splitlines():
        if line.count("|") >= 2 and brief_name(line):
            name = brief_name(line)
            first = line.strip().strip("|").split("|")[0].strip()
            row = by_brief.get(name) or by_id.get(first)
            if not_launchable((row or {}).get("id") or first, line, row, phase0_proven):
                held += 1
            else:
                out.append(name)
    return os.path.basename(files[-1]), out, held


def dispatch_ready(fleet, rows=None, phase0_proven=True):
    rows = dispatch_rows(fleet) if rows is None else rows
    return [brief_name(" ".join(r.values())) or r.get("id", "?") for r in rows
            if r.get("status", "").strip().lower() == "ready"
            and not not_launchable(r.get("id", ""), " ".join(r.values()), r, phase0_proven)]


def latest_run(path, state_col):
    """Rows (as cell lists) of the newest run in a TSV: rows sharing the last row's time stamp."""
    lines = [l.split("\t") for l in read(path).splitlines()[-400:] if l.strip()]
    if not lines:
        return []
    stamp = lines[-1][0]
    run = [l for l in lines if l[0] == stamp]
    return [l for l in run if len(l) > abs(state_col) and l[state_col].strip() == "red"] if state_col else run


def heartbeat_value(fleet, metric):
    for row in latest_run(os.path.join(fleet, "HEARTBEAT.tsv"), 0):
        if len(row) > 3 and row[2] == metric:
            return row[3]
    return None


def queue_pass(fleet):
    return [r.get("packet", "?") for r in table_rows(read(os.path.join(fleet, "QUEUE.md")))
            if any(v.upper() == "PASS" for v in r.values()) and not any(
                v.lower() in ("landed", "merged", "done") for v in r.values())]


def health_entry(name):
    """True or False when the scheduler can be asked, None when it cannot."""
    try:
        if os.name == "nt":
            r = subprocess.run(["schtasks", "/query", "/tn", name], capture_output=True, text=True, timeout=15)
            return r.returncode == 0
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
        return "health" in r.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def fmt(m):
    return "missing" if m is None else f"{m:.0f} min"


def landed_3h(repo):
    """Gate merge commits on origin/main in the last 3 hours, or None when it cannot be measured."""
    if not repo:
        return None
    try:
        r = subprocess.run(["git", "-C", repo, "log", "--since=3 hours ago", "--oneline", "--grep=gate: merge",
                            "origin/main"], capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return len(r.stdout.splitlines()) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def status(fleet, check_scheduler=True):
    b = budget(fleet)
    tr = b.get("transcripts", "")
    s = {"paused": os.path.exists(os.path.join(fleet, "PAUSE.md")),
         "finished": os.path.exists(os.path.join(fleet, "FINISHED.md"))}
    try:
        target = int(read(os.path.join(fleet, "FLEET-TARGET")).split()[0])
    except (ValueError, IndexError):
        target = 0
    running = len(subagent_files(tr, 5))
    launched = launched_briefs(tr)
    rows = dispatch_rows(fleet)
    phase, phase0_proven = phase_state(fleet, rows)
    lfile, llines, held = launch_lines(fleet, rows, phase0_proven)
    waiting = [n for n in llines if n not in launched]
    ready = [n for n in dispatch_ready(fleet, rows, phase0_proven) if n not in launched]
    s.update(target=target, running=running, waiting=waiting, ready=ready, launch_file=lfile, held=held,
             phase=phase, phase0_proven=phase0_proven)
    # Landing brake (owner 2026-11-06, "is it just spinning wheels?"): FLEET-TARGET = landing rate x pass length,
    # capped at the owner's number. Never fill to the owner's number while nothing lands: agents only queue on the
    # check slot and the merge queue. Landings = gate merge commits on origin/main in the last 3 h.
    s["owner_target"] = target
    s["landed_3h"] = landed_3h(b.get("repo", ""))
    if s["landed_3h"] is not None:
        target = min(target, max(4, -(-s["landed_3h"] * 2 // 3)))  # per hour x ~2 h pass-to-land cycle
        s["target"] = target
    s["under"] = target > 0 and running * 4 < target * 3
    # Outcome stall alarm (owner 2026-11-06 "get your managing GOOD"): proven on deployed main and gate merges are the
    # only progress. No new proven function AND no gate merge for 60 min while not paused = STALL: the master launches a
    # root-cause agent on the phase gate's critical path that same wake, never more builders.
    s["stall"] = None
    try:
        st = json.load(open(os.path.join(fleet, "dash", "status.json"), encoding="utf-8"))
        st = st[0] if isinstance(st, list) else st
        hist = os.path.join(fleet, ".proven-history")
        now = time.time()
        rows = [l.split() for l in open(hist, encoding="utf-8")] if os.path.exists(hist) else []
        # Only a RISE counts (owner 2026-11-07: 7 hours of merges, proven flat at 4): a deploy placeholder that read 0
        # for a minute used to restart this clock and hide the stall.
        top = max([int(r[1]) for r in rows if len(r) > 1 and r[1].isdigit()] or [-1])
        cur = st.get("proven")
        if isinstance(cur, int) and cur > top:
            with open(hist, "a", encoding="utf-8") as f:
                f.write(f"{int(now)} {cur}\n")
            rows.append([str(int(now)), str(cur)])
        top = max([int(r[1]) for r in rows if len(r) > 1 and r[1].isdigit()] or [-1])
        rise = [r for r in rows if len(r) > 1 and r[1].isdigit() and int(r[1]) == top]  # when the best was first reached
        since_proven = (now - int(rise[0][0])) / 60 if rise else 0
        merged_1h = None
        repo = b.get("repo", "")
        if repo:
            r = subprocess.run(["git", "-C", repo, "log", "--since=1 hour ago", "--oneline", "--grep=gate: merge",
                                "origin/main"], capture_output=True, text=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            merged_1h = len(r.stdout.splitlines()) if r.returncode == 0 else None
        if since_proven >= 60 and merged_1h == 0:
            s["stall"] = f"proven {st.get('proven')} unchanged {int(since_proven)} min and 0 gate merges in 1 h"
        elif since_proven >= 180:  # busy, not finishing: merges land but no function completes (owner 2026-11-07)
            s["stall"] = (f"proven has not risen in {int(since_proven)} min although {merged_1h} gate merges landed in 1 h: "
                          "packets land but no function completes; push the function nearest proven to done first")
        s["proven_now"], s["merged_1h"] = st.get("proven"), merged_1h
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    s["launchable"] = bool(waiting or ready)

    s["wait_age"] = age_min(os.path.join(fleet, ".wait-armed"))
    s["wait_armed"] = s["wait_age"] is not None and s["wait_age"] <= WAIT_MAX

    dash = os.path.join(fleet, "dash")
    s["upload_age"] = age_min(os.path.join(dash, ".uploaded"))
    data = [age_min(f) for f in glob.glob(os.path.join(dash, "*.json"))]
    s["data_age"] = min([a for a in data if a is not None], default=None)
    s["dash_stale"] = s["upload_age"] is None or s["upload_age"] > DASH_UPLOAD_MAX or \
        s["data_age"] is None or s["data_age"] > DASH_DATA_MAX

    health = os.path.join(fleet, "HEALTH.tsv")
    s["has_health"] = os.path.exists(health)
    s["health_age"] = age_min(health if s["has_health"] else os.path.join(fleet, "HEARTBEAT.tsv"))
    s["health_stale"] = s["health_age"] is None or s["health_age"] > HEALTH_MAX
    beats = glob.glob(os.path.join(fleet, "heartbeat", "*.md"))
    mgmts = glob.glob(os.path.join(fleet, "reviews", "management-*.md"))
    s["beat_age"] = min((age_min(f) for f in beats), default=None)
    s["mgmt_age"] = min((age_min(f) for f in mgmts), default=None)
    s["beat_stale"] = any(a is None or a > BEAT_MAX for a in (s["beat_age"], s["mgmt_age"]))
    repo = b.get("repo") or fleet
    s["health_task"] = b.get("health_task") or os.path.basename(os.path.normpath(repo)) + "-health"
    s["health_sched"] = health_entry(s["health_task"]) if (check_scheduler and s["has_health"]) else None

    hb = [f"{r[1]} {r[2]} {r[3].split()[0] if r[3].split() else ''}/{r[4]}" for r in
          latest_run(os.path.join(fleet, "HEARTBEAT.tsv"), -1) if len(r) >= 5]
    hl = [r[1] for r in latest_run(health, 2) if len(r) > 2]
    s["reds"] = ["hb " + x for x in hb] + ["health " + x for x in hl]

    mw = heartbeat_value(fleet, "merge_waiting") or "0"
    s["merge_waiting"] = mw
    s["queue_pass"] = queue_pass(fleet)
    s["land"] = (mw.split()[0] not in ("0", "")) or bool(s["queue_pass"])
    return s


def lines(s, fleet, mark=True):
    out = []
    if s["paused"] or s["finished"]:
        return ["PAUSED (PAUSE.md): launch nothing; resume per PAUSE.md" if s["paused"] else
                "FINISHED (FINISHED.md): launch nothing"]
    head = f"AGENTS {s['running']}/{s['target']} running"
    if s["under"] and s["launchable"]:
        need = max(s["target"] - s["running"], 0)
        what = []
        if s["waiting"]:
            what.append(f"the {len(s['waiting'])} unlaunched lines of {s['launch_file']} ({', '.join(s['waiting'][:3])})")
        if s["ready"]:
            what.append(f"a dispatcher pass for {len(s['ready'])} ready DISPATCH rows")
        out.append(f"{head}, under 3/4: launch {need} now: " + " then ".join(what))
    elif s["under"]:
        out.append(f"{head}, under 3/4 and nothing launchable: have the ruling agent write the next packets now")
    if s.get("held"):
        gate = "proven" if s.get("phase0_proven") else "not proven"
        out.append(f"HELD {s['held']} lines of {s['launch_file']} are not launchable (held status, or a later-phase "
                   f"build packet while the Phase 0 gate is {gate}): never launch them")
    ok = [] if any(l.startswith("AGENTS") for l in out) else [head]
    if s["wait_armed"]:
        ok.append("wait armed")
    else:
        out.append(f"WAIT unarmed (.wait-armed {fmt(s['wait_age'])}): arm FLEET/bin/wait.sh in the background before ending this turn")
    beats = sorted(glob.glob(os.path.join(fleet, "heartbeat", "20*.md")), key=os.path.getmtime)
    if beats:  # MANAGING: BAD in the newest heartbeat blocks until a red-fix or ruling newer than it exists
        first = (open(beats[-1], encoding="utf-8", errors="replace").read(400).splitlines() or [""])
        bad = next((l for l in first if l.startswith("MANAGING: BAD")), None)
        acted = [f for f in glob.glob(os.path.join(fleet, "heartbeat", "redfix-*.md"))
                 if os.path.getmtime(f) > os.path.getmtime(beats[-1])]
        if bad and not acted:
            s["managing_bad"] = bad
            out.append(f"{bad[:220]}: act on it now (launch red-fix on that cause, or do the FIX); the turn cannot end until you do")
    if s.get("stall"):
        out.append(f"STALL {s['stall']}: launch a root-cause agent on the phase gate's critical path now (FLEET/briefs/red-fix.md "
                   f"with 'critical path only'); launch no builders until a merge or a proven function moves")
    if s["beat_stale"]:
        out.append(f"BEAT heartbeat {fmt(s['beat_age'])}, management {fmt(s['mgmt_age'])} (red over {BEAT_MAX} min): "
                   f"launch the heartbeat (FLEET/briefs/heartbeat.md) now, then dash-refresh and red-fix (FLEET/briefs/red-fix.md) on its hand-back")
    else:
        ok.append(f"beat {fmt(s['beat_age'])}")
    if s["dash_stale"]:
        out.append(f"DASH upload {fmt(s['upload_age'])}, data {fmt(s['data_age'])}: launch dash-refresh "
                   f"(FLEET/briefs/dash-refresh.md) now; if data is stale fix the writer or its scheduler entry")
    else:
        ok.append(f"dash {fmt(s['upload_age'])}")
    sched_missing = s["health_sched"] is False
    if s["health_stale"] or sched_missing:
        name = "HEALTH.tsv" if s["has_health"] else "HEARTBEAT.tsv (no HEALTH.tsv yet)"
        fix = (f"register it: schtasks /create /tn {s['health_task']} /tr \"pyw -3 <FLEET>/bin/health.py\" /sc minute /mo 10 /it /f"
               if sched_missing else "run health.py once and fix its scheduler entry")
        out.append(f"HEALTH {name} {fmt(s['health_age'])} old, scheduler entry {s['health_task']} "
                   f"{'MISSING' if sched_missing else 'present' if s['health_sched'] else 'unchecked'}: {fix}")
    else:
        ok.append(f"health {fmt(s['health_age'])}")
    if s["reds"]:
        seen_path = os.path.join(fleet, ".master-status-seen")
        seen = set(read(seen_path).splitlines())
        shown = [("*" if r not in seen else "") + r for r in s["reds"]]
        shown.sort(key=lambda r: not r.startswith("*"))
        out.append(f"REDS {len(s['reds'])} (* new): " + "; ".join(shown[:8]) + ": act on each by its action column")
        if mark:
            try:
                with open(seen_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write("\n".join(s["reds"]))
            except OSError:
                pass
    if s["land"]:
        mw = s["merge_waiting"]
        parts = []
        if mw.split()[0] not in ("0", ""):
            parts.append(f"{mw.split()[0]} PRs passed review ({' '.join(mw.split()[1:])[:60]})")
        if s["queue_pass"]:
            parts.append(f"{len(s['queue_pass'])} QUEUE.md PASS rows ({', '.join(s['queue_pass'][:3])})")
        out.append("LAND " + " + ".join(parts) + ": launch the land/merge pass now")
    if ok:
        out.append("ok: " + ", ".join(ok))
    return out[:8]


def main():
    args = sys.argv[1:]
    fleet = args[args.index("--fleet") + 1] if "--fleet" in args else os.path.dirname(HERE)
    s = status(fleet)
    print("\n".join(lines(s, fleet, mark="--no-mark" not in args)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
