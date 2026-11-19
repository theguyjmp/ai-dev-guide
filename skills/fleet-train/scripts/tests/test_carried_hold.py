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
# an integration row absorbs rows on purpose (clean-submit --absorbs): INT merges CLEAN-k (neither queued, claimed nor
# proven) and adds spec row R9; S is stacked on INT
git("checkout", "-q", "-b", "CLEAN-k", "main")
(h.REPO / "k.txt").write_text("k")
git("add", "-A")
git("commit", "-q", "-m", "CLEAN-k: work")
git("branch", "h/CLEAN-k", "HEAD")
git("checkout", "-q", "-b", "int", "main")
git("merge", "-q", "--no-ff", "-m", "Merge CLEAN-k into int", "CLEAN-k")
(h.REPO / "spec").mkdir(exist_ok=True)
(h.REPO / "spec" / "FUNCTIONS.md").write_text("| R9 | 1 | m | - | cli | tests/acceptance/r9.rs::t_r9 | planned | r9.txt |" + chr(10)
                                           + "| R10 | 1 | m | - | cli | tests/acceptance/r10.rs::t_r10 | proven | r10.txt |" + chr(10))
git("add", "-A")
git("commit", "-q", "-m", "R9: work")
int_sha = git("rev-parse", "HEAD")
git("checkout", "-q", "-b", "s", "int")
(h.REPO / "s.txt").write_text("s")
git("add", "-A")
git("commit", "-q", "-m", "CLEAN-s: work")
s_sha = git("rev-parse", "HEAD")
git("checkout", "-q", "main")
spec9 = h.parse_spec(git("show", f"{int_sha}:spec/FUNCTIONS.md"))
check("fixture spec row parses", "R9" in spec9, spec9)
INT, S_ = {"row": "CLEAN-int", "sha": int_sha}, {"row": "CLEAN-s", "sha": s_sha}
check("without absorbs the integration row and its stack are held", h.disjoint([INT, S_]) == [])
check("absorbed rows verified by git", h.absorbs_unverified(int_sha, ["CLEAN-k", "R9"]) == [])
check("a row the branch does not carry is unverified", h.absorbs_unverified(int_sha, ["R7"]) == ["R7"])
h.save("queue.json", [])
h.queue_clean("int", int_sha, 1, absorbs=["CLEAN-k", "R9"])
h.queue_clean("int", int_sha, 1)
check("clean-submit --absorbs is recorded and kept on resubmit", h.load("queue.json", [])[0].get("absorbs") == ["CLEAN-k", "R9"])
INTA = dict(INT, absorbs=["CLEAN-k", "R9"])
check("absorbed rows are provided by their entry: it and its stack are picked",
      [x["row"] for x in h.disjoint([INTA, S_])] == ["CLEAN-int", "CLEAN-s"] and h.load("pick-held.json", None) == {})
rows = {}
check("gate rows: an absorbed spec row brings its tests from the branch", h.gate_rows([INTA], rows) == []
      and rows["R9"]["tests"] == ["tests/acceptance/r9.rs::t_r9"] and rows["R9"]["status"] == "absorbed")
check("a row the branch marks proven and main does not lands with the entry, with its tests",
      INTA.get("proves") == ["R10"] and rows["R10"]["tests"] == ["tests/acceptance/r10.rs::t_r10"], INTA)
check("an absorbed spec row with no tests cannot land", len(h.gate_rows([dict(INT, absorbs=["R8"])], {})) == 1)
calls, verdicts = {"reject": [], "finish": []}, [(False, ["tests/acceptance/r9.rs::t_r9"], ""), (True, [], "")]
h.gate = lambda ids, rows_, quick=False: verdicts.pop(0)
h.reject = lambda q, why, locked=False: calls["reject"].append(q["row"])
h.finish_spec = lambda ids: calls["finish"].append(sorted(ids))
h.build_train = lambda batch, rows_: (list(batch), [])
h.TRAIN = h.REPO  # the green path reads the train head
rows["CLEAN-s"] = {"id": "CLEAN-s", "tests": [], "status": "cleanup", "file_list": [], "deps": [], "module": "cleanup"}
got = h.try_batch([INTA, S_], rows, reviewed=True)
check("a red absorbed test rejects its integration row and the rest lands", got == [S_] and calls == {"reject": ["CLEAN-int"], "finish": [["CLEAN-s"]]}, calls)

sys.exit(1 if FAILS else 0)
