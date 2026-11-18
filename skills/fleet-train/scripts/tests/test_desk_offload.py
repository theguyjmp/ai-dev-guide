"""Desk offload (any runner but cargo): with [desk] host in harness.toml every test run of the train goes to the desk
(the gate's unit, full and quick, the main unit baseline, unit_flaky reruns, acceptance, probe, run_alone) and none runs
here; no desk verdict raises desk.Unavailable and the train defers; the desk side runs each op here, never over ssh;
env beats harness.toml beats the defaults; pick_run never starts while the train holds the desk. No network.

  py -3 tests/test_desk_offload.py
"""
from __future__ import annotations

import json, os, subprocess, sys, tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="test-desk-offload-"))
(TMP / "home").mkdir(parents=True)
(TMP / "home" / "harness.toml").write_text(
    "stack = \"node\"\n\n[desk]\nhost = \"desk-test\"\nroot = 'X:\\d'\npath = ['X:\\d\\node22']\n", encoding="utf-8")
os.environ.update({"HARNESS_HOME": str(TMP / "home"), "HARNESS_REPO": str(TMP / "repo"), "HARNESS_WT": str(TMP / "wt")})
for k in [k for k in os.environ if k.startswith(("HARNESS_REMOTE_", "HARNESS_DESK", "HARNESS_STACK"))]:
    os.environ.pop(k)
sys.path.insert(0, str(HERE))
import desk  # noqa: E402
import harness as h  # noqa: E402
import quarantine  # noqa: E402

FAILS = []
SHA = "abc123def4567890"
T = [f"test/k{i}.test.mjs::case {i}" for i in range(6)]
LOCAL, OPS, REPLIES = [], [], []  # test commands run here; desk jobs (op, spec); queued desk results


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *([] if ok else info))
    if not ok:
        FAILS.append(name)


def fake_sh(cmd, cwd=None, check=True, env=None, timeout=None, stdin=None):
    text = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
    if "node" in text or "cargo" in text:
        LOCAL.append(text)
    return subprocess.CompletedProcess(cmd, 0, "", "")


def fake_remote(h_, rid, sha, spec, limit, alive=lambda: True, poll_secs=0):
    OPS.append((spec["op"], spec))
    return {"rid": rid, "head": sha, "secs": 1, "send_secs": 0, **(REPLIES.pop(0) if REPLIES else {})}


def setup():
    h.sh, h.git = fake_sh, (lambda *a, **k: SimpleNamespace(stdout=SHA + "\n", returncode=0, stderr=""))
    desk.remote = fake_remote
    LOCAL.clear(), OPS.clear(), REPLIES.clear()


def test_config():
    check("harness.toml [desk]: host, root, node path first on PATH", desk.HOST == "desk-test" and desk.ROOT == "X:\\d"
          and 'set "PATH=X:\\d\\node22;%PATH%"' in desk.ENV and desk.KIT == "X:\\d\\desk-kit", desk.CONF)
    over = desk.load_conf({"HARNESS_REMOTE_HOST": "envhost", "HARNESS_REMOTE_ENV": "set A=1"}, TMP / "home")
    check("env beats harness.toml", over.host == "envhost" and over.env.startswith("set A=1") and over.root == "X:\\d")
    bare = desk.load_conf({}, TMP / "no-toml")
    check("no [desk] table: the old defaults, off", (bare.host, bare.root, bare.env, bare.configured)
          == ("remote-host", "D:\\fleet", "set CARGO_BUILD_JOBS=3", False), bare)
    check("runner script carries the pinned PATH", "node22;%PATH%" in desk.runner_scripts(h)["run.cmd"])
    check("a configured host is on without state/desk-on", desk.on(h.ST) and desk.offload(h))
    os.environ["HARNESS_DESK"] = "0"
    check("HARNESS_DESK=0 turns it off", not desk.on(h.ST) and not desk.offload(h))
    os.environ.pop("HARNESS_DESK")
    keep, h.P["accept"] = h.P["accept"], "cargo"
    check("cargo runner never offloads (it splits acceptance only)", not desk.offload(h))
    h.P["accept"] = keep


