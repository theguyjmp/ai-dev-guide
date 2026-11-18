"""Cloud builder bridge: hand frontier rows to Claude Code cloud sessions through GitHub and collect their branches.

  py -3 cloud.py assign --n N   # claim N frontier rows under slots 1000+, publish assign/<row>.json on harness-cloud
  py -3 cloud.py collect        # queue pushed cloud/<row> heads, release cloud/release-<row>, expire silent sessions
  py -3 cloud.py status         # assigned / claimed / pushed / landed
  py -3 cloud.py fixes          # fix lane (state/fix-tasks.txt): claimable now, claimed, queued

Refs on origin: harness-cloud (assign/<row>.json), cloud/claim-<row> (a session took the row), cloud/<row> (its build),
cloud/release-<row> (its blocker, in a REASON file), cloud/prev-<row> (an earlier attempt to salvage),
cloud/stack-<row> (a queued dep to merge first), cloud-base (the train's local main, the base every session starts from).
Task lanes (Lane; kit FIXER.md): cloud/<kind>-claim-<task>, cloud/wip-<kind>-<task>, <kind>/cloud-<task>; harness-cloud
<kind>-tasks.txt, <kind>-tasks/<task>.md, <kind>-check.sh, <kind>-rejects/<task>.md.
The cloud builder brief is <HARNESS_CLOUD_KIT>/BUILDER.md in the repo (templates: the skill's cloud/ folder).
Run `collect` every 5 minutes (Task Scheduler / cron); it also tops the assignments up to HARNESS_CLOUD_TARGET.

Env: HARNESS_CLOUD_KIT (kit folder in the repo, default tools/cloud), HARNESS_CLOUD_TARGET (rows kept assigned, default
40), HARNESS_CLOUD_TTL (seconds a claimed row may push nothing, default 7200), plus harness.py's HARNESS_* settings.
"""
from __future__ import annotations

import argparse, json, os, re, subprocess, sys, traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass

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
STATE = h.CLOUD_STATE
QA_DIR = "/tmp/harness-qa"  # claim.sh copies qa/ here
SESSION_RULES_CHECK = (f"HARNESS_QS={QA_DIR} python3 {QA_DIR}/rules_gate.py check --dir . --base origin/{BASE} "
                       f"--state {QA_DIR} --lang {h.STACK}")


def qa_files() -> dict:
    """{name: source}: the train's checks for a session (overhaul-build scripts, rules gate, its debt)."""
    out = {p.name: p for p in sorted(Path(h.QS).glob("*.py"))}
    here = Path(h.__file__).parent
    out.update({n: here / n for n in ("rules_gate.py", "rules_langs.py")})
    debt = h.ST / h.rules_gate.DEBT
    if debt.exists():
        out[debt.name] = debt
    return out


def for_session(cmds: str) -> str:
    """harness.QUALITY_BRIEF as a session runs it."""
    return cmds.replace(h.RULES_CHECK, SESSION_RULES_CHECK).replace(f"py -3 {h.QS}/", f"python3 {QA_DIR}/")


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
    qa = qa_files()
    for f in (CW / "qa").iterdir():
        if f.is_file() and f.name not in qa:
            f.unlink()
    for name, src in qa.items():
        (CW / "qa" / name).write_bytes(src.read_bytes())
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
         "must_also_before_commit": for_session(info["must_also_before_commit"]),
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


def review_dup(sha) -> str:
    """Duplicate-capability refusal for a cloud row's tip (h.submit_dup, the local submit's check), else "". The tip is
    checked out detached in a scratch worktree: the reviewer reads the tree."""
    wt = h.WT / "cloud-review"
    if (wt / ".git").exists():
        h.git("checkout", "-q", "-f", "--detach", sha, cwd=wt)
    else:
        h.git("worktree", "add", "-q", "-f", "--detach", str(wt), sha)
    return h.submit_dup(wt)


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


REBASE_CLAIM_TTL = 60 * 60  # a rebaser claim with no rebased-<row> answer for this long is abandoned


