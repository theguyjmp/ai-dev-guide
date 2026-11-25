"""devping: one Discord card per project, edited in place; owner-important alerts show on that card.

The tool lives in the skill folder. Everything the user owns (config.json with the webhook, state/ with card ids
and alert history) lives in the data dir ~/.claude/devping (or $DEVPING_HOME), because skill updates replace the
skill folder whole. One scheduled job runs `tick` every 5 minutes for every project; `setup` prints the command.

  devping.py setup [--webhook URL] [--bot-token T --channel-id C]
  devping.py add NAME --dir PATH [--stats-cmd CMD] [--publish-cmd CMD] [--webhook URL] [--chat-dir DIR] [--idle-min 20]
  devping.py start NAME | stop NAME          begin / end a dev cycle (stopped: grey card, no alerts)
  devping.py alert NAME "text" [--key K] [--cooldown MIN]
  devping.py note NAME "text"                quiet line on the card ("" clears it)
  devping.py tick [NAME]                     refresh cards, record alerts
  devping.py list | remove NAME
  --dry (or --dry-run) on start, stop, alert, note and tick prints each Discord payload as one JSON line instead of
  sending it, and saves nothing.

Stats adapter contract (optional, per project): CMD prints one JSON object
  {"health": "green|amber|red", "headline": "...", "progress": [done, total],
   "fields": [{"name": "...", "value": "...", "inline": true, "short": "..."}], "links": [{"name": "...", "url": "..."}],
   "tiles": [["name", "value"]], "alerts": [{"key": "...", "text": "...", "cooldown_min": 120}], "quiet_ok": false}
Every key is optional and unknown keys are ignored. "fields" render as a compact table plus one line of Discord
times; "tiles", when given, replace them with one full-width Discord field each. "quiet_ok": true lets the chat sit
idle without the idle alarm; it is ignored while health is red. Output outside the contract shows as a red card.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

SCRIPT = Path(__file__).resolve()
CLAUDE_HOME = Path.home() / ".claude"
UA = "DiscordBot (https://localhost/devping, 1.0)"  # Cloudflare refuses urllib's default agent
MAX_ALERTS_PER_HOUR = 4
GRACE_S = 15 * 60
BURY = 3
REPOST_IDLE_S = 30 * 60
LAST_ALERT_SHOWN_S = 6 * 3600
ADAPTER_TIMEOUT_S = 180
STALE_LOCK_S = 300
LOCK_WAIT_S = 240
LOG_MAX_BYTES = 256 * 1024
SUPPRESS_NOTIFICATIONS = 4096
HEALTH = ("green", "amber", "red")
COLORS = {"green": 0x2ECC71, "amber": 0xF1C40F, "red": 0xE74C3C, "grey": 0x95A5A6}
ICONS = {"green": "🟢", "amber": "🟡", "red": "🔴", "grey": "⏸️"}
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
LAST_POST = "last_post.json"
DRY = False


def data_dir() -> Path:
    """The user's devping data (config.json, state/): never inside the skill folder."""
    return Path(os.environ.get("DEVPING_HOME") or CLAUDE_HOME / "devping")


def cfg_path() -> Path:
    return data_dir() / "config.json"


def state_dir() -> Path:
    return data_dir() / "state"


