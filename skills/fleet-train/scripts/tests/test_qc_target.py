"""Which cargo target dir `quality_problems` (harness.py quality-check) builds in. Every caller once shared the train's
target, and a shared fallback goes stale under concurrent lanes: a caller's own CARGO_TARGET_DIR wins, a slot worktree
uses its slot target, and the shared qc-other is used only when neither applies, under the qc-other lock.
Temp state; every command is stubbed (no cargo runs).

  py -3 tests/test_qc_target.py
"""
from __future__ import annotations

import os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="test-qc-target-"))
os.environ.update({"HARNESS_HOME": str(TMP / "home"), "HARNESS_REPO": str(TMP / "repo"), "HARNESS_WT": str(TMP / "wt"),
                   "HARNESS_STACK": "rust"})
os.environ.pop("CARGO_TARGET_DIR", None)
sys.path.insert(0, str(HERE))
import harness as h  # noqa: E402

FAILS = []
SEEN = []  # (CARGO_TARGET_DIR, qc-other lock held) per command


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def fake_sh(cmd, cwd=None, check=True, env=None, timeout=None, **kw):
    SEEN.append(((env or {}).get("CARGO_TARGET_DIR", ""), (h.ST / "qc-other.lock").exists()))
    return subprocess.CompletedProcess(cmd, 0, "", "")


h.sh = fake_sh
h.static_problems = lambda *a, **k: ""
h.changed_files = lambda *a, **k: []
h.ST.mkdir(parents=True, exist_ok=True)


def targets(cwd: Path, env_dir):
    SEEN.clear()
    if env_dir is None:
        os.environ.pop("CARGO_TARGET_DIR", None)
    else:
        os.environ["CARGO_TARGET_DIR"] = env_dir
    cwd.mkdir(parents=True, exist_ok=True)
    out = h.quality_problems(cwd)
    check(f"{cwd.name} / env {env_dir!r}: no problems reported", out == "", out)
    return list(SEEN)


def all_in(got, want):
    return bool(got) and all(t == want for t, _ in got)


QC = str(h.TARGETS / "qc-other")
lane, own = TMP / "lanes" / "wt-fix-1", str(TMP / "lane-target")
got = targets(lane, own)
check("caller's CARGO_TARGET_DIR is used", all_in(got, own), got)
check("own target: the qc-other lock is not taken", got and not any(held for _, held in got), got)
check("empty CARGO_TARGET_DIR counts as unset: qc-other", all_in(targets(lane, ""), QC))
got = targets(lane, None)
check("unset CARGO_TARGET_DIR falls back to qc-other", all_in(got, QC), got)
check("qc-other: every command runs under the qc-other lock", got and all(held for _, held in got), got)
check("qc-other lock is released afterwards", not (h.ST / "qc-other.lock").exists())
check("train tree keeps the train target even with CARGO_TARGET_DIR set",
      all_in(targets(h.TRAIN, own), h.TRAIN_ENV["CARGO_TARGET_DIR"]))
check("slot worktree s3 builds in its slot target", all_in(targets(h.WT / "s3", None), h.env_for(3)["CARGO_TARGET_DIR"]))
h.TRAIN_ENV = {k: v for k, v in h.TRAIN_ENV.items() if k != "CARGO_TARGET_DIR"}  # a stack without a cargo target
got = targets(lane, None)
check("no cargo target in the stack: env untouched, no lock", got and all(t == "" and not held for t, held in got), got)
os.environ.pop("CARGO_TARGET_DIR", None)
print(f"{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