def expire_rebase_claims(hd, st, gone) -> list:
    """Delete a cloud/rebase-claim-<row> that has had no cloud/rebased-<row> answer for REBASE_CLAIM_TTL and forget the
    sent sha, so export_conflicts publishes the row for rebase again. rows[row].rebase_expired counts the expiries."""
    queued, t, out = {q["row"] for q in h.load("queue.json", [])}, h.now(), []
    claims = {n.removeprefix("rebase-claim-") for n in hd if n.startswith("rebase-claim-")}
    for row, r in st["rows"].items():
        if row not in claims:
            r.pop("rebase_claim_seen", None)
    for row in sorted(claims):
        if f"rebased-{row}" in hd or row not in queued:
            continue
        r = st["rows"].setdefault(row, {})
        if t - r.setdefault("rebase_claim_seen", t) < REBASE_CLAIM_TTL:
            continue
        gone.append(f"rebase-claim-{row}")
        r.pop("rebase_claim_seen", None)
        r.pop("rebase_sent", None)
        r["rebase_expired"] = r.get("rebase_expired", 0) + 1
        h.event("cloud_rebase_claim_expired", row=row, n=r["rebase_expired"])
        out.append(row)
    return out


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
        gone, done, readd = [], [], []  # refs to delete, assignment files to drop, (slot, row) offered again

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
                why = review_dup(sha)
                if why:  # the same refusal a local submit gets; the next builder reads it in the row's rejects
                    release(slot, row, "cloud: " + why)
                    st["rows"].setdefault(row, {}).pop("claim_seen", None)
                    readd.append((slot, row))
                    gone += [row, f"claim-{row}", f"prev-{row}"]
                    continue
                with h.lock("state"):
                    h.queue_sha(slot, row, sha, c["ts"])
                st["rows"].setdefault(row, {}).update(queued_sha=sha, queued=h.iso())
                done.append(row)
                gone += [row, f"claim-{row}", f"prev-{row}"]

        # sessions that claimed a row and pushed nothing for CLOUD_TTL: release, free the claim ref, offer the row again
        owned = cloud_claims()
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
        expire_rebase_claims(hd, st, gone)
        exported = export_conflicts(hd, st)
        if exported:
            h.event("cloud_rebase_export", n=exported)
        gone += [n for n in hd if n.startswith("stack-") and n[6:] not in queued]  # dep landed or was rejected
        delete_refs(sorted(set(gone) & set(hd)))
        lanes = {lane.state: collect_lane(lane, hd, st) for lane in LANES}
        publish("collect: " + " ".join(sorted(set(done) | {r for _, r in readd})) if (done or readd) else "collect")
        h.save(STATE, st)
        if done or readd:
            h.event("cloud_collect", done=done, readd=[r for _, r in readd])
        return {"done": done, "readd": [r for _, r in readd], "refs": len(hd), **lanes}


# ------------------------------------------------------------------ cloud task lanes (kit FIXER.md)
# A lane hands sessions tasks through harness-cloud: <kind>-tasks.txt (claimable, one id per line), <kind>-check.sh (the
# train's checks; stamps a passing tree), <kind>-rejects/<task>.md (why a branch came back) and <kind>-tasks/<task>.md
# (the task's text). A session claims ONE task with the kit's claim_task.sh (unique commit on the new ref
# cloud/<kind>-claim-<task>), saves WIP on cloud/wip-<kind>-<task> and pushes <kind>/cloud-<task>. collect queues a pushed
# branch whose tip carries the lane's stamp of its own tree as CLEAN-<kind>-<task>; an unstamped, rejected or unlisted
# branch goes back to cloud/wip-<kind>-<task> with its reason. A task is claimable while open, unclaimed and not queued.
#   fix: one clean-up packet the manager lists in state/fix-tasks.txt (text in state/fix-tasks/<id>.md); queued at the
#   front. A planner lane is the same Lane with a todo of unplanned roadmap phases.
LANE_TTL = CLOUD_TTL  # a claim whose session pushed nothing (claim, WIP) for this long is released
MERGES_CLEAN = (  # git < 2.38 has no --write-tree (exit 129): the old merge-tree prints conflict markers instead
    f"git merge-tree --write-tree origin/{BASE} HEAD >/dev/null 2>&1; r=$?; if [ $r -gt 1 ]; then "
    f"git merge-tree \"$(git merge-base origin/{BASE} HEAD)\" origin/{BASE} HEAD | grep -q '^+<<<<<<<' && r=1 || r=0; fi; "
    f"[ $r -eq 0 ] || {{ echo 'conflicts with origin/{BASE}: git merge origin/{BASE}, resolve, commit'; exit 1; }}")


