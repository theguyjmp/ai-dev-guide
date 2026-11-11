import json, subprocess, time, html, os, datetime
H = os.path.dirname(os.path.abspath(__file__))
TOTAL, BASE, BAR = 425, 79, 10


def jl(p):
    out = []
    try:
        for l in open(p, encoding='utf-8'):
            l = l.strip()
            if l:
                try:
                    out.append(json.loads(l))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def jf(p, d):
    try:
        return json.load(open(p, encoding='utf-8'))
    except Exception:
        return d


ev = jl(os.path.join(H, 'state', 'events.jsonl'))
claims = jf(os.path.join(H, 'state', 'claims.json'), {})
queue = jf(os.path.join(H, 'state', 'queue.json'), [])
st = {}
try:
    r = subprocess.run(['py', '-3', os.path.join(H, 'harness.py'), 'status'], capture_output=True, text=True, timeout=60, cwd=H)
    st = json.loads(r.stdout[r.stdout.find('{'):])
except Exception:
    pass
now = time.time()
lands = [e for e in ev if e.get('event') == 'land']
rej = [e for e in ev if e.get('event') == 'reject']
nrows = lambda e: len(e.get('rows', []))
l1 = st.get('landed_1h', sum(nrows(e) for e in lands if e['ts'] > now - 3600))
l3 = st.get('landed_3h', sum(nrows(e) for e in lands if e['ts'] > now - 10800))
r1 = st.get('rejects_1h', sum(1 for e in rej if e['ts'] > now - 3600))
cum = BASE + len({r for e in lands for r in e.get('rows', [])})
proven = st.get('proven', cum)
busy = st.get('claimed', len(claims))
ql = st.get('queued', len(queue))
ok = l1 >= BAR

# cumulative chart
pts = [(ev[0]['ts'] if ev else now, BASE)]
c = BASE
seen = set()
for e in lands:
    seen |= set(e.get('rows', []))
    c = BASE + len(seen)
    pts.append((e['ts'], c))
