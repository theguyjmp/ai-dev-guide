"""Cloud builder bridge: hand frontier rows to Claude Code cloud sessions through GitHub and collect their branches.

  py -3 cloud.py assign --n N   # claim N frontier rows under slots 1000+, publish assign/<row>.json on harness-cloud
  py -3 cloud.py collect        # queue pushed cloud/<row> heads, release cloud/release-<row>, expire silent sessions
  py -3 cloud.py status         # assigned / claimed / pushed / landed

Refs on origin: harness-cloud (assign/<row>.json), cloud/claim-<row> (a session took the row), cloud/<row> (its build),
cloud/release-<row> (its blocker, in a REASON file), cloud/prev-<row> (an earlier attempt to salvage),
cloud/stack-<row> (a queued dep to merge first), cloud-base (the train's local main, the base every session starts from).
The cloud builder brief is <HARNESS_CLOUD_KIT>/BUILDER.md in the repo (templates: the skill's cloud/ folder).
Run `collect` every 5 minutes (Task Scheduler / cron); it also tops the assignments up to HARNESS_CLOUD_TARGET.

Env: HARNESS_CLOUD_KIT (kit folder in the repo, default tools/cloud), HARNESS_CLOUD_TARGET (rows kept assigned, default
40), HARNESS_CLOUD_TTL (seconds a claimed row may push nothing, default 7200), HARNESS_CLOUD_QA (quality scripts copied
to sessions, comma-separated, default the train's), plus harness.py's HARNESS_* settings.
"""
from __future__ import annotations

import argparse, json, os, re, subprocess, sys, traceback

if os.name == "nt":  # run by pythonw from Task Scheduler: no console window per git call
    class _Quiet(subprocess.Popen):
        def __init__(self, *a, **kw):
            kw.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
            super().__init__(*a, **kw)
    subprocess.Popen = _Quiet

from pathlib import Path

import harness as h

CW = h.WT / "harness-cloud"  # worktree of branch harness-cloud
BR = "harness-cloud"
KIT = os.environ.get("HARNESS_CLOUD_KIT", "tools/cloud")  # the builder kit inside the repo
BASE = "cloud-base"  # origin branch every session starts from (refreshed from local main by collect)
CLOUD_TTL = int(os.environ.get("HARNESS_CLOUD_TTL", str(2 * 3600)))  # a session that claimed a row and pushed nothing for this long is released
STATE = "cloud.json"
QA = [f for f in os.environ.get("HARNESS_CLOUD_QA", ",".join(h.P["cloud_qa"])).split(",") if f]  # the train's checks (stack profile)
QA_DIR = "/tmp/harness-qa"  # where the kit's claim.sh puts them in a session


def heads() -> dict:
    """origin's cloud/* branches: {name under cloud/: sha}."""
    out = h.git("ls-remote", "origin", "refs/heads/cloud/*").stdout
    return {ref.split("refs/heads/cloud/", 1)[1]: sha for sha, ref in (l.split("\t") for l in out.splitlines() if "\t" in l)}


def fetch_cloud():
    h.git("fetch", "-q", "--prune", "origin", "+refs/heads/cloud/*:refs/remotes/origin/cloud/*", check=False)


def delete_refs(names):
    if names:
        h.git("push", "-q", "origin", "--delete", *[f"refs/heads/cloud/{n}" for n in names], check=False)


