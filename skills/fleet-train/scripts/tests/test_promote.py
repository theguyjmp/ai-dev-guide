"""promote() lands only a gate-verified train head: a bisect that lands its left half and rejects its right half must not
push the right half's rejected merge to main (2026-11-16: CLEAN-reds-gate3 reached main after its own reject).
Temp state and a fixture git repo; the gate, reviewer and spec step are fakes.

  py -3 tests/test_promote.py
"""
from __future__ import annotations

import os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="test-promote-"))
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


def on_main(path):
    return subprocess.run(["git", "cat-file", "-e", f"main:{path}"], cwd=str(h.REPO), capture_output=True).returncode == 0


def test_bisect_never_lands_the_rejected_half():
    h.REPO.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (h.REPO / "a.txt").write_text("a\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "main")
    batch = []
    for i, r in enumerate(("R1", "R2")):
        git("checkout", "-q", "-B", r, "main")
        (h.REPO / f"{r}.txt").write_text(r, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", r)
        batch.append({"row": r, "sha": git("rev-parse", "HEAD"), "ts": i})
        git("checkout", "-q", "main")
    rows = {r: {"id": r, "tests": [], "status": "cleanup", "file_list": [f"{r}.txt"], "deps": [], "module": "cleanup"}
            for r in ("R1", "R2")}

    def gate(ids, rows_, quick=False):  # unit red whenever R2 is in the tree, named in no file (no blame by file)
        return (False, ["unit"], "") if (h.TRAIN / "R2.txt").exists() else (True, [], "")
    h.gate, h.batch_review, h.finish_spec = gate, (lambda merged, cwd=None, base="main": []), (lambda ids: None)
    h.quarantine.heal = lambda *a, **k: False
    h.GREEN["head"] = ""
    landed = h.try_batch(batch, rows)
    check("bisect lands R1 only", [q["row"] for q in landed] == ["R1"], landed)
    check("the train HEAD still holds the rejected R2 merge", (h.TRAIN / "R2.txt").exists())
    h.promote(landed)  # what train_once does after try_batch
    check("main has R1", on_main("R1.txt"))
    check("main never gets the rejected R2", not on_main("R2.txt"), git("log", "--oneline", "-3", "main"))
    check("R2 rejected", "R2" in h.load("rejects.json", {}))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"{len(FAILS)} failed" if FAILS else "ALL PASS")
    sys.exit(1 if FAILS else 0)
