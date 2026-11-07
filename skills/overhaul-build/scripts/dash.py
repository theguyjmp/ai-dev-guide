"""dash.py: write <repo>/dash/index.html, the static, self-contained overhaul v2 dashboard (light and dark).
Shows the outcome line (proven N/T, rate, verdict, tokens per proven function when the log carries tokens), the
latest verdict from .overhaul/verdict.json with its reds and checks, every function (id, module, status, attempts,
last reason) in plan order, and the last 30 events from .overhaul/log.jsonl. Read only: run the verdict first for a
fresh verdict. The page reloads itself every 60 s, so a browser left open follows each rewrite.
  py -3 dash.py --repo R
"""
import argparse
import html
import os
import re
import sys

import verdict as vd

EVENTS_SHOWN = 30

CSS = """
:root{--bg:#f6f6f4;--card:#fff;--ink:#1c1c21;--mute:#6a6a74;--line:#e3e3e8;--chip:#efeff2;
--good:#1a7f37;--bad:#c62828;--info:#0b62c4;--bar:#1a7f37}
@media (prefers-color-scheme:dark){:root{--bg:#121215;--card:#1b1b20;--ink:#ececf1;--mute:#a0a0ab;--line:#2d2d35;
--chip:#26262d;--good:#4ac26b;--bad:#ff6b6b;--info:#6cb6ff;--bar:#3fb862}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1120px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:20px;margin:0}
h2{font-size:12px;margin:28px 0 8px;color:var(--mute);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.sub{color:var(--mute);margin:2px 0 0;font-size:13px}
.outcome{font-size:17px;font-weight:600;margin:18px 0 10px}
.bar{height:8px;border-radius:99px;background:var(--chip);overflow:hidden;margin:0 0 10px}
.bar i{display:block;height:100%;background:var(--bar)}
.tiles{display:flex;flex-wrap:wrap;gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:130px;flex:1}
.tile b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.tile span{color:var(--mute);font-size:12px}
.chips{margin:10px 0 0;display:flex;flex-wrap:wrap;gap:6px}
.badge{display:inline-block;padding:1px 10px;border-radius:99px;font-weight:700;font-size:12px;color:#fff;
background:var(--mute)}
.badge.GOOD{background:var(--good)}.badge.BAD{background:var(--bad)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow-x:auto}
.pad{padding:12px 14px}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:7px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--mute);font-weight:600;white-space:nowrap}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12.5px}
.nowrap{white-space:nowrap}
.st{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;background:var(--chip);white-space:nowrap}
.st-proven{color:var(--good)}.st-needs-plan{color:var(--bad)}.st-building,.st-review,.st-landing{color:var(--info)}
.line{margin:0 0 8px;word-break:break-word}
ol{margin:0;padding-left:20px}li{margin:4px 0}
.act,.empty{color:var(--mute)}
.ok{color:var(--good);font-weight:600}.red{color:var(--bad);font-weight:600}
"""


def esc(v):
    return html.escape("" if v is None else str(v))


def when(t):
    """Local 12-hour time with a short zone label, e.g. Nov 7 9:45 PM CST."""
    if not t:
        return ""
    lt = t.astimezone()
    z = lt.strftime("%Z")
    z = "".join(w[0] for w in z.split()) if " " in z else z
    return f"{lt:%b} {lt.day} {lt.hour % 12 or 12}:{lt:%M} {'AM' if lt.hour < 12 else 'PM'} {z}".strip()


def fmt_tokens(x):
    return f"{x / 1e6:.1f}M" if x >= 1e6 else f"{x / 1e3:.1f}k" if x >= 1e3 else str(round(x))


def status_chip(status):
    return f'<span class="st st-{re.sub(r"[^a-z-]", "", status)}">{esc(status)}</span>'


def table(head, rows, empty):
    th = "".join(f'<th class="{c}">{esc(h)}</th>' if c else f"<th>{esc(h)}</th>" for h, c in head)
    body = "".join(f"<tr>{r}</tr>" for r in rows) or f'<tr><td colspan="{len(head)}" class="empty">{esc(empty)}</td></tr>'
    return f'<div class="card"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def verdict_html(v):
    if not v.get("line"):
        return '<div class="card pad empty">No verdict yet: run the verdict script with --repo R.</div>'
    reds = "".join(f'<li>{esc(r.get("what"))} <span class="act">&rarr; {esc(r.get("action"))}</span></li>'
                   for r in v.get("reds") or [] if isinstance(r, dict))
    more = vd.as_int(v.get("reds_total")) - len(v.get("reds") or [])
    if more > 0:
        reds += f'<li class="act">+{more} more</li>'
    t = vd.parse_t(v.get("t"))
    out = (f'<div class="card pad"><p class="line mono">{esc(v["line"])}</p>'
           f'<p class="sub">verdict at {esc(when(t))}</p>' + (f"<ol>{reds}</ol>" if reds else "") + "</div>")
    checks = v.get("checks")
    if isinstance(checks, dict) and checks:
        rows = [f'<td class="mono">{esc(n)}</td><td class="{"ok" if c.get("ok") else "red"}">'
                f'{"pass" if c.get("ok") else "red"}</td><td class="num">{esc(c.get("exit"))}</td>'
                f'<td class="num">{esc(c.get("seconds"))}</td><td class="mono">{esc(c.get("tail"))}</td>'
                for n, c in checks.items() if isinstance(c, dict)]
        out += "<h2>Checks on main</h2>" + table(
            [("check", ""), ("result", ""), ("exit", "num"), ("s", "num"), ("output tail", "")], rows, "")
    elif checks == "skipped":
        out += '<p class="sub">Checks were skipped (--no-checks).</p>'
    return out