@dataclass(frozen=True)
class Lane:
    kind: str                            # refs cloud/<kind>-claim-<task>, cloud/wip-<kind>-<task>, <kind>/cloud-<task>
    task_re: re.Pattern                  # a task id
    todo: Callable[[dict], dict]         # cloud state -> {open task: [anything it covers]}
    order: Callable[[Iterable[str]], list]  # claim order
    stamp: str                           # trailer <kind>-check.sh writes on a passing tree: `<stamp><tree>`
    state: str                           # cloud state key of the queued tasks {task: {sha, queued}}
    front: bool = False                  # queue ahead of every row (h.queue_clean front)
    brief: Callable[[str], str] | None = None  # the task's text, published as <kind>-tasks/<task>.md

    claim = property(lambda s: f"{s.kind}-claim-")
    wip = property(lambda s: f"wip-{s.kind}-")
    branch = property(lambda s: f"{s.kind}/cloud-")

    def row(self, task: str) -> str:
        return f"{h.quarantine.FIX_PREFIX}{self.kind}-{task}"


def fix_tasks(st: dict) -> dict:
    """{task: []} of state/fix-tasks.txt (manager-owned: one id per line, `#` comments), less tasks landed through
    this lane (st fix_landed)."""
    p = h.ST / "fix-tasks.txt"
    ids = [l.split("#", 1)[0].strip() for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []
    landed = st.get("fix_landed", {})
    return {t: [] for t in dict.fromkeys(ids) if FIX.task_re.fullmatch(t) and t not in landed}


def fix_brief(task: str) -> str:
    p = h.ST / "fix-tasks" / f"{task}.md"
    return p.read_text(encoding="utf-8") if p.exists() else f"# {task}\n\nno state/fix-tasks/{task}.md: ask the manager.\n"


FIX = Lane("fix", re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*"), fix_tasks, sorted, "Fix-Check: ", "fixes", front=True,
           brief=fix_brief)
LANES = (FIX,)


def lane_heads(lane: Lane) -> dict:
    """origin's <kind>/cloud-<task> branches: {task: sha}."""
    out = h.git("ls-remote", "origin", f"refs/heads/{lane.branch}*").stdout
    refs = (l.split("\t") for l in out.splitlines() if "\t" in l)
    tasks = ((ref.rsplit(lane.branch, 1)[1], sha) for sha, ref in refs)
    return {t: sha for t, sha in tasks if lane.task_re.fullmatch(t)}


def stamped(sha: str, stamp: str) -> bool:
    """True when sha's message carries `<stamp><its own tree>` (the lane's check script passed on exactly this tree)."""
    tree = h.git("rev-parse", f"{sha}^{{tree}}", check=False).stdout.strip()
    msg = h.git("log", "-1", "--format=%B", sha, check=False).stdout
    return bool(tree) and f"{stamp}{tree}" in msg.splitlines()


def commit_time(sha: str) -> int | None:
    """None when sha is not here (a fetch failed): unknown is never silent."""
    out = h.git("log", "-1", "--format=%ct", sha, check=False).stdout.strip()
    return int(out) if out.isdigit() else None


def drop_ref(ref: str, sha: str) -> None:
    """Delete origin's ref only while it still holds sha (a session may have claimed the task again since)."""
    h.git("push", "-q", "origin", f"--force-with-lease={ref}:{sha}", f":{ref}", check=False)


def salvage(lane: Lane, task: str, sha: str, why: str) -> bool:
    """Hand a branch back to the lane: sha becomes cloud/wip-<kind>-<task>, the reason <kind>-rejects/<task>.md."""
    if h.git("push", "-q", "-f", "origin", f"{sha}:refs/heads/cloud/{lane.wip}{task}", check=False).returncode:
        return False
    (CW / f"{lane.kind}-rejects").mkdir(parents=True, exist_ok=True)
    (CW / f"{lane.kind}-rejects" / f"{task}.md").write_text(
        f"# {lane.branch}{task} came back {h.iso()}\n\nsha {sha} (now cloud/{lane.wip}{task})\n\n{why}\n", encoding="utf-8")
    h.event(f"{lane.kind}_salvaged", task=task, sha=sha[:12], why=why[:300])
    return True


def reject_reason(row: str, sha: str) -> str:
    hits = [r for r in h.load("rejects.json", {}).get(row, []) if r.get("sha") == sha]
    return hits[-1]["reason"] if hits else "the branch left the queue without landing (dropped by hand?)"


def lane_left_queue(lane: Lane, st: dict, queue: dict, hd: dict, out: dict, freed: set) -> None:
    """A queued task that left the queue: landed (recorded in st <kind>_landed), or back to the lane."""
    queued = st.setdefault(lane.state, {})
    for task, p in list(queued.items()):
        if lane.row(task) in queue:
            p["sha"] = queue[lane.row(task)]["sha"]  # a cloud rebase swaps it
            continue
        if h.git("merge-base", "--is-ancestor", p["sha"], "main", check=False).returncode == 0:
            out["landed"].append(task)
            st.setdefault(f"{lane.kind}_landed", {})[task] = h.iso()
            (CW / f"{lane.kind}-rejects" / f"{task}.md").unlink(missing_ok=True)
            if f"{lane.wip}{task}" in hd:
                drop_ref(f"refs/heads/cloud/{lane.wip}{task}", hd[f"{lane.wip}{task}"])
        elif salvage(lane, task, p["sha"], "rejected by the land train: " + reject_reason(lane.row(task), p["sha"])):
            out["salvaged"].append(task)
        else:
            continue
        queued.pop(task)
        freed.add(task)


def lane_pushed(lane: Lane, st: dict, todo: dict, out: dict, freed: set) -> None:
    """Queue each pushed <kind>/cloud-<task> branch stamped for its own tree; send the others back with the reason."""
    queued, batch = st.setdefault(lane.state, {}), batch_rows()
    for task, sha in lane_heads(lane).items():
        if queued.get(task, {}).get("sha") == sha or lane.row(task) in batch:
            continue
        ahead = h.git("rev-list", "--count", f"main..{sha}", check=False).stdout.strip()
        if not ahead.isdigit():
            continue  # not fetched: leave it for the next collect
        ref = f"refs/heads/{lane.branch}{task}"
        if ahead == "0":
            drop_ref(ref, sha)  # nothing on top of main (landed)
            continue
        why = (f"task {task} is not open (not in {lane.kind}-tasks.txt, nothing of it queued): nothing to land"
               if task not in todo and task not in queued else
               f"no {lane.stamp.strip()} stamp of this tree: run {lane.kind}-check.sh after your last change, then push again"
               if not stamped(sha, lane.stamp) else "")
        if why:
            if salvage(lane, task, sha, why):
                out["salvaged"].append(task)
                freed.add(task)
                drop_ref(ref, sha)
            continue
        with h.lock("state"):
            h.queue_clean(f"{lane.kind}-{task}", sha, 0, front=lane.front)
        queued[task] = {"sha": sha, "queued": h.iso()}
        out["queued"].append(task)
        (CW / f"{lane.kind}-rejects" / f"{task}.md").unlink(missing_ok=True)
        drop_ref(ref, sha)


def lane_publish(lane: Lane, tasks: list) -> None:
    """<kind>-tasks.txt, <kind>-check.sh and the task briefs on harness-cloud (LF: sessions read them with bash)."""
    (CW / f"{lane.kind}-tasks.txt").write_text("".join(f"{t}\n" for t in tasks), encoding="utf-8", newline="\n")
    (CW / f"{lane.kind}-check.sh").write_text(check_script(lane), encoding="utf-8", newline="\n")
    if lane.brief:
        d = CW / f"{lane.kind}-tasks"
        d.mkdir(exist_ok=True)
        for f in d.glob("*.md"):
            if f.stem not in tasks:
                f.unlink()
        for t in tasks:
            (d / f"{t}.md").write_text(lane.brief(t), encoding="utf-8", newline="\n")


def collect_lane(lane: Lane, hd: dict, st: dict) -> dict:
    """Queue stamped branches, salvage unstamped, unlisted and rejected ones, release silent or finished claims, and
    publish the lane (lane_publish). Caller holds the cloud lock."""
    out = {"queued": [], "salvaged": [], "landed": [], "released": []}
    if lane_heads(lane):
        h.git("fetch", "-q", "origin", f"+refs/heads/{lane.branch}*:refs/remotes/origin/{lane.branch}*", check=False)
    queue, freed = {q["row"]: q for q in h.load("queue.json", [])}, set()
    claims = {n[len(lane.claim):]: sha for n, sha in hd.items() if n.startswith(lane.claim)}
    lane_left_queue(lane, st, queue, hd, out, freed)
    todo = lane.todo(st)  # after: a task that just landed is no longer open
    lane_pushed(lane, st, todo, out, freed)
    queued = st[lane.state]
    for n, sha in hd.items():  # WIP that main already holds would only do its task twice
        if n.startswith(lane.wip) and h.git("merge-base", "--is-ancestor", sha, "main", check=False).returncode == 0:
            drop_ref(f"refs/heads/cloud/{n}", sha)
    t = h.now()
    for task, sha in claims.items():  # a queued task's claim holds it until the branch lands or comes back
        wip = hd.get(f"{lane.wip}{task}")
        times = [commit_time(x) for x in (sha, wip) if x]
        silent = None not in times and t - max(times) > LANE_TTL
        if task in freed or (task not in queued and (task not in todo or silent)):
            drop_ref(f"refs/heads/cloud/{lane.claim}{task}", sha)
            out["released"].append(task)
    live = {p for p in claims if p not in out["released"]}
    tasks = lane.order(p for p in todo if p not in queued)
    lane_publish(lane, tasks)
    st[f"{lane.kind}_status"] = {"claimable": [p for p in tasks if p not in live], "claimed": lane.order(live),
                                 "queued": lane.order(queued), "t": h.iso()}
    if any(out.values()):
        h.event(f"cloud_{lane.state}", **out)
    return {**out, "claimable": len(st[f"{lane.kind}_status"]["claimable"])}


def check_script(lane: Lane) -> str:
    """<kind>-check.sh: the train's checks on a lane branch, built from the harness's own commands (no copy to drift). On
    a clean commit that passes, it stamps HEAD with `<stamp><tree>`; collect queues only a tip stamped for its own tree."""
    checks = [("strict lint", h.CLIPPY_STRICT), ("unit tests", h.P["unit"]),
              *((c.split()[2].rsplit("/", 1)[-1], for_session(c)) for c in h.QUALITY_CMDS),
              ("rules check", SESSION_RULES_CHECK), ("merges with the base", MERGES_CLEAN)]
    body = "".join(f"run '{name}' <<'CMD'\n{cmd}\nCMD\n" for name, cmd in checks if cmd)
    say = f"{lane.kind.upper()} CHECK"
    return (f"#!/usr/bin/env bash\n# Generated by fleet-train cloud.py: run at the {lane.kind} branch's repo root, all "
            "committed. Do not edit.\nset -uo pipefail\ncd \"$(git rev-parse --show-toplevel)\"\n"
            f"[ -z \"$(git status --porcelain)\" ] || {{ echo '{say}: commit first'; exit 2; }}\n"
            f"git fetch -q origin {BASE} {BR} || exit 2\n"
            f"mkdir -p {QA_DIR} && for p in $(git ls-tree --name-only origin/{BR} qa/); do\n"
            f"  git show \"origin/{BR}:$p\" > \"{QA_DIR}/$(basename \"$p\")\"; done\n"
            "fail=0\nrun() { echo \"== $1\"; local out; out=$(bash -c \"$(cat)\" 2>&1) || "
            "{ echo \"$out\" | tail -40; echo \"FAIL: $1\"; fail=1; }; }\n" + body +
            f"[ $fail -eq 0 ] || {{ echo '{say} FAILED: fix every FAIL, commit, run again'; exit 1; }}\n"
            "t=$(git rev-parse 'HEAD^{tree}')\n"
            f"git -c trailer.ifexists=replace commit -q --amend --no-edit --trailer \"{lane.stamp}$t\"\n"
            f"echo \"{say} OK $t\"\n")


def cmd_lane(a):
    """A lane's tasks as the last collect saw them; claimable = the useful number of concurrent sessions."""
    kind = next(l.kind for l in LANES if l.state == a.cmd)
    s = h.load(STATE, {}).get(f"{kind}_status", {})
    print(json.dumps({"claimable_now": len(s.get("claimable", [])), **s}))


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
        **{f"{l.kind}_claimable": len(h.load(STATE, {}).get(f"{l.kind}_status", {}).get("claimable", [])) for l in LANES},
    }))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("assign").add_argument("--n", type=int, required=True)
    sp.add_parser("collect")
    sp.add_parser("status")
    for lane in LANES:
        sp.add_parser(lane.state)
    a = ap.parse_args()
    {"assign": cmd_assign, "collect": cmd_collect, "status": cmd_status, **{l.state: cmd_lane for l in LANES}}[a.cmd](a)


if __name__ == "__main__":
    main()