def sync_wt():
    """Bring the harness-cloud worktree to origin's branch (or start it as an orphan: it carries no repo files)."""
    have = h.git("ls-remote", "origin", f"refs/heads/{BR}").stdout.strip()
    if have:
        h.git("fetch", "-q", "origin", f"+refs/heads/{BR}:refs/remotes/origin/{BR}")
    if not (CW / ".git").exists():
        if have:
            h.git("worktree", "add", "-q", "-f", "-B", BR, str(CW), f"origin/{BR}")
        else:
            h.git("worktree", "add", "-q", "--orphan", "-B", BR, str(CW))
            (CW / "README.md").write_text("Cloud builder assignments (fleet-train cloud.py). One file per row in "
                                          "assign/, the land train's quality scripts in qa/. A session claims a row by "
                                          f"pushing cloud/claim-<row>; see {KIT}/BUILDER.md (also in kit/ here).\n",
                                          encoding="utf-8")
    elif have:
        h.git("reset", "-q", "--hard", f"origin/{BR}", cwd=CW)
    (CW / "assign").mkdir(exist_ok=True)
    (CW / "qa").mkdir(exist_ok=True)
    for f in QA:  # current copies of the land train's quality scripts, so sessions run exactly what the train runs
        (CW / "qa" / f).write_bytes((Path(h.QS) / f).read_bytes())
    (CW / "kit").mkdir(exist_ok=True)
    for f in h.git("ls-tree", "--name-only", "main", f"{KIT}/").stdout.split():  # the builder kit, readable even
        (CW / "kit" / Path(f).name).write_bytes(h.git("show", f"main:{f}").stdout.encode())  # while origin/main lags


def publish(msg):
    h.git("add", "-A", ".", cwd=CW)
    if h.git("status", "--porcelain", cwd=CW).stdout.strip():
        h.git("commit", "-qm", msg, cwd=CW)
        h.git("push", "-q", "origin", f"HEAD:refs/heads/{BR}", cwd=CW)


def write_assignment(slot, info, hd):
    """assign/<row>.json: the local claim fields plus where to branch, push and salvage from, and the queued deps to
    merge first (stacked start, as a local claim does)."""
    row = info["row"]
    stack = []
    for q in h.load("queue.json", []):
        if q["row"] in info["deps"]:
            h.git("push", "-q", "-f", "origin", f"{q['sha']}:refs/heads/cloud/stack-{q['row']}", check=False)
            stack.append(f"origin/cloud/stack-{q['row']}")
    prev = info.get("previous_branch")
    if prev:  # local-only branch: publish it so the session can merge it
        h.git("push", "-q", "-f", "origin", f"{prev['branch']}:refs/heads/cloud/prev-{row}", check=False)
        prev = {**prev, "branch": f"origin/cloud/prev-{row}"}
    elif f"prev-{row}" in hd:  # work in progress a released cloud session saved
        prev = {"branch": f"origin/cloud/prev-{row}"}
    a = {**info, "slot": slot, "branch": f"cloud/{row}", "base": f"origin/{BASE}", "previous_branch": prev, "stack": stack,
         "must_also_before_commit": info["must_also_before_commit"].replace(f"py -3 {h.QS}/", f"python3 {QA_DIR}/"),
         "assigned": h.iso(), "brief": f"{KIT}/BUILDER.md"}
    (CW / "assign" / f"{row}.json").write_text(json.dumps(a, indent=1), encoding="utf-8")


def cloud_claims() -> dict:
    """{row: (slot, claim)} for every cloud slot in claims.json."""
    return {c["row"]: (int(k), c) for k, c in h.load("claims.json", {}).items() if h.is_cloud(k)}


def free_slot(claims) -> int:
    s = h.CLOUD_SLOT0
    while str(s) in claims:
        s += 1
    return s


def cmd_assign(a):
    with h.lock("cloud"):
        sync_wt()
        origin_rows = h.parse_spec(h.git("show", f"origin/{BASE}:{h.SPEC}").stdout)
        st = h.load(STATE, {"rows": {}})
        took = []
        with h.lock("state"):
            if (h.ST / "pause").exists():
                sys.exit("harness paused")
            rows = h.main_spec()
            claims, queue, attempts = h.load("claims.json", {}), h.load("queue.json", []), h.load("attempts.json", {})
            for _ in range(a.n):
                # a session branches from origin/cloud-base: every dep must be proven there or queued (merged in as a stack)
                queued = {q["row"] for q in queue}
                f = [r for r in h.frontier(rows, claims, queue, attempts)
                     if all(origin_rows.get(d, {}).get("status") == "proven" or d in queued for d in rows[r]["deps"])]
                if not f:
                    break
                slot = free_slot(claims)
                took.append((slot, h.take_row(slot, f[0], rows, claims, attempts)))
        hd = heads()
        for slot, info in took:
            write_assignment(slot, info, hd)
            st["rows"][info["row"]] = {"slot": slot, "assigned_ts": h.now()}
        if took:
            publish("assign " + " ".join(i["row"] for _, i in took))
        h.save(STATE, st)
        print(json.dumps({"assigned": [i["row"] for _, i in took]}))


