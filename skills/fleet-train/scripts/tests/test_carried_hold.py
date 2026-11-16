"""disjoint() holds a queued row whose branch carries another unproven row's commits until that row is picked earlier
in the batch or proven (2026-11-16: a stacked row carried its unlanded base row and failed the plan check).
Temp state and a fixture git repo.

  py -3 tests/test_carried_hold.py
"""
from __future__ import annotations

import os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="test-carried-"))
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


h.REPO.mkdir(parents=True, exist_ok=True)
h.ST.mkdir(parents=True, exist_ok=True)
git("init", "-q", "-b", "main")
git("config", "user.email", "t@t")
git("config", "user.name", "t")
(h.REPO / "a.txt").write_text("0")
git("add", "-A")
git("commit", "-q", "-m", "base")
shas = {}
for row, parent, f in (("R1", "main", "r1.txt"), ("R2", "R1", "r2.txt"), ("R3", "main", "r3.txt")):
    git("checkout", "-q", "-B", row, parent)
    (h.REPO / f).write_text(row)
    git("add", "-A")
    git("commit", "-q", "-m", f"{row}: work")
    shas[row] = git("rev-parse", "HEAD")
git("checkout", "-q", "main")
planned = {"R1": {"status": "planned"}, "R2": {"status": "planned"}, "R3": {"status": "planned"}}
h.main_spec = lambda: planned
q = lambda *rs: [{"row": r, "sha": shas[r]} for r in rs]
check("carried rows read from subjects", h.carried_rows(q("R2")[0], planned) == {"R1"})
check("dependent held when its carried row is not picked", [x["row"] for x in h.disjoint(q("R2", "R3"))] == ["R3"])
check("held row recorded", h.load("pick-held.json", {}) == {"R2": ["R1"]})
check("dependent picked after its carried row", [x["row"] for x in h.disjoint(q("R1", "R2", "R3"))] == ["R1", "R2", "R3"])
planned["R1"]["status"] = "proven"
check("a proven carried row does not hold", [x["row"] for x in h.disjoint(q("R2"))] == ["R2"])
sys.exit(1 if FAILS else 0)
