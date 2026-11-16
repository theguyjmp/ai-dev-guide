"""run_tests_cargo scheduling: binaries start longest first by their recorded times; a test with a recent
perf-budget miss skips the parallel phase and runs alone only, with the same verdict; every run records its split.

Usage: py -3 tests/test_accept_order.py [path/to/harness.py]   (a copy under test, per the freeze rule)"""
from __future__ import annotations

import importlib.util, json, os, sys, tempfile, threading
from pathlib import Path
from types import SimpleNamespace

HOME = Path(__file__).resolve().parent.parent
HARNESS = Path(sys.argv[1]) if len(sys.argv) > 1 else HOME / "harness.py"
sys.path.insert(0, str(HOME))
FAILS: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {detail}"))
    if not ok:
        FAILS.append(name)


def load_harness(root: Path):
    os.environ.update(HARNESS_HOME=str(root), HARNESS_REPO=str(root), HARNESS_WT=str(root / "wt"))
    (root / "state").mkdir(exist_ok=True)
    spec = importlib.util.spec_from_file_location("harness_accept_order", HARNESS)
    h = importlib.util.module_from_spec(spec)
    sys.modules["harness_accept_order"] = h
    spec.loader.exec_module(h)
    return h


def fake_runner(h, alone: dict, parallel_rc: dict):
    """cargo builds every stem; a parallel run of stem s exits parallel_rc.get(s, 0); an alone run of test name n
    prints alone[n] = (rc, out). Records the order binaries start and every alone run."""
    started, ran_alone, mu = [], [], threading.Lock()

    def sh(cmd, cwd=None, check=True, env=None, timeout=None, stdin=None):
        if cmd[0] == "cargo":
            stems = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--test"]
            lines = [json.dumps({"executable": f"bin/{s}", "target": {"name": s, "kind": ["test"]}}) for s in stems]
            return SimpleNamespace(returncode=0, stdout="\n".join(lines), stderr="")
        stem = cmd[0].split("/")[-1]
        if cmd[2] == "--test-threads=1":
            ran_alone.append(f"{stem}::{cmd[-1]}")
            rc, out = alone.get(cmd[-1], (0, "ok"))
            return SimpleNamespace(returncode=rc, stdout=out, stderr="")
        with mu:
            started.append(stem)
        rc = parallel_rc.get(stem, 0)
        out = "".join(f"test {n} ... FAILED\n" for n in cmd[3:]) if rc else "ok"
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")
    h.sh = sh
    return started, ran_alone


T = lambda stem, name: f"tests/acceptance/{stem}.rs::{name}"

with tempfile.TemporaryDirectory() as d:
    h = load_harness(Path(d))
    h.save(h.ACCEPT_TIMES, {"r_fast": 1.0, "s_slow": 160.0, "r_mid": 20.0})
    order = h.longest_first({"r_fast", "s_slow", "r_mid", "r_new"}, h.load(h.ACCEPT_TIMES, {}))

    # 1. longest first, unknown (a new row's binary) before all; the split event and per-binary times are recorded
    started, alone = fake_runner(h, {}, {})
    tests = [T("r_fast", "test_a"), T("s_slow", "test_b"), T("r_mid", "test_c"), T("r_new", "test_d")]
    fails = h.run_tests_cargo(tests)
    check("green batch passes", fails == [], fails)
    check("unknown first, then longest first", order == ["r_new", "s_slow", "r_mid", "r_fast"], order)
    check("every binary ran in the parallel phase", sorted(started) == ["r_fast", "r_mid", "r_new", "s_slow"], started)
    times = h.load(h.ACCEPT_TIMES, {})
    check("per-binary times kept for every binary run", set(times) >= {"r_fast", "s_slow", "r_mid", "r_new"}, times)
    ev = [json.loads(x) for x in (Path(d) / "state" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    split = [e for e in ev if e.get("event") == "accept_split"]
    check("accept_split event has build/parallel/serial/alone", split and {"build", "parallel", "serial", "alone"}
          <= set(split[-1]), split)

    # 2. a recent perf-budget miss: never in the parallel phase, run alone once; its alone verdict decides
    with open(Path(d) / "state" / "perf-debt.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": h.iso(), "test": T("s_slow", "test_b"), "out": "over budget"}) + "\n")
        fh.write(json.dumps({"t": "2020-01-01T00:00:00Z", "test": T("r_mid", "test_c"), "out": "over budget"}) + "\n")
    started, alone = fake_runner(h, {"test_b": (0, "ok")}, {})
    fails = h.run_tests_cargo(tests)
    check("recent perf miss skips the parallel phase", "s_slow" not in started and "r_mid" in started, started)
    check("an old perf miss stays parallel; the recent one runs alone exactly once", alone == ["s_slow::test_b"], alone)
    check("solo test green alone: batch passes", fails == [], fails)
    ev = [json.loads(x) for x in (Path(d) / "state" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    last = [e for e in ev if e.get("event") == "accept_split"][-1]
    check("the alone run is itemised with its seconds", [k for k, _ in last["alone"]] == ["s_slow.rs::test_b"], last)

    started, alone = fake_runner(h, {"test_b": (101, "test test_b ... FAILED\nassert left == right")}, {})
    fails = h.run_tests_cargo(tests)
    check("solo test red alone: the batch fails on it (same verdict as a failed retry)", fails == [T("s_slow", "test_b")],
          fails)

    # 3. a parallel failure is still retried alone and still fails when red alone
    started, alone = fake_runner(h, {"test_a": (101, "test test_a ... FAILED\nboom")}, {"r_fast": 101})
    fails = h.run_tests_cargo(tests)
    check("parallel red retried alone, red alone fails", fails == [T("r_fast", "test_a")] and T("r_fast", "test_a").split(
        "/")[-1].replace(".rs", "") in alone, (fails, alone))

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