def release(slot, row, reason):
    h.cmd_release(argparse.Namespace(slot=slot, row=row, reason=reason))


def batch_rows() -> set:
    """Rows of the train batch in flight (the last train_start with no train_end after it)."""
    for e in reversed(h.read_events()[-3000:]):
        if e.get("event") == "train_end":
            return set()
        if e.get("event") == "train_start":
            return set(e.get("rows", []))
    return set()


def export_conflicts(hd, st):
    """Queued rows outside the batch in flight that no longer merge onto main go to cloud rebasers as
    cloud/rebase-<row> (cloud/REBASER.md); rebased while they wait, they merge clean."""
    batch, sent = batch_rows(), 0
    for q in h.load("queue.json", []):
        row, sha = q["row"], q["sha"]
        if row in batch or st["rows"].get(row, {}).get("rebase_sent") == sha or f"rebased-{row}" in hd:
            continue
        if h.git("merge-tree", "--write-tree", "main", sha, check=False).returncode == 1:
            h.git("push", "-q", "-f", "origin", f"{sha}:refs/heads/cloud/rebase-{row}", check=False)
            st["rows"].setdefault(row, {})["rebase_sent"] = sha
            sent += 1
    return sent


def take_rebased(row, sha, st):
    """Swap a queued row's sha for its cloud rebase, if the row still waits (not in the batch) on the sha we sent.
    The swap happens under the state lock and leaves claims untouched."""
    old = st["rows"].get(row, {}).get("rebase_sent")
    if not old or row in batch_rows():
        return False
    with h.lock("state"):
        queue = h.load("queue.json", [])
        hit = [q for q in queue if q["row"] == row and q["sha"] == old]
        if not hit:
            return False
        hit[0]["sha"] = sha
        h.git("branch", "-f", f"h/{row}", sha)
        h.save("queue.json", queue)
    st["rows"][row].update(queued_sha=sha, rebased=h.iso())
    h.event("cloud_rebased", row=row, sha=sha)
    return True


