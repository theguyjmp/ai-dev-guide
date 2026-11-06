"""Hourly history for the dashboard: writes FLEET/dash/history.json, one row per UTC hour from hour 0.

Installed as FLEET/bin/dashhistory.py and run right after dashdata.py on every heartbeat. It rebuilds every series
from the source logs on each run, never appends a snapshot, so a missed run or a stopped scheduler leaves no gap:
  merged_in_hour, merged_total   first-parent commits on origin/main (git log of the repo)
  agents_running, agents_active,  the hour's highest `fleet` / `burn` row in FLEET/HEARTBEAT.tsv
  merge_waiting, tokens_per_hour, agents_per_hour
  tokens                         all usage (input + cache read + cache write + output) in the transcripts that hour
  functions_proven, phase        `functions_proven` / `phase` heartbeat rows, and the current hour from
                                 FLEET/dash/status.json; carried forward from the last known value, left out before it
A value never measured is left out, never zero-filled. Reads only; never changes a threshold.
Paths come from FLEET/BUDGET.json, as burn.py reads them:
  {"repo": "<repo path>", "transcripts": "<~/.claude/projects/<slug>>", "since": "<YYYY-MM-DD, optional hour 0>"}
"""
import collections, datetime as dt, glob, json, os, subprocess

FLEET = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HB_KEYS = ("agents_running", "agents_active", "merge_waiting", "tokens_per_hour", "agents_per_hour")
USAGE = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")


def budget():
    try:
        with open(os.path.join(FLEET, "BUDGET.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def hour_utc(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:00Z")


def merges(repo, since):
    if not repo:
        return collections.Counter()
    out = subprocess.run(["git", "-C", repo, "log", "origin/main", "--first-parent", "--since=" + since.isoformat(),
                          "--format=%cI"], capture_output=True, text=True).stdout.split()
    return collections.Counter(hour_utc(dt.datetime.fromisoformat(x)) for x in out)


def heartbeat(since):
    """Per hour: the highest value of each heartbeat metric, plus the last phase / functions_proven seen."""
    peak = collections.defaultdict(lambda: collections.defaultdict(list))
    last = collections.defaultdict(dict)
    local = dt.datetime.now().astimezone().tzinfo
    try:
        f = open(os.path.join(FLEET, "HEARTBEAT.tsv"), encoding="utf-8")
    except OSError:
        return peak, last
    with f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) < 4:
                continue
            try:
                t = dt.datetime.strptime(p[0], "%Y-%m-%d %H:%M").replace(tzinfo=local)
            except ValueError:
                continue
            if t < since:
                continue
            h, metric, value = hour_utc(t), p[2], p[3]
            if metric == "phase":
                last[h]["phase"] = value
                continue
            try:
                v = float(value)
            except ValueError:
                continue
            if metric == "functions_proven":
                last[h]["functions_proven"] = v
            elif p[1] in ("fleet", "burn") and metric in HB_KEYS:
                peak[h][metric].append(v)
    return peak, last


def tokens(folder, since):
    tok, seen = collections.Counter(), set()
    if not folder:
        return tok
    folder = os.path.expanduser(folder)
    for path in glob.glob(os.path.join(folder, "**", "*.jsonl"), recursive=True):
        try:
            if dt.datetime.fromtimestamp(os.path.getmtime(path), dt.timezone.utc) < since:
                continue
            f = open(path, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                msg = j.get("message") or {}
                u, ts = msg.get("usage"), j.get("timestamp")
                if not u or not ts:
                    continue
                key = (j.get("requestId"), msg.get("id"))
                if key != (None, None):
                    if key in seen:
                        continue
                    seen.add(key)
                t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if t >= since:
                    tok[hour_utc(t)] += sum(u.get(k, 0) or 0 for k in USAGE)
    return tok


def current_status():
    try:
        with open(os.path.join(FLEET, "dash", "status.json"), encoding="utf-8") as f:
            s = json.load(f)
        return s[0] if isinstance(s, list) and s else {}
    except (OSError, ValueError):
        return {}


def main():
    b = budget()
    now = dt.datetime.now(dt.timezone.utc)
    try:
        since = dt.datetime.fromisoformat(b["since"]).replace(tzinfo=dt.timezone.utc)
    except (KeyError, ValueError, TypeError):
        since = now - dt.timedelta(days=14)
    m = merges(b.get("repo", ""), since)
    peak, last = heartbeat(since)
    tok = tokens(b.get("transcripts", ""), since)
    st = current_status()
    if st.get("proven") is not None:
        last[hour_utc(now)].setdefault("functions_proven", st["proven"])
    if st.get("phase"):
        last[hour_utc(now)].setdefault("phase", st["phase"])
    hours = set(m) | set(peak) | set(tok) | set(last)
    if not hours:
        res = []
    else:
        t = dt.datetime.strptime(min(hours), "%Y-%m-%dT%H:00Z").replace(tzinfo=dt.timezone.utc)
        end = dt.datetime.strptime(max(hours), "%Y-%m-%dT%H:00Z").replace(tzinfo=dt.timezone.utc)
        res, cum, carry = [], 0, {}
        while t <= end:
            h = hour_utc(t)
            cum += m.get(h, 0)
            carry.update(last.get(h, {}))
            r = {"hour": h, "merged_in_hour": m.get(h, 0), "merged_total": cum}
            r.update(carry)
            r.update({k: max(v) for k, v in peak.get(h, {}).items() if v})
            if h in tok:
                r["tokens"] = tok[h]
            res.append(r)
            t += dt.timedelta(hours=1)
    os.makedirs(os.path.join(FLEET, "dash"), exist_ok=True)
    out = os.path.join(FLEET, "dash", "history.json")
    with open(out + ".tmp", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    os.replace(out + ".tmp", out)
    print(len(res), "hours", (res[0]["hour"] + " -> " + res[-1]["hour"]) if res else "", "merged", res[-1]["merged_total"] if res else 0)


if __name__ == "__main__":
    main()