def test_every_path_offloads():
    setup()
    REPLIES.append({"fails": [T[1]], "log": "desk log " + T[1]})
    fails = h.acceptance_tests(T)
    check("acceptance: the desk's fails and log, nothing here", fails == [T[1]] and not LOCAL and OPS[0][0] == "tests"
          and OPS[0][1]["tests"] == sorted(T) and "desk log" in (h.ST / "last-gate.txt").read_text(encoding="utf-8"), OPS)
    setup()
    for p in (h.ST / "unit-baseline.json",):
        p.unlink(missing_ok=True)
    h.fmt_tree, h.regen_tree, h.quality_problems = (lambda cwd: None), (lambda cwd: ""), (lambda *a, **k: "")
    h.gate_tests = lambda new_rows, rows: list(T)
    REPLIES.extend([{"runs": [{"rc": 1, "out": "not ok 1 - flaky one\n"}]},  # gate unit: red
                    {"runs": [{"rc": 0, "out": "ok 1 - flaky one\n"}]},  # main baseline: green
                    {"runs": [{"rc": 0, "out": "ok 1 - flaky one\n"}]},  # unit_one rerun: passes alone
                    {"fails": [], "log": ""}])  # acceptance
    ok, fails, _ = h.gate(["R1"], {})
    check("full gate: unit, baseline, flaky rerun and acceptance all on the desk", ok and [o for o, _ in OPS]
          == ["cmd", "cmd", "cmd", "tests"] and OPS[2][1]["first_fail"] and "flaky" in OPS[2][1]["cmds"][0]
          and not LOCAL, OPS, LOCAL)
    setup()
    REPLIES.extend([{"runs": [{"rc": 1, "out": "not ok 1 - other one\n"}]},  # quick gate unit: red, baseline cached
                    {"runs": [{"rc": 1, "out": "not ok 1 - other one\n"}]}])  # red alone too
    ok, fails, log = h.gate(["R1"], {}, quick=True)
    check("quick gate: a red unit stays red, from the desk", not ok and fails == ["unit"] and "other one" in log
          and [o for o, _ in OPS] == ["cmd", "cmd"] and not LOCAL, OPS)
    setup()
    real_build, h.build_train = h.build_train, (lambda half, rows: (list(half), []))
    REPLIES.extend([{"fails": [T[0]]}, {"fails": []}, {"fails": [T[0]]}])
    qs = [{"row": f"R{i}", "sha": f"s{i}"} for i in range(4)]
    bad = h.probe_culprit(qs, {}, [T[0]])
    h.build_train = real_build
    check("probe bisect runs on the desk", bad == qs[1] and {o for o, _ in OPS} == {"tests"} and not LOCAL, bad, OPS)
    setup()
    REPLIES.append({"red": {T[2]: "tail"}})
    red = quarantine.main_red(h, T[:3])
    check("run_alone (quarantine's main baseline) on the desk", red == {T[2]: "tail"} and OPS[0][0] == "alone"
          and not LOCAL, red, OPS)


