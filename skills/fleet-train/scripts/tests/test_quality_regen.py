"""`quality-check --regen` regenerates generated files first, exactly as the train does before its quality stage
(regen_tree), so a pre-push check of a row that changes a generator's input does not fail on drift the train would fix;
a failing regen command fails the check with its text; without --regen nothing is regenerated. Temp repo and state only;
quality_problems is stubbed to report whether the generated file is current.

  py -3 tests/test_quality_regen.py
"""
from __future__ import annotations

import argparse, contextlib, io, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
FAILS: list[str] = []


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def g(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    repo = root / "repo"
    repo.mkdir()
    os.environ.update(HARNESS_HOME=str(root / "home"), HARNESS_WT=str(root / "wt"), HARNESS_REPO=str(repo),
                      HARNESS_STACK="node")
    (root / "home" / "state").mkdir(parents=True)
    g(repo, "init", "-q", "-b", "main")
    g(repo, "config", "user.email", "t@t")
    g(repo, "config", "user.name", "t")
    (repo / "src.txt").write_text("v2\n", encoding="utf-8")       # the generator's input changed ...
    (repo / "gen.txt").write_text("v1\n", encoding="utf-8")       # ... and its output is stale
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", "row")
    sys.path.insert(0, str(HERE))
    import harness as h  # noqa: E402

    gen = [sys.executable, "-c", "import shutil; shutil.copy('src.txt', 'gen.txt')"]
    h.event = lambda *a, **k: None
    h.quality_problems = lambda cwd, base="main", **k: (
        "" if (Path(cwd) / "gen.txt").read_text() == (Path(cwd) / "src.txt").read_text() else "generated file drift")

    def run(regen, cmds):
        h.P["regen"] = cmds
        out, code = io.StringIO(), 0
        with contextlib.redirect_stdout(out):
            try:
                h.cmd_quality_check(argparse.Namespace(dir=str(repo), base="main", regen=regen))
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue()

    real_sh = h.sh
    h.sh = lambda c, **k: real_sh(gen if c == "GEN" else c, **k)
    code, out = run(False, ["GEN"])
    check("without --regen the stale generated file fails quality", code == 1 and "drift" in out, out)
    code, out = run(True, ["GEN"])
    check("--regen regenerates first, then quality passes", code == 0 and "QUALITY OK" in out, out)
    log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=repo, capture_output=True, text=True).stdout.strip()
    check("the regenerated files are committed as the train commits them", log == "train: regen", log)
    h.sh = lambda c, **k: subprocess.CompletedProcess(c, 3, "", "boom")
    code, out = run(True, ["FAIL"])
    check("a failing regen command fails the check with its text", code == 1 and "exited 3" in out, out)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
