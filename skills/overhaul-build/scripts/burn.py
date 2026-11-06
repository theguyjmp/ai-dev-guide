"""Token burn check (run by heartbeat.py every 10 min; no LLM). Measures from the Claude transcripts:
  tokens_per_hour    all usage (input + cache read + cache write + output) in the last 60 min, main chat and agents
  main_ctx           context size of the main chat's latest turn
  agents_per_hour    agent transcripts started in the last 60 min
  tokens_no_land     tokens spent since the last merge into origin/main, when that merge is older than 6 h
Thresholds live in FLEET/BUDGET.json. Any red writes FLEET/BURN-STOP (reason inside); the PreToolUse guard
bin/burn-guard.py then refuses every new Agent launch until the master fixes the cause and deletes BURN-STOP."""
import glob, json, os, subprocess, time

FLEET = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# BUDGET.json also names the project: {"repo": "<repo path>", "transcripts": "<~/.claude/projects/<slug>>", ...}
DEFAULTS = {"tokens_per_hour": 30_000_000, "main_ctx": 200_000, "agents_per_hour": 30, "tokens_no_land": 300_000_000,
            "repo": "", "transcripts": ""}


def budget():
    try:
        with open(os.path.join(FLEET, "BUDGET.json"), encoding="utf-8") as f:
            return {**DEFAULTS, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def usage_rows(path, since):
    """Yield (epoch, tokens, context) for each usage line newer than since. The file mtime bounds the scan."""
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if '"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            u = (d.get("message") or {}).get("usage") or {}
            ts = d.get("timestamp")
            if not u or not ts:
                continue
            t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            if t < since:
                continue
            ctx = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            yield t, ctx + u.get("output_tokens", 0), ctx


def last_main_merge(repo):
    try:
        out = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%ct", "origin/main"],
                             capture_output=True, text=True, timeout=20, check=True).stdout.strip()
        return int(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def measure(b):
    proj = os.path.expanduser(b["transcripts"])
    now = time.time()
    hour = now - 3600
    land = last_main_merge(b["repo"])
    since_land = max(land, now - 48 * 3600)
    mains = [f for f in glob.glob(os.path.join(proj, "*.jsonl")) if os.path.getmtime(f) > since_land]
    subs = [f for f in glob.glob(os.path.join(proj, "*", "subagents", "*.jsonl")) if os.path.getmtime(f) > since_land]
    tph = tnl = 0
    main_ctx = 0
    main_t = 0
    for f in mains + subs:
        for t, tok, ctx in usage_rows(f, since_land):
            tnl += tok
            if t >= hour:
                tph += tok
            if f in mains and t > main_t:
                main_t, main_ctx = t, ctx
    aph = sum(1 for f in subs if os.path.getctime(f) >= hour)
    stale_land = land and now - land > 6 * 3600
    return {"tokens_per_hour": tph, "main_ctx": main_ctx, "agents_per_hour": aph,
            "tokens_no_land": tnl if stale_land else 0}


def rows(stamp):
    b = budget()
    m = measure(b)
    out, reds = [], []
    for k in ("tokens_per_hour", "main_ctx", "agents_per_hour", "tokens_no_land"):
        red = m[k] > b[k]
        out.append((stamp, "burn", k, str(m[k]), str(b[k]), "red" if red else "green"))
        if red:
            reds.append(f"{k} {m[k]:,} over {b[k]:,}")
    stop = os.path.join(FLEET, "BURN-STOP")
    if reds and not os.path.exists(stop):
        with open(stop, "w", encoding="utf-8", newline="\n") as f:
            f.write(stamp + " " + "; ".join(reds) + "\n")
    return out


if __name__ == "__main__":
    for r in rows(time.strftime("%Y-%m-%d %H:%M")):
        print("\t".join(r))