pts.append((now, c))
W, Ht, L, R, T, B = 640, 220, 44, 14, 12, 28
t0, t1 = pts[0][0], max(pts[-1][0], pts[0][0] + 1)
ymax = max(100, ((c // 50) + 1) * 50)
X = lambda t: L + (t - t0) / (t1 - t0) * (W - L - R)
Y = lambda v: Ht - B - v / ymax * (Ht - B - T)
d = ' '.join(('M' if i == 0 else 'L') + '%.1f,%.1f' % (X(t), Y(v)) for i, (t, v) in enumerate(pts))
area = d + ' L%.1f,%.1f L%.1f,%.1fZ' % (X(t1), Y(0), X(t0), Y(0))
fmt = lambda t: datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%H:%M')
step = 50 if ymax <= 250 else 100
grid = ''.join('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="gl"/><text x="%d" y="%.1f" text-anchor="end" class="ax">%d</text>' % (L, W - R, Y(v), Y(v), L - 6, Y(v) + 4, v) for v in range(0, int(ymax) + 1, step))
xl = ''.join('<text x="%.1f" y="%d" text-anchor="middle" class="ax">%s</text>' % (X(t0 + (t1 - t0) * f), Ht - 8, fmt(t0 + (t1 - t0) * f)) for f in (0, .5, 1))
svg = '<svg viewBox="0 0 %d %d" role="img" aria-label="Cumulative proven functions">%s%s<path d="%s" class="ar"/><path d="%s" class="ln"/><circle cx="%.1f" cy="%.1f" r="4" class="dot"/></svg>' % (W, Ht, grid, xl, area, d, X(t1), Y(c))

# last train gate
ts_ = [e for e in ev if e.get('event') == 'train_start']
since = ts_[-1]['ts'] if ts_ else 0
stages = [e for e in ev if e.get('event') == 'gate_stage' and e['ts'] >= since]
mx = max([s.get('secs', 0) for s in stages] or [1]) or 1
gate = ''.join('<div class="gr"><span>%s</span><i class="%s" style="width:%.0f%%"></i><b>%ss</b></div>' % (html.escape(str(s['stage'])), 'bad' if s.get('rc') else 'good', max(2, s.get('secs', 0) / mx * 100), s.get('secs', 0)) for s in stages) or '<p class="mut">no stages yet</p>'

# table
rows = []
for e in lands + rej:
    if e['event'] == 'land':
        rs = e.get('rows', [])
        rows.append((e['ts'], 'land', '%d rows' % len(rs), ', '.join(rs[:4]) + ('...' if len(rs) > 4 else '')))
    else:
        rows.append((e['ts'], 'reject', e.get('row', ''), e.get('reason', '').split('::')[0][:90]))
rows.sort(reverse=True)
tb = ''.join('<tr><td>%s</td><td><span class="pill %s">%s</span></td><td>%s</td><td class="det">%s</td></tr>' % (fmt(t), k, k, html.escape(a), html.escape(b)) for t, k, a, b in rows[:10])
pct = proven / TOTAL * 100
bigc = '--good' if ok else '--bad'
rc = '--bad' if r1 else '--fg'
upd = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M')

CSS = """
/* layout: hero rate, progress, stat row, chart, gate bars, event table */
:root{--bg:#f4f6f8;--card:#fff;--fg:#14202b;--mut:#5d6b78;--line:#dbe2e8;--good:#14804a;--bad:#c23a2b;--accent:#1f5fbf;--accent2:#d7e4f7}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e1419;--card:#161e26;--fg:#e6edf3;--mut:#8b9aa8;--line:#26323d;--good:#3ecf84;--bad:#ff7a6b;--accent:#6aa4ff;--accent2:#1d2e47;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0e1419;--card:#161e26;--fg:#e6edf3;--mut:#8b9aa8;--line:#26323d;--good:#3ecf84;--bad:#ff7a6b;--accent:#6aa4ff;--accent2:#1d2e47;color-scheme:dark}
body{background:var(--bg);color:var(--fg);font-family:Inter,system-ui,sans-serif;padding-block:20px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));font-variant-numeric:tabular-nums}
main{max-width:760px;margin:0 auto;display:grid;gap:14px}
h1{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin:0;font-weight:600}
h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin:0 0 10px;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;min-width:0}
.hero{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.big{font-size:72px;line-height:1;font-weight:700}
.unit{color:var(--mut);font-size:15px}
.bar{height:10px;background:var(--accent2);border-radius:5px;overflow:hidden;margin-top:10px}
.bar i{display:block;height:100%;background:var(--accent)}
.row{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;font-size:14px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:14px}
.n{font-size:32px;font-weight:700}.lbl{font-size:12px;color:var(--mut)}
svg{width:100%;height:auto;display:block}.gl{stroke:var(--line);stroke-width:1}.ax{fill:var(--mut);font-size:11px}
.ln{fill:none;stroke:var(--accent);stroke-width:2.5;stroke-linejoin:round}.ar{fill:var(--accent);opacity:.12}.dot{fill:var(--accent)}
.gr{display:grid;grid-template-columns:70px 1fr 46px;align-items:center;gap:8px;font-size:13px;margin-bottom:6px}.gr i{height:8px;border-radius:4px;display:block}.gr b{text-align:right;font-weight:600}
i.good{background:var(--good)}i.bad{background:var(--bad)}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}td{padding:6px 8px 6px 0;border-top:1px solid var(--line);white-space:nowrap}.det{color:var(--mut);white-space:normal;min-width:160px}
.pill{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600}
.pill.land{color:var(--good);background:color-mix(in srgb,var(--good) 15%,transparent)}
.pill.reject{color:var(--bad);background:color-mix(in srgb,var(--bad) 15%,transparent)}
.mut{color:var(--mut);font-size:12px;margin:0}
"""

BODY = """<main>
<h1>Fleet</h1>
<section class="card"><h2>Functions landed, last hour</h2>
<div class="hero"><span class="big" style="color:var(%(bigc)s)">%(l1)d</span><span class="unit">/ h &nbsp;bar %(BAR)d/h &nbsp;·&nbsp; %(l3)d in 3 h</span></div></section>
<section class="card"><h2>Proven</h2><div class="row"><b>%(proven)d / %(TOTAL)d</b><span>%(pct).1f%%</span></div><div class="bar"><i style="width:%(pct).1f%%"></i></div>
<p class="mut" style="margin-top:8px">old harness: %(BASE)d in ~64 h (1.2/h)</p></section>
<section class="stats">
<div class="card"><div class="n">%(busy)d</div><div class="lbl">builders busy</div></div>
<div class="card"><div class="n">%(ql)d</div><div class="lbl">queued</div></div>
<div class="card"><div class="n" style="color:var(%(rc)s)">%(r1)d</div><div class="lbl">rejects, last hour</div></div></section>
<section class="card"><h2>Cumulative proven (UTC)</h2>%(svg)s</section>
<section class="card"><h2>Last train gate</h2>%(gate)s</section>
<section class="card"><h2>Last 10 lands / rejects</h2><div class="wrap"><table>%(tb)s</table></div></section>
<p class="mut">updated %(upd)s UTC</p>
</main>"""

page = '<title>Fleet</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n<style>' + CSS + '</style>\n' + BODY % dict(
    bigc=bigc, l1=l1, BAR=BAR, l3=l3, proven=proven, TOTAL=TOTAL, pct=pct, BASE=BASE, busy=busy, ql=ql, rc=rc, r1=r1, svg=svg, gate=gate, tb=tb, upd=upd)
os.makedirs(os.path.join(H, 'dash'), exist_ok=True)
open(os.path.join(H, 'dash', 'index.html'), 'w', encoding='utf-8').write(page)
print('ok', l1, proven, busy, ql, r1)