def safe(name: str) -> str:
    """The file stem of a project's state and lock files."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def jload(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def jsave(p: Path, d) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def log(msg: str) -> None:
    """stderr when there is one (pythonw has none), and always state/devping.log."""
    line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg)
    if sys.stderr:
        print(line, file=sys.stderr)
    path = state_dir() / "devping.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.write_text(path.read_text(encoding="utf-8", errors="replace")[-LOG_MAX_BYTES // 2:], encoding="utf-8")
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- the stats contract
class ContractError(ValueError):
    """The stats adapter printed something outside the contract."""


@dataclass(frozen=True)
class Field:
    name: str
    value: str
    inline: bool = True
    short: str = ""


@dataclass(frozen=True)
class Link:
    name: str
    url: str


@dataclass(frozen=True)
class Alert:
    key: str
    text: str
    cooldown_min: float = 120.0


def _items(d: dict, key: str, kind: type = dict) -> list:
    v = d.get(key) or []
    if not isinstance(v, list) or not all(isinstance(x, kind) for x in v):
        raise ContractError("%s must be a list of %s" % (key, "objects" if kind is dict else "lists"))
    return v


def _text(v: object, what: str, required: bool = False) -> str:
    if v is None and not required:
        return ""
    if isinstance(v, bool) or not isinstance(v, (str, int, float)) or (required and not str(v).strip()):
        raise ContractError("%s must be %stext, got %r" % (what, "non-empty " if required else "", v))
    return str(v)


def _flag(d: dict, key: str, default: bool) -> bool:
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise ContractError("%s must be true or false, got %r" % (key, v))
    return v


def _number(d: dict, key: str, default: float) -> float:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise ContractError("%s must be a number >= 0, got %r" % (key, v))
    return float(v)


def _tile(t: list) -> tuple[str, str]:
    if len(t) != 2:
        raise ContractError("a tile must be [name, value], got %r" % (t,))
    return _text(t[0], "tile name", True), _text(t[1], "tile value")


@dataclass(frozen=True)
class Stats:
    health: str = "green"
    headline: str = ""
    progress: tuple[int, int] | None = None
    fields: tuple[Field, ...] = ()
    tiles: tuple[tuple[str, str], ...] = ()
    links: tuple[Link, ...] = ()
    alerts: tuple[Alert, ...] = ()
    quiet_ok: bool = False

    @classmethod
    def parse(cls, d: object) -> Stats:
        if not isinstance(d, dict):
            raise ContractError("not a JSON object")
        health = d.get("health", "green")
        if health not in HEALTH:
            raise ContractError("health must be one of %s, got %r" % ("/".join(HEALTH), health))
        prog = d.get("progress")
        if prog is not None:
            ok = isinstance(prog, list) and len(prog) == 2 and all(type(n) is int and n >= 0 for n in prog)
            if not ok or prog[0] > prog[1]:
                raise ContractError("progress must be [done, total] with 0 <= done <= total, got %r" % (prog,))
            prog = (prog[0], prog[1])
        return cls(
            health=health, headline=_text(d.get("headline"), "headline"), progress=prog,
            fields=tuple(Field(_text(f.get("name"), "field name", True), _text(f.get("value"), "field value"),
                               _flag(f, "inline", True), _text(f.get("short"), "field short"))
                         for f in _items(d, "fields")),
            tiles=tuple(_tile(t) for t in _items(d, "tiles", list)),
            links=tuple(Link(_text(x.get("name"), "link name", True), _text(x.get("url"), "link url", True))
                        for x in _items(d, "links")),
            alerts=tuple(Alert(_text(x.get("key"), "alert key", True), _text(x.get("text"), "alert text", True),
                               _number(x, "cooldown_min", 120.0)) for x in _items(d, "alerts")),
            quiet_ok=_flag(d, "quiet_ok", False))

    @classmethod
    def failed(cls, reason: str) -> Stats:
        return cls(health="red", headline="Stats adapter failed: %s" % reason.strip()[:80])


# ---------------------------------------------------------------- config and state
def known(cls: type, d: dict) -> dict:
    """The keys of `d` that dataclass `cls` models."""
    names = {f.name for f in dataclasses.fields(cls)} - {"raw"}
    return {k: v for k, v in d.items() if k in names}


def record(obj: object, keep_empty: tuple[str, ...] = ()) -> dict:
    """A dataclass as its JSON dict, on top of the dict it was loaded from: keys this version does not model stay as
    they are, and an empty value is written only when the key was there before or is in keep_empty."""
    out = dict(obj.raw)
    for f in dataclasses.fields(obj):
        v = getattr(obj, f.name)
        if f.name != "raw" and (v or f.name in out or f.name in keep_empty):
            out[f.name] = v
    return out


@dataclass
class Project:
    name: str
    dir: str
    chat_dir: str
    idle_min: float = 20.0
    active: bool = True
    cycle_ts: float = 0.0
    stats_cmd: str = ""
    publish_cmd: str = ""
    webhook: str = ""
    raw: dict = field(default_factory=dict)
    REQUIRED: ClassVar = ("name", "dir", "chat_dir", "idle_min", "active", "cycle_ts")

    @classmethod
    def from_dict(cls, d: dict) -> Project:
        return cls(**known(cls, d), raw=d)

    def to_dict(self) -> dict:
        return record(self, self.REQUIRED)


@dataclass
class Config:
    webhook: str = ""
    bot_token: str = ""
    channel_id: str = ""
    projects: dict[str, Project] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> Config:
        d = jload(cfg_path(), {})
        projects = {n: Project.from_dict(p) for n, p in (d.get("projects") or {}).items()}
        return cls(**{k: str(v or "") for k, v in known(cls, d).items() if k != "projects"}, projects=projects, raw=d)

    def save(self) -> None:
        d = record(self)
        d["projects"] = {n: p.to_dict() for n, p in self.projects.items()}
        jsave(cfg_path(), d)

    def hook(self, p: Project) -> str:
        return p.webhook or self.webhook


@dataclass
class CardState:
    dash_id: str = ""
    dash_url: str = ""
    dash_ts: float = 0.0
    dash_sig: str = ""
    dash_channel: str = ""
    stale: list = field(default_factory=list)   # [[message id, webhook]] of old cards still to delete
    keys: dict = field(default_factory=dict)    # alert key -> when it was last recorded
    sent: list = field(default_factory=list)    # [{ts, key, text}], newest last
    suppressed: int = 0
    note: str = ""
    raw: dict = field(default_factory=dict)

    @staticmethod
    def path(name: str) -> Path:
        return state_dir() / (safe(name) + ".json")

    @classmethod
    def load(cls, name: str) -> CardState:
        d = jload(cls.path(name), {})
        model = known(cls, d)
        for k in ("dash_id", "dash_url", "dash_sig", "dash_channel", "note"):
            if k in model:
                model[k] = str(model[k] or "")
        return cls(**model, raw=d)

    def save(self, name: str) -> None:
        jsave(self.path(name), record(self))


@contextlib.contextmanager
def locked(name: str, wait_s: float):
    """Yields True while this process holds the project's lock, False when another run held it for wait_s."""
    lock = state_dir() / (safe(name) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    end = time.time() + wait_s
    while True:
        try:
            if lock.exists() and time.time() - lock.stat().st_mtime > STALE_LOCK_S:
                lock.unlink(missing_ok=True)
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            if time.time() >= end:
                yield False
                return
            time.sleep(1)
    try:
        yield True
    finally:
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------- Discord
def post(url: str, payload: dict, method: str = "POST", path: str = "") -> dict:
    if DRY:
        print(json.dumps({"method": method, "path": path, "payload": payload}, ensure_ascii=False))
        return {"id": ""}
    base = url.split("?")[0]
    query = url[len(base):]
    full = base + path + (query + ("&" if query else "?") + "wait=true" if method == "POST" else query)
    req = urllib.request.Request(full, data=json.dumps(payload).encode(), method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read()
            out = json.loads(body) if body else {}
            if method == "POST" and out.get("id") and "embeds" not in payload:
                lp = jload(state_dir() / LAST_POST, {})
                lp[base] = out["id"]
                lp["recent:" + base] = (lp.get("recent:" + base, []) + [out["id"]])[-20:]
                jsave(state_dir() / LAST_POST, lp)
            return out
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(min(float(json.loads(e.read() or b"{}").get("retry_after", 2)), 10))
    raise RuntimeError("Discord rate limit")


def buried(cfg: Config, url: str, mid: str) -> bool:
    """True when at least BURY messages that are not devping cards sit below card `mid`. With bot_token and
    channel_id it asks Discord, so people talking count; without them only devping's own non-card posts count."""
    cards = {str(jload(f, {}).get("dash_id")) for f in state_dir().glob("*.json") if f.name != LAST_POST}
    if cfg.bot_token and cfg.channel_id and not DRY:
        try:
            req = urllib.request.Request(
                "https://discord.com/api/v10/channels/%s/messages?limit=20&after=%s" % (cfg.channel_id, mid),
                headers={"Authorization": "Bot " + cfg.bot_token, "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                msgs = json.loads(r.read() or b"[]")
            return sum(1 for m in msgs if str(m.get("id")) not in cards) >= BURY
        except (OSError, ValueError) as e:
            log("channel read failed, counting own posts only: %s" % e)
    recent = jload(state_dir() / LAST_POST, {}).get("recent:" + url.split("?")[0], [])
    return sum(1 for i in recent if str(i).isdigit() and mid.isdigit() and int(i) > int(mid)) >= BURY


def rel(ts: float) -> str:
    return "<t:%d:R>" % int(ts) if ts else "never"


def card(p: Project, st: CardState, s: Stats, chat_ts: float, now: float) -> dict:
    """The card embed. With tiles: progress bar, note and last alert, then one full-width field per tile."""
    health = s.health if p.active else "grey"
    head = s.headline or ("Cycle running" if p.active else "Cycle stopped")
    lines = []
    if s.progress and s.progress[1]:
        done, total = s.progress
        n = int(round(12 * done / total))
        lines.append("`%s` **%d/%d** · %d%%" % ("■" * n + "□" * (12 - n), done, total, round(100 * done / total)))
    plain, timed = [], []
    for f in ([] if s.tiles else s.fields[:12]):
        v = re.sub(r"\*\*|__", "", f.value).strip() or "—"
        (timed if "<t:" in v else plain).append((f.short or f.name, v))
    if plain:
        cells = ["%-8s %-9s" % (k[:8], v[:9]) for k, v in plain]
        lines.append("```\n%s\n```" % "\n".join(" ".join(cells[i:i + 2]).rstrip() for i in range(0, len(cells), 2)))
    timed.append(("chat", rel(chat_ts)))
    lines.append("-# " + " · ".join("%s %s" % (k.lower(), v) for k, v in timed))
    if s.links:
        lines.append("-# " + " · ".join("[%s](%s)" % (x.name, x.url) for x in s.links))
    if st.note:
        lines.append("📝 " + st.note[:200])
    last = st.sent[-1] if st.sent else None
    if last and now - last["ts"] < LAST_ALERT_SHOWN_S:
        lines.append("-# last alert %s: %s" % (rel(last["ts"]), last["text"][:80]))
    if st.suppressed:
        lines.append("-# %d alert(s) held back (cap %d/h)" % (st.suppressed, MAX_ALERTS_PER_HOUR))
    lines.append("-# last edited <t:%d:R>" % int(now))
    embed = {"title": ("%s %s — %s" % (ICONS[health], p.name, head))[:256], "color": COLORS[health]}
    if s.tiles:
        kept = lines[:1] + [x for x in lines[1:] if x.startswith(("-# last alert", "📝"))]
        embed["description"] = "\n".join(kept)[:4000]
        embed["fields"] = ([{"name": k[:256], "value": v[:1024] or "-", "inline": False} for k, v in s.tiles[:23]]
                           + [{"name": "Last edited", "value": "<t:%d:R>" % int(now), "inline": False}])
    else:
        embed["description"] = "\n".join(lines)[:4000]
    return {"content": "", "allowed_mentions": {"parse": []}, "embeds": [embed]}


def signature(e: dict) -> str:
    """What makes a card different, without Discord relative times, which move every tick."""
    return re.sub(r"<t:\d+(:\w)?>", "", "%s|%s|%s|%s" % (e.get("title"), e.get("description"), e.get("color"),
                                                          json.dumps(e.get("fields", []), sort_keys=True)))


def refresh(cfg: Config, p: Project, st: CardState, payload: dict, now: float) -> None:
    """Edit the card in place (never notifies); repost it, silently, only when it is buried or its webhook changed."""
    url = cfg.hook(p)
    sig = signature(payload["embeds"][0])
    changed, same_hook = st.dash_sig != sig, url == st.dash_url
    if st.dash_id and same_hook and not buried(cfg, url, st.dash_id):
        if not changed:
            return
        try:
            post(url, {"embeds": payload["embeds"], "allowed_mentions": {"parse": []}}, "PATCH",
                 "/messages/" + st.dash_id)
            st.dash_sig = sig
            return
        except urllib.error.HTTPError as err:
            if err.code != 404:
                raise
            st.dash_id = ""  # deleted by hand: post a new one
    elif st.dash_id and same_hook and not changed and now - st.dash_ts < REPOST_IDLE_S:
        return
    old = [st.dash_id, st.dash_url]
    st.dash_sig = sig
    res = post(url, dict(payload, flags=SUPPRESS_NOTIFICATIONS))
    st.dash_id, st.dash_url, st.dash_ts = str(res.get("id") or ""), url, now
    if res.get("channel_id"):
        st.dash_channel = str(res["channel_id"])
    if all(old) and old not in st.stale:
        st.stale.append(old)
    keep = []
    for sid, surl in st.stale:
        if sid == st.dash_id:
            continue
        try:
            post(surl, {}, "DELETE", "/messages/" + sid)
        except urllib.error.HTTPError as err:
            if err.code != 404:
                keep.append([sid, surl])
        except (OSError, RuntimeError):
            keep.append([sid, surl])
    st.stale = keep[-50:]


def send_alert(st: CardState, al: Alert, now: float) -> bool:
    """Record an alert for the card's 'last alert' line; False when its key is in cooldown or the hourly cap is hit."""
    if now - st.keys.get(al.key, 0) < al.cooldown_min * 60:
        return False
    if sum(1 for x in st.sent if now - x["ts"] < 3600) >= MAX_ALERTS_PER_HOUR:
        st.suppressed += 1
        return False
    st.keys[al.key] = now
    st.sent = (st.sent + [{"ts": now, "key": al.key, "text": al.text}])[-20:]
    return True


# ---------------------------------------------------------------- one tick
def chat_activity(chat_dir: str) -> float:
    """Newest write in the project's most recent Claude Code session (main transcript and its subagents)."""
    try:
        sessions = [e for e in os.scandir(chat_dir) if e.name.endswith(".jsonl")]
    except OSError:
        return 0.0
    if not sessions:
        return 0.0
    newest = max(sessions, key=lambda e: e.stat().st_mtime)
    best = newest.stat().st_mtime
    for root, _, files in os.walk(os.path.join(chat_dir, newest.name[:-len(".jsonl")])):
        for f in files:
            with contextlib.suppress(OSError):
                best = max(best, os.path.getmtime(os.path.join(root, f)))
    return best


def collect(p: Project) -> Stats:
    if not p.stats_cmd:
        return Stats()
    try:
        r = subprocess.run(p.stats_cmd, shell=True, cwd=p.dir, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=ADAPTER_TIMEOUT_S, stdin=subprocess.DEVNULL,
                           creationflags=NO_WINDOW)
        start = r.stdout.find("{")
        if start < 0:
            raise ContractError("no JSON on stdout (exit %d) %s" % (r.returncode, r.stderr.strip()[-60:]))
        return Stats.parse(json.JSONDecoder().raw_decode(r.stdout[start:])[0])
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return Stats.failed(str(e))


def tick(cfg: Config, p: Project, st: CardState, now: float) -> None:
    s = collect(p) if p.active else Stats()
    chat_ts = chat_activity(p.chat_dir)
    if p.active and now - p.cycle_ts > GRACE_S:
        for al in s.alerts:
            send_alert(st, al, now)
        quiet_ok = s.quiet_ok and s.health != "red"
        if not quiet_ok and chat_ts and now - chat_ts > p.idle_min * 60:
            send_alert(st, Alert("chat-idle", "the project chat has been quiet for %d min: it has stopped or is "
                                              "waiting on you." % ((now - chat_ts) // 60)), now)
    refresh(cfg, p, st, card(p, st, s, chat_ts, now), now)
    if p.publish_cmd and not DRY:
        subprocess.Popen(p.publish_cmd, shell=True, cwd=p.dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    if not any(now - x["ts"] < 3600 for x in st.sent):
        st.suppressed = 0


def run_one(cfg: Config, p: Project, a: argparse.Namespace) -> bool:
    with locked(p.name, 0 if a.cmd == "tick" else LOCK_WAIT_S) as got:
        if not got:
            log("%s: another devping run holds its lock" % p.name)
            return a.cmd == "tick"
        st = CardState.load(p.name)
        now = time.time()
        try:
            if a.cmd == "start":
                st.keys = {}
            elif a.cmd == "note":
                st.note = a.text.strip()
            elif a.cmd == "alert" and not send_alert(st, Alert(a.key or "chat:" + a.text[:40], a.text, a.cooldown),
                                                     now):
                print("held back: same key within its cooldown, or the hourly cap")
            tick(cfg, p, st, now)
            return True
        except Exception as e:  # one project's failure must not stop the others; the log keeps it
            log("%s: %s: %s" % (p.name, type(e).__name__, e))
            return False
        finally:
            if not DRY:
                st.save(p.name)


# ---------------------------------------------------------------- commands
def slug(path: Path) -> str:
    """Claude Code's transcript folder name for a working directory."""
    return re.sub(r"[^A-Za-z0-9-]", "-", str(path).rstrip("\\/"))


def schedule_help() -> str:
    if os.name == "nt":
        exe = Path(sys.executable).with_name("pythonw.exe")
        out = ("Schedule tick every 5 min, windowless (paste into Command Prompt, not PowerShell):\n"
               'schtasks /Create /F /TN devping /SC MINUTE /MO 5 /TR "\\"%s\\" \\"%s\\" tick"' % (exe, SCRIPT))
        if not exe.exists():
            out += "\nNo pythonw.exe next to %s: install Python from python.org, which ships it." % sys.executable
    else:
        out = ("Schedule tick every 5 min (run crontab -e and add this line):\n"
               '*/5 * * * * "%s" "%s" tick >/dev/null 2>&1' % (sys.executable, SCRIPT))
    if os.environ.get("DEVPING_HOME"):
        out += "\nDEVPING_HOME is set: the scheduled job needs the same value."
    return out


def setup(cfg: Config, a: argparse.Namespace) -> int:
    for k in ("webhook", "bot_token", "channel_id"):
        if getattr(a, k):
            setattr(cfg, k, getattr(a, k).strip())
    if cfg.webhook and not cfg.webhook.startswith("https://"):
        sys.exit("the webhook must be an https:// URL")
    cfg.save()
    state_dir().mkdir(parents=True, exist_ok=True)
    print("config: %s (webhook %s; channel read %s)" % (
        cfg_path(), "set" if cfg.webhook else "MISSING, run setup --webhook URL",
        "on" if cfg.bot_token and cfg.channel_id else "off"))
    print(schedule_help())
    return 0


def add(cfg: Config, a: argparse.Namespace) -> int:
    d = Path(a.dir or os.getcwd()).resolve()
    if not d.is_dir():
        sys.exit("no such folder: %s" % d)
    clash = [n for n in cfg.projects if n != a.name and safe(n) == safe(a.name)]
    if not a.name.strip() or clash:
        sys.exit("pick another project name (empty, or its state file clashes with %s)" % ", ".join(clash))
    if a.webhook and not a.webhook.startswith("https://"):
        sys.exit("the webhook must be an https:// URL")
    old = cfg.projects.get(a.name)
    p = old or Project(name=a.name, dir=str(d), chat_dir="", cycle_ts=time.time())
    p.dir, p.idle_min, p.active = str(d), a.idle_min, True
    p.chat_dir = str(Path(a.chat_dir).resolve()) if a.chat_dir else str(CLAUDE_HOME / "projects" / slug(d))
    p.stats_cmd = a.stats_cmd or p.stats_cmd
    p.publish_cmd = a.publish_cmd or p.publish_cmd
    p.webhook = a.webhook or p.webhook
    cfg.projects[a.name] = p
    cfg.save()
    print("added %s (chat transcripts: %s)" % (p.name, p.chat_dir))
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry", "--dry-run", dest="dry", action="store_true",
                        help="print each Discord payload as one JSON line instead of sending it; save nothing")
    ap = argparse.ArgumentParser(prog="devping.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="save the default webhook; print the schedule command")
    s.add_argument("--webhook", default="", help="Discord channel webhook URL (the default channel)")
    s.add_argument("--bot-token", default="", help="optional: a bot token to see other people's messages")
    s.add_argument("--channel-id", default="", help="optional: the channel the bot reads")
    s = sub.add_parser("add", help="register or update a project")
    s.add_argument("name")
    s.add_argument("--dir", default="", help="the project folder (default: here)")
    s.add_argument("--chat-dir", default="", help="its Claude Code transcripts folder (default: from --dir)")
    s.add_argument("--stats-cmd", default="", help="adapter command printing the stats JSON, run in --dir")
    s.add_argument("--publish-cmd", default="", help="optional command started (not awaited) after every tick")
    s.add_argument("--webhook", default="", help="this project's own channel webhook (default: setup's)")
    s.add_argument("--idle-min", type=float, default=20.0, help="idle-chat alarm after this many minutes")
    for c, h in (("start", "begin a dev cycle"), ("stop", "end a dev cycle")):
        sub.add_parser(c, parents=[common], help=h).add_argument("name")
    sub.add_parser("remove", help="unregister a project (its card stays)").add_argument("name")
    s = sub.add_parser("alert", parents=[common], help="an owner-important line on the card")
    s.add_argument("name")
    s.add_argument("text")
    s.add_argument("--key", default="", help="stable dedup key (default: the text's start)")
    s.add_argument("--cooldown", type=float, default=30.0, help="minutes before the same key may show again")
    s = sub.add_parser("note", parents=[common], help="quiet line on the card")
    s.add_argument("name")
    s.add_argument("text")
    sub.add_parser("tick", parents=[common], help="refresh cards, record alerts").add_argument("name", nargs="?",
                                                                                               default="")
    sub.add_parser("list", help="list registered projects")
    return ap


def main(argv: list[str] | None = None) -> int:
    global DRY
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    a = build_parser().parse_args(argv)
    DRY = getattr(a, "dry", False)
    cfg = Config.load()
    if a.cmd == "setup":
        return setup(cfg, a)
    if a.cmd == "add":
        return add(cfg, a)
    if a.cmd == "list":
        for n, p in cfg.projects.items():
            print("%-20s %-7s %s" % (n, "active" if p.active else "stopped", p.dir))
        return 0
    name = getattr(a, "name", "")
    if name and name not in cfg.projects:
        sys.exit("unknown project %r; known: %s" % (name, ", ".join(cfg.projects) or "none"))
    if a.cmd == "remove":
        cfg.projects.pop(name)
        cfg.save()
        print("removed %s (its Discord card stays)" % name)
        return 0
    names = [name] if name else list(cfg.projects)
    no_hook = [n for n in names if not cfg.hook(cfg.projects[n])]
    if no_hook and not DRY:
        sys.exit("no webhook for %s: run devping.py setup --webhook URL" % ", ".join(no_hook))
    if a.cmd in ("start", "stop"):
        cfg.projects[name].active, cfg.projects[name].cycle_ts = a.cmd == "start", time.time()
        if not DRY:
            cfg.save()
    ok = [run_one(cfg, cfg.projects[n], a) for n in names]
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