def render(p, events, v, t_now):
    view, proven, total = p["view"], p["proven"], p["total"]
    rate = vd.rate_per_h(p["state"].get("proven_history"), proven, t_now)
    tokens = sum(n for n in (vd.as_int(e.get("tokens")) for e in events) if n > 0)
    per = tokens / proven if tokens and proven else None
    verdict = v.get("verdict") if v.get("verdict") in ("GOOD", "BAD") else "none"
    pct = round(100 * proven / total) if total else 0
    outcome = f"proven {proven}/{total} · {rate:.1f}/h · verdict {verdict}"
    if per is not None:
        outcome += f" · {fmt_tokens(per)} tokens per proven function"
    tiles = [(f"{proven}/{total}", f"proven ({pct}%)"), (f"{rate:.1f}/h", "proven per hour, last 3 h"),
             (f'<span class="badge {verdict}">{verdict}</span>', esc(when(vd.parse_t(v.get("t")))) or "no verdict yet")]
    if per is not None:
        tiles.append((fmt_tokens(per), f"tokens per proven function ({fmt_tokens(tokens)} logged)"))
    if p["state"].get("cycle") is not None:
        tiles.append((esc(p["state"].get("cycle")), "cycle"))
    counts = {}
    for f in view:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    order = [s for s in vd.STATUSES if s in counts] + sorted(s for s in counts if s not in vd.STATUSES)
    chips = "".join(f"{status_chip(s)} <span class='act'>{counts[s]}</span> " for s in order)
    fn_rows = [f'<td class="mono nowrap">{esc(f["id"])}</td><td>{esc(f["module"])}</td><td>{status_chip(f["status"])}'
               f'</td><td class="num">{f["attempts"]}</td><td>{esc(f["last_reason"])}</td>' for f in view]
    ev_rows = []
    for e in reversed(events[-EVENTS_SHOWN:]):
        t = vd.parse_t(e.get("t"))
        detail = str(e.get("detail") or "")
        ev_rows.append(
            f'<td class="nowrap">{esc(when(t) if t else e.get("t"))}</td><td class="mono nowrap">{esc(e.get("fn"))}</td>'
            f'<td class="nowrap">{esc(e.get("event"))}</td><td title="{esc(detail)}">{esc(vd.short(detail, 160))}</td>'
            f'<td class="num">{esc(e.get("seconds"))}</td><td class="num">{esc(e.get("tokens"))}</td>')
    name = os.path.basename(p["repo"].rstrip("/\\")) or p["repo"]
    tile_html = "".join(f'<div class="tile"><b>{b}</b><span>{s}</span></div>' for b, s in tiles)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60"><title>Overhaul {esc(name)}</title><style>{CSS}</style></head>
<body><main>
<h1>Overhaul: {esc(name)}</h1>
<p class="sub">Dashboard written {esc(when(t_now))}; reloads every 60 s.</p>
<p class="outcome">{esc(outcome)}</p>
<div class="bar"><i style="width:{pct}%"></i></div>
<div class="tiles">{tile_html}</div>
<div class="chips">{chips}</div>
<h2>Verdict</h2>
{verdict_html(v)}
<h2>Functions ({total})</h2>
{table([("id", ""), ("module", ""), ("status", ""), ("attempts", "num"), ("last reason", "")], fn_rows,
       "No functions yet: overhaul-plan writes spec/FUNCTIONS.md.")}
<h2>Last {EVENTS_SHOWN} events, newest first</h2>
{table([("time", ""), ("fn", ""), ("event", ""), ("detail", ""), ("s", "num"), ("tokens", "num")], ev_rows,
       "No events yet.")}
</main></body></html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="write the overhaul v2 dashboard")
    ap.add_argument("--repo", required=True, help="the project repo")
    a = ap.parse_args(argv)
    repo = os.path.abspath(a.repo)
    if not os.path.isdir(repo):
        print(f"dash: no repo at {repo}", file=sys.stderr)
        return 2
    p = vd.load_project(repo)
    v = vd.load_json(p["verdict"], {})
    page = render(p, vd.read_log(p["log"]), v if isinstance(v, dict) else {}, vd.now())
    out = os.path.join(repo, "dash", "index.html")
    vd.write_atomic(out, page)
    vd.say(f"dash: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
