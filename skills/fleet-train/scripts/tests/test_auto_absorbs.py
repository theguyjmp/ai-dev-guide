"""A clean row whose own commits carry spec rows nobody absorbs absorbs them at clean-submit (harness.absorb_own_rows),
so the picker never holds the rows stacked on it for them. Temp state and a fixture git repo.

  py -3 tests/test_auto_absorbs.py
"""
from __future__ import annotations

import json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="test-auto-absorbs-"))
os.environ.update({"HARNESS_HOME": str(TMP / "home"), "HARNESS_REPO": str(TMP / "repo"), "HARNESS_WT": str(TMP / "wt")})
sys.path.insert(0, str(HERE))
import harness as h  # noqa: E402

FAILS = []


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def git(*a):
    return subprocess.run(["git", *a], cwd=str(h.REPO), capture_output=True, text=True, check=True).stdout.strip()


def commit(branch, parent, *subjects):
    git("checkout", "-q", "-B", branch, parent)
    for s in subjects:
        f = h.REPO / (s.split(":")[0] + ".txt")
        f.write_text((f.read_text() if f.exists() else "") + s)
        git("add", "-A")
        git("commit", "-q", "-m", s)
    git("checkout", "-q", "main")
    return git("rev-parse", branch)


h.REPO.mkdir(parents=True, exist_ok=True)
h.ST.mkdir(parents=True, exist_ok=True)
git("init", "-q", "-b", "main")
git("config", "user.email", "t@t")
git("config", "user.name", "t")
(h.REPO / "a.txt").write_text("0")
git("add", "-A")
git("commit", "-q", "-m", "base")
spec = {i: {"status": "planned", "tests": [f"tests/{i}.rs::t"]} for i in ("R101", "R102", "R103", "R104", "R105")}
spec["R105"]["status"] = "proven"
h.main_spec = lambda: spec
absorbs = lambda row: next((q.get("absorbs", []) for q in h.load("queue.json", []) if q["row"] == row), None)
submit = lambda name, sha, **kw: h.queue_clean(name, sha, 1, **kw)

x = commit("x", "main", "CLEAN-x: a", "R999: not a spec row", "R105: touch proven")
check("never a row outside the spec or a proven row", h.own_spec_rows({"row": "CLEAN-x", "sha": x}, []) == [])
a1 = commit("a1", "main", "CLEAN-a1: tidy", "R101: built in the lane")
submit("a1", a1)
check("a clean row absorbs the spec row it built", absorbs("CLEAN-a1") == ["R101"], absorbs("CLEAN-a1"))
events = [json.loads(ln) for ln in (h.ST / "events.jsonl").read_text().splitlines()]
check("absorbs_auto logged", [(e["row"], e["ids"]) for e in events if e["event"] == "absorbs_auto"] == [("CLEAN-a1", ["R101"])])
a2 = commit("a2", "a1", "CLEAN-a2: next", "R102: built in the lane")
submit("a2", a2)
check("a stacked row absorbs only its own rows", absorbs("CLEAN-a2") == ["R102"], absorbs("CLEAN-a2"))
b1 = commit("b1", "main", "CLEAN-b1: other", "R101: again")
submit("b1", b1)
check("an id another queued entry absorbs is never moved", absorbs("CLEAN-b1") == [], absorbs("CLEAN-b1"))
h.save("queue.json", [q for q in h.load("queue.json", []) if q["row"] != "CLEAN-a1"])  # a1 rejected
check("a child carrying an unqueued clean parent never takes its rows",
      h.own_spec_rows({"row": "CLEAN-a2", "sha": a2}, h.load("queue.json", [])) == [])
submit("a1", commit("a1", "a1", "CLEAN-a1: fix"))
check("the parent's resubmit absorbs its row again", absorbs("CLEAN-a1") == ["R101"], absorbs("CLEAN-a1"))
submit("a3", commit("a3", "a2", "CLEAN-a3: more", "R103: built", "R104: built"), absorbs=["R102"])
check("an explicit absorbs is kept beside the automatic ones", absorbs("CLEAN-a3") == ["R102", "R103", "R104"],
      absorbs("CLEAN-a3"))
by = {q["row"]: q for q in h.load("queue.json", [])}
h.disjoint([by[r] for r in ("CLEAN-a1", "CLEAN-a2", "CLEAN-a3")])
check("the picker holds none of the stack", h.load("pick-held.json", {}) == {}, h.load("pick-held.json", {}))

print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