def collect():
    with h.lock("cloud"):
        sync_wt()
        st = h.load(STATE, {"rows": {}})
        hd = heads()
        if hd:
            fetch_cloud()
        owned = cloud_claims()
        gone, done = [], []  # refs to delete, assignment files to drop

        for name, sha in hd.items():
            if name.startswith("claim-"):
                row = name[6:]
                if row in owned:
                    st["rows"].setdefault(row, {}).setdefault("claim_seen", h.now())
                else:
                    gone.append(name)  # stale claim of a row no cloud slot holds
            elif name.startswith("release-"):
                row = name[8:]
                if row in owned:
                    why = h.git("show", f"{sha}:REASON", check=False).stdout.strip() or "no REASON file"
                    release(owned[row][0], row, f"cloud: {why}")
                    st["rows"].setdefault(row, {})["released"] = h.iso()
                    done.append(row)
                gone += [name, f"claim-{row}", row]
            elif name.startswith("rebased-"):
                row = name[8:]
                take_rebased(row, sha, st)
                gone += [name, f"rebase-{row}", f"rebase-claim-{row}"]
            elif h.ROW_RE.match(name):
                row = name
                if st["rows"].get(row, {}).get("queued_sha") == sha:
                    continue
                if row not in owned:
                    st.setdefault("orphans", {})[row] = sha  # no cloud slot holds it (released or expired): ignore
                    continue
                if h.git("rev-list", "--count", f"main..{sha}", check=False).stdout.strip() in ("", "0"):
                    continue  # nothing on top of main yet
                slot, c = owned[row]
                with h.lock("state"):
                    h.queue_sha(slot, row, sha, c["ts"])
                st["rows"].setdefault(row, {}).update(queued_sha=sha, queued=h.iso())
                done.append(row)
                gone += [row, f"claim-{row}", f"prev-{row}"]

        # sessions that claimed a row and pushed nothing for CLOUD_TTL: release, free the claim ref, offer the row again
        owned = cloud_claims()
        readd = []
        for row, (slot, c) in owned.items():
            seen = st["rows"].get(row, {}).get("claim_seen")
            if seen and h.now() - seen > CLOUD_TTL and row not in hd:
                release(slot, row, f"cloud timeout: a session claimed the row and pushed nothing for {CLOUD_TTL // 60} min")
                gone.append(f"claim-{row}")
                st["rows"][row].pop("claim_seen", None)
                readd.append((slot, row))
        if readd:
            with h.lock("state"):
                rows = h.main_spec()
                claims, attempts = h.load("claims.json", {}), h.load("attempts.json", {})
                taken = {c["row"] for c in claims.values()}  # a local builder may have taken it meanwhile
                infos = [(slot, h.take_row(slot, row, rows, claims, attempts)) for slot, row in readd if row not in taken]
            for slot, info in infos:
                write_assignment(slot, info, hd)

        # an assignment whose cloud claim is gone (released, queued, or dropped by hand) is withdrawn
        owned = cloud_claims()
        for p in (CW / "assign").glob("*.json"):
            if p.stem in done or p.stem not in owned:
                p.unlink()
        queued = {q["row"] for q in h.load("queue.json", [])}
        gone += [n for n in hd if n.startswith(("rebase-", "rebase-claim-")) and n.split("-")[-1] not in queued]
        exported = export_conflicts(hd, st)
        if exported:
            h.event("cloud_rebase_export", n=exported)
        gone += [n for n in hd if n.startswith("stack-") and n[6:] not in queued]  # dep landed or was rejected
        delete_refs(sorted(set(gone) & set(hd)))
        publish("collect: " + " ".join(sorted(set(done) | {r for _, r in readd})) if (done or readd) else "collect")
        h.save(STATE, st)
        if done or readd:
            h.event("cloud_collect", done=done, readd=[r for _, r in readd])
        return {"done": done, "readd": [r for _, r in readd], "refs": len(hd)}


def refresh_cloud_base():
    """Cloud builders start from origin/cloud-base = local main (2026-11-12: the pre-push quality hook guards only
    refs/heads/main, and origin/main can lag local main when a push is refused; cloud work must start from what the
    train has)."""
    h.git("push", "-q", "-f", "origin", f"main:refs/heads/{BASE}", check=False)


def cmd_collect(a):
    try:
        refresh_cloud_base()
        out = collect()
        # keep CLOUD_TARGET rows outstanding (assigned, not yet pushed) so self-firing routines always find work
        target = int(os.environ.get("HARNESS_CLOUD_TARGET", "40"))
        want = target - len(cloud_claims())
        print(json.dumps(out))
        if want > 0:
            cmd_assign(argparse.Namespace(n=want))
    except Exception:  # noqa: BLE001  (scheduled task: log and exit non-zero)
        h.event("cloud_error", error=traceback.format_exc()[-1500:])
        raise


def cmd_status(a):
    owned = cloud_claims()
    hd = heads()
    rows = h.main_spec()
    ev = h.read_events()
    pushed = {e["row"] for e in ev if e["event"] == "submit" and h.is_cloud(e.get("slot", 0))}
    print(json.dumps({
        "assigned": len(owned),
        "claimed": sum(1 for n in hd if n.startswith("claim-")),
        "waiting_for_a_session": sum(1 for r in owned if f"claim-{r}" not in hd),
        "pushed": len(pushed),
        "landed": sum(1 for r in pushed if rows.get(r, {}).get("status") == "proven"),
        "released": sum(1 for e in ev if e["event"] == "release" and h.is_cloud(e.get("slot", 0))),
    }))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("assign").add_argument("--n", type=int, required=True)
    sp.add_parser("collect")
    sp.add_parser("status")
    a = ap.parse_args()
    {"assign": cmd_assign, "collect": cmd_collect, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    main()
