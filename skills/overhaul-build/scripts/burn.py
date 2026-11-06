"""Waste check (run by heartbeat.py every 10 min; no LLM). Spend on work is fine; spend that produces nothing is
the red. Owner 2026-11-06: "I'm not worried about how many tokens are spent if the work is getting done; I'm
worried about burned tokens that do nothing."

Measured from the Claude transcripts. Info rows (never red, never a brake):
  tokens_per_hour      all usage (input + cache read + cache write + output) in the last 60 min, main chat and agents
  agents_per_hour      agent transcripts started in the last 60 min
Waste rows (red over FLEET/BUDGET.json):
  main_ctx             context of the main chat's latest turn (every later turn replays it)
  main_turns_per_hour  main-chat turns in the last 60 min (each notice answered is a full replay that builds nothing)
  waste_share          share of last-hour tokens spent by the main chat plus agents that have made reader_calls or more
                       tool calls since their last edit or commit (reading, waiting or spinning, not building);
                       judged only when the hour's tokens are over waste_floor
  tokens_no_land       tokens spent since the last merge into origin/main, when that merge is older than 6 h
Any waste red writes FLEET/BURN-STOP (reasons inside); the PreToolUse guard bin/burn-guard.py then refuses new agent
launches. This script deletes BURN-STOP itself on the first run where every waste row is green, so a brake never
outlives its cause and a burst of real work never trips it."""
import glob, json, os, subprocess, time

FLEET = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# BUDGET.json also names the project: {"repo": "<repo path>", "transcripts": "<~/.claude/projects/<slug>>", ...}
DEFAULTS = {"main_ctx": 300_000, "main_turns_per_hour": 60, "waste_share": 0.5, "waste_floor": 20_000_000,
            "reader_calls": 30, "tokens_no_land": 300_000_000, "repo": "", "transcripts": ""}
INFO = ("tokens_per_hour", "agents_per_hour")
WASTE = ("main_ctx", "main_turns_per_hour", "waste_share", "tokens_no_land")
EDITS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
COMMIT_WORDS = ("git commit", "git push", "vc wip", "gh pr create")


def budget():
    try:
        with open(os.path.join(FLEET, "BUDGET.json"), encoding="utf-8") as f:
            return {**DEFAULTS, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def is_edit(block):
    if block.get("name") in EDITS:
        return True
    cmd = str((block.get("input") or {}).get("command", ""))
    return any(w in cmd for w in COMMIT_WORDS)


def scan(path, since):
    """Yield (epoch, tokens, context, calls_since_edit) per usage line newer than since. calls_since_edit counts the
    tool calls made since the transcript's last edit or commit, over the whole transcript."""
    calls = 0
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if '"tool_use"' not in line and '"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msg = d.get("message") or {}
            content = msg.get("content")
            for block in content if isinstance(content, list) else ():
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    calls = 0 if is_edit(block) else calls + 1
            u = msg.get("usage") or {}
            ts = d.get("timestamp")
            if not u or not ts:
                continue
            t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            if t < since:
                continue
            ctx = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            yield t, ctx + u.get("output_tokens", 0), ctx, calls


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
    since = min(max(land, now - 48 * 3600), hour)
    mains = [f for f in glob.glob(os.path.join(proj, "*.jsonl")) if os.path.getmtime(f) > since]
    subs = [f for f in glob.glob(os.path.join(proj, "*", "subagents", "*.jsonl")) if os.path.getmtime(f) > since]
    tph = tnl = waste = mtph = main_ctx = main_t = 0
    for f in mains + subs:
        main = f in mains
        for t, tok, ctx, calls in scan(f, since):
            if t >= land:
                tnl += tok
            if t >= hour:
                tph += tok
                if main:
                    mtph += 1
                if main or calls >= b["reader_calls"]:
                    waste += tok
            if main and t > main_t:
                main_t, main_ctx = t, ctx
    stale_land = land and now - land > 6 * 3600
    share = round(waste / tph, 2) if tph > b["waste_floor"] else 0
    return {"tokens_per_hour": tph, "agents_per_hour": sum(1 for f in subs if os.path.getctime(f) >= hour),
            "main_ctx": main_ctx, "main_turns_per_hour": mtph, "waste_share": share,
            "tokens_no_land": tnl if stale_land else 0}


def rows(stamp):
    b = budget()
    m = measure(b)
    out, reds = [], []
    for k in INFO:
        out.append((stamp, "burn", k, str(m[k]), "-", "info"))
    for k in WASTE:
        red = m[k] > b[k]
        out.append((stamp, "burn", k, str(m[k]), str(b[k]), "red" if red else "green"))
        if red:
            reds.append(f"{k} {m[k]:,} over {b[k]:,}")
    stop = os.path.join(FLEET, "BURN-STOP")
    if reds:
        with open(stop, "w", encoding="utf-8", newline="\n") as f:
            f.write(stamp + " " + "; ".join(reds) + "\n")
    elif os.path.exists(stop):
        os.remove(stop)
    return out


if __name__ == "__main__":
    for r in rows(time.strftime("%Y-%m-%d %H:%M")):
        print("\t".join(r))
