"""One owner for the unit verdict: unit_verdict is the gate's rule (green when the run passed, when every failure is
main's own, or when each other failure passes alone), and `unit-baseline` + `unit-check` apply exactly that rule to a
tree outside the train (a cloud builder's scratch merge): RED <names> and exit 1, MAIN-RED <names> (never blocking),
UNIT OK. Temp repo and state only; the unit command is a fake that fails the tests a tree lists in fails.txt.

  py -3 tests/test_unit_check.py
"""
from __future__ import annotations

import argparse, contextlib, io, json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
FAILS: list[str] = []
FAKE = r'''import sys
from pathlib import Path
names = [n for n in Path("fails.txt").read_text().split("\n") if n] if Path("fails.txt").exists() else []
alone = [n for n in Path("alone.txt").read_text().split("\n") if n] if Path("alone.txt").exists() else []
if len(sys.argv) > 1:  # one test alone (unit_one): fails only if the tree lists it in alone.txt
    sys.exit(1 if sys.argv[1] in alone else 0)
for i, n in enumerate(names, 1):
    print(f"not ok {i} - {n}")
print("garbled" if "GARBLED" in names else "")
sys.exit(1 if names else 0)
'''


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def g(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, files, msg):
    for rel, text in files.items():
        (repo / rel).write_text(text, encoding="utf-8")
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "--allow-empty", "-m", msg)


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    repo, home = root / "repo", root / "home"
    repo.mkdir()
    (home / "state").mkdir(parents=True)
    py = sys.executable.replace("\\", "/")
    (home / "harness.toml").write_text(f'[profile]\nunit = "{py} fake_unit.py"\nunit_one = "{py} fake_unit.py \\"{{name}}\\""\n'
                                       'name_escape = false\n', encoding="utf-8")
    os.environ.update(HARNESS_HOME=str(home), HARNESS_WT=str(root / "wt"), HARNESS_REPO=str(repo), HARNESS_STACK="node")
    g(repo, "init", "-q", "-b", "main")
    g(repo, "config", "user.email", "t@t")
    g(repo, "config", "user.name", "t")
    commit(repo, {"fake_unit.py": FAKE}, "base")
    sys.path.insert(0, str(HERE))
    import harness as h  # noqa: E402
    h.event = lambda *a, **k: None

    def run(cmd, **kw):
        out, code = io.StringIO(), 0
        with contextlib.redirect_stdout(out):
            try:
                cmd(argparse.Namespace(**kw))
            except SystemExit as e:
                code = e.code or 0
        return out.getvalue(), code

    def tree(fails=(), alone=()):
        commit(repo, {"fails.txt": "\n".join(fails) + "\n", "alone.txt": "\n".join(alone) + "\n"}, "state")
        return str(repo)

    base_file = str(root / "base.json")
    text, code = run(h.cmd_unit_baseline, dir=tree(), out=base_file)
    data = json.loads(Path(base_file).read_text(encoding="utf-8"))
    check("unit-baseline writes main's sha and its (no) failures", code == 0 and data == {"sha": g(repo, "rev-parse", "HEAD"),
                                                                                           "fails": []}, text)
    text, code = run(h.cmd_unit_check, dir=tree(), baseline=base_file)
    check("CLEAN: a passing tree is UNIT OK, exit 0", code == 0 and text.strip().endswith("UNIT OK"), text)

    text, code = run(h.cmd_unit_check, dir=tree(["adds a wheel"], ["adds a wheel"]), baseline=base_file)
    check("RED: a test main passes fails here and alone, RED <name>, exit 1",
          code == 1 and text.strip().splitlines()[-1] == "RED adds a wheel", text[-300:])

    text, code = run(h.cmd_unit_check, dir=tree(["slow under load"], []), baseline=base_file)
    check("flaky: a failure that passes alone is not the row's (the gate's unit_flaky rule)",
          code == 0 and text.strip().endswith("UNIT OK"), text[-300:])

    run(h.cmd_unit_baseline, dir=tree(["main is red"], ["main is red"]), out=base_file)
    text, code = run(h.cmd_unit_check, dir=tree(["main is red"], ["main is red"]), baseline=base_file)
    check("MAIN-RED: main's own failure is printed and never blocks",
          code == 0 and text.startswith("MAIN-RED main is red") and text.strip().endswith("UNIT OK"), text[-300:])

    text, code = run(h.cmd_unit_check, dir=tree(["main is red", "row broke it"], ["main is red", "row broke it"]),
                     baseline=base_file)
    check("RED names only the row's failure when main is red too",
          code == 1 and text.strip().splitlines()[-1] == "RED row broke it", text[-300:])

    bad = subprocess.CompletedProcess("unit", 1, "crashed before any test ran", "")
    check("unit_verdict: a failed run that names no test is red", h.unit_verdict(bad, lambda: h.cf.from_keys([]))[0])
    asked = []
    ok = subprocess.CompletedProcess("unit", 0, "", "")
    check("unit_verdict: a passing run is green and never asks for main's baseline",
          h.unit_verdict(ok, lambda: asked.append(1))[0] is False and not asked)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