def test_no_verdict_defers():
    setup()

    def down(*a, **k):
        raise desk.Unavailable("desk-test unreachable")
    desk.remote = down
    try:
        h.run_tests(T)
        raised = False
    except desk.Unavailable:
        raised = True
    check("desk down: Unavailable, never a pass or a local run", raised and not LOCAL)
    check("the HELD mark is gone and the error is an event", not (h.ST / desk.HELD).exists()
          and any(json.loads(x)["event"] == "desk_error" for x in (h.ST / "events.jsonl").read_text().splitlines()))
    real, h.train_once = h.train_once, down
    h.cmd_train(SimpleNamespace(loop=False))
    h.train_once = real
    last = json.loads((h.ST / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    check("the train defers the batch (desk_deferred, not train_error)", last["event"] == "desk_deferred", last)


def test_transport():
    setup()
    desk.remote = REAL_REMOTE
    said = []
    result = {"head": SHA, "fails": [], "tests": T}

    def ssh(h_, cmd, timeout=180):
        said.append(cmd)
        out = ("ok" if cmd == "echo ok" else "pid 7 rc 0" if "start.ps1" in cmd
               else json.dumps(result) if cmd.startswith("type ") and result else "")
        return SimpleNamespace(stdout=out, stderr="", returncode=0)
    desk.ssh, desk.scp = ssh, (lambda h_, src, dst, timeout=600: True)
    desk.send, desk.kit = (lambda h_, sha: ""), (lambda h_: "")
    res = desk.job(h, "tests", 60, tests=T, solo=[])
    spec = json.loads(next((h.ST / "desk").glob("tests-*.json")).read_text(encoding="utf-8"))
    check("job: spec sent with its op at the train HEAD, result back", res["head"] == SHA and spec["op"] == "tests"
          and spec["sha"] == SHA and not (h.ST / desk.HELD).exists(), res, spec)
    result["head"] = "other"
    try:
        desk.job(h, "tests", 60, tests=T, solo=[])
        raised = False
    except desk.Unavailable as e:
        raised = "desk head" in str(e)
    check("another head is no verdict", raised)
    result.clear()
    said.clear()
    try:
        desk.remote(h, "r-late", SHA, {}, 0, poll_secs=0)
        raised = False
    except desk.Unavailable:
        raised = True
    check("late: stopped on the desk and no verdict", raised and any("stop.cmd r-late" in c for c in said), said)
    h.proc_start = lambda pid: "start"
    h.save(desk.HELD, {"pid": os.getpid(), "rid": "tests-x"})
    try:
        desk.pick_run(h, SHA, T)
        raised = ""
    except h.NoVerdict as e:
        raised = str(e)
    (h.ST / desk.HELD).unlink()
    check("pick_run waits while the train holds the desk", "holds the desk" in raised, raised)


def test_desk_side():
    setup()

    def no_ssh(*a, **k):
        raise AssertionError("the desk side called ssh")
    desk.ssh, desk.scp = no_ssh, no_ssh
    desk_runs = []

    def sh_here(cmd, cwd=None, check=True, env=None, timeout=None, stdin=None):
        desk_runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "not ok 1 - x\n", "")
    h.sh = sh_here
    runs = h.HOME / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    specs = {"d-cmd": {"op": "cmd", "sha": SHA, "cmds": ["node --test a", "node --test b"], "timeout": 9,
                       "first_fail": True},
             "d-alone": {"op": "alone", "sha": SHA, "tests": ["test/a.test.mjs::x"]},
             "d-bad": {"op": "nope", "sha": SHA}}
    for rid, spec in specs.items():
        (runs / f"{rid}.json").write_text(json.dumps(spec), encoding="utf-8")
    res = desk.accept(h, "d-cmd")
    check("desk side cmd: run here, stops at the first red, full output back", res["runs"] == [
        {"rc": 1, "out": "not ok 1 - x\n"}] and desk_runs == ["node --test a"] and "not ok" in res["log"]
          and os.environ.get("HARNESS_DESK") == "0", res)
    res = desk.accept(h, "d-alone")
    check("desk side alone: run_alone here", list(res.get("red", {})) == ["test/a.test.mjs::x"], res)
    res = desk.accept(h, "d-bad")
    check("desk side: an unknown op is an error (no verdict)", "unknown desk op" in res.get("error", ""), res)
    os.environ.pop("HARNESS_DESK")


REAL_REMOTE = desk.remote


def main():
    test_config()
    test_every_path_offloads()
    test_no_verdict_defers()
    test_transport()
    test_desk_side()
    print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
