"""finish.py, the lane finisher that mirrors the train's gate before a submit: it waits while the running train gates
the row (a resubmit supersedes the whole run), pins the tests that quote a changed or moved file or a directory
holding one, scopes unit and lint to the touched crates, and runs every build after the train's compile stage inside
a machine-wide build slot. The gate marks its compile stages for it. Temp repo and fakes only.

  py -3 tests/test_finish.py
"""
from __future__ import annotations

import inspect, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
FAILS: list[str] = []


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def g(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, files, msg="c"):
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", msg)


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    repo = root / "repo"
    repo.mkdir()
    os.environ.update(HARNESS_HOME=str(root / "home"), HARNESS_WT=str(root / "wt"), HARNESS_REPO=str(repo),
                      HARNESS_STACK="rust", HARNESS_CRATE="app")
    (root / "home" / "state").mkdir(parents=True)
    sys.path.insert(0, str(HERE))
    import cargo_slots  # noqa: E402
    import finish as f  # noqa: E402
    h = f.h

    # ---- the running train holds the row
    start = {"event": "train_start", "rows": ["CLEAN-a", "R1"]}
    check("held while the running train gates it", f.train_holds([start], "CLEAN-a"))
    check("free once that train ended", not f.train_holds([start, {"event": "train_end"}], "CLEAN-a"))
    check("free once that train rejected it", not f.train_holds([start, {"event": "reject", "row": "CLEAN-a"}], "CLEAN-a"))
    check("a reject from an older train does not count",
          f.train_holds([{"event": "reject", "row": "CLEAN-a"}, start], "CLEAN-a"))
    check("free when the train gates other rows", not f.train_holds([start], "CLEAN-b"))
    check("free with no train yet", not f.train_holds([], "CLEAN-a"))

    # ---- pins: a test quoting a changed or moved file, or a directory holding one
    g(repo, "init", "-q", "-b", "main")
    g(repo, "config", "user.email", "t@t")
    g(repo, "config", "user.name", "t")
    commit(repo, {"crates/geo/src/old.rs": "x", "crates/geo/src/lib.rs": "l", "crates/ui/src/lib.rs": "u",
                  "tests/acceptance/moved_pin.rs": 'read("crates/geo/src/old.rs")',
                  "tests/acceptance/scan_dir.rs": 'scan("crates/geo/src")',
                  "tests/acceptance/scan_crate.rs": 'scan("crates/geo/")',
                  "tests/acceptance/root_only.rs": 'scan("crates")',
                  "tests/acceptance/other.rs": 'read("crates/ui/src/lib.rs")'})
    g(repo, "checkout", "-q", "-b", "lane")
    g(repo, "mv", "crates/geo/src/old.rs", "crates/geo/src/new.rs")
    commit(repo, {"crates/geo/src/lib.rs": "l2"})
    files = f.changed(repo, "main")
    check("changed files include a moved file's old path", "crates/geo/src/old.rs" in files and "crates/geo/src/new.rs" in files, files)
    pins = f.pinned_tests(repo, files)
    check("pins: the moved file's path pin and both source scans of the touched crate, nothing else",
          pins == ["tests/acceptance/moved_pin.rs", "tests/acceptance/scan_crate.rs", "tests/acceptance/scan_dir.rs"], pins)
    check("crates scope: one -p per touched crate, the root package for src/ and tests/",
          f.scope_pkgs(["crates/geo/src/a.rs", "tests/acceptance/x.rs", "spec/FUNCTIONS.md"]) == "-p app -p app-geo")

    # ---- every build waits out the train's compile stage, then holds a build slot
    sleeps, stage = [], ["unit", "unit", ""]
    slots = cargo_slots.CargoSlots(lambda pid, start: h.pid_alive(pid), path=root / "slots.json", cap=1)
    rc = f.run(f'"{sys.executable}" -c "print(42)"', repo, root / "logs" / "one.txt", slots,
               sleep=sleeps.append, compiling=lambda: stage.pop(0))
    check("waits while the train compiles, then runs", rc == 0 and sleeps == [f.POLL, f.POLL]
          and (root / "logs" / "one.txt").read_text().strip() == "42", (rc, sleeps))

    class Busy:
        tries = 0

        def claim(self, fleet, tag, spawn):
            Busy.tries += 1
            if Busy.tries < 3:
                raise cargo_slots.Full("2/2")
            return spawn()
    sleeps.clear()
    rc = f.run(f'"{sys.executable}" -c "raise SystemExit(3)"', repo, root / "logs" / "two.txt", Busy(),
               sleep=sleeps.append, compiling=lambda: "")
    check("a full machine waits for a slot, and the exit code is the command's", rc == 3 and sleeps == [15, 15], (rc, sleeps))

    # ---- the gate marks the compile stages finish.py yields to
    src = inspect.getsource(h.gate)
    check("the gate marks each compile stage", all(f'train_stage("{s}")' in src for s in f.COMPILE_STAGES), f.COMPILE_STAGES)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
