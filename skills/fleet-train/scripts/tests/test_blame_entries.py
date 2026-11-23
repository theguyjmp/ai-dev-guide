"""One shared red cause, every row of it in one step (harness.blame_entries). Replays a pilot run: a shrink-only list
test went red on a 20-row run; its panic printed outside the unit log's 6000-char tail, so blame had no assert diff,
named the whole crate, ejected an innocent row and bisected for 4 h. Now the red unit log carries the failing test's
captured block (unit_red_log), the assert diff names the entries, and every row whose own change includes one (the
entry's source, or its line in the list the test include_str!s) is rejected at once; rows lowering other lines of the
same list, or touching an unrelated file of the crate, gate on as ONE run. No assert diff: no group reject.
Temp repo and stubs only.

  py -3 tests/test_blame_entries.py
"""
from __future__ import annotations

import json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
FAILS: list[str] = []
TEST = "ext::tests::raw_access_outside_the_registry_only_shrinks"
LIST = "crates/doc/src/ext/raw_access.txt"
LINES = ["crates/draw/src/layer_set.rs 1", "crates/draw/src/drafting.rs 2", "crates/script/src/bake.rs 2",
         "crates/draw/src/records.rs 1", "crates/render/src/stage.rs 1", "crates/motion/src/data.rs 2",
         "crates/draw/src/style_set.rs 1", "crates/part/src/strip.rs 1"]
GATE_SRC = f"""/// The shrink-only list of raw accesses: `path count` per line.
const RAW_ACCESS: &str = include_str!("raw_access.txt");

#[test]
fn {TEST.rsplit('::', 1)[1]}() {{
    assert_eq!(scan(), app_core::source_scan::allow_list(RAW_ACCESS));
}}
"""


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def git(root, *a):
    return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def commit(root, files, msg):
    for f, text in files.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", msg)
    return git(root, "rev-parse", "HEAD")


def listed(*drop):
    return "\n".join(line for line in LINES if line.split()[0] not in drop) + "\n"


def cargo_red_output() -> str:
    """`cargo test -q --workspace` red in doc's lib: the panic block first, then other crates' passing output well past
    the 6000-char tail."""
    found = {line.split()[0]: int(line.split()[1]) for line in LINES}
    for gone in ("crates/draw/src/layer_set.rs", "crates/draw/src/style_set.rs"):  # DRAW1/2 moved them off
        del found[gone]
    found["crates/render/src/appearance.rs"] = 3  # SRC's new raw access
    allowed = {k: v for k, v in found.items() if k not in ("crates/render/src/appearance.rs",
                                                           "crates/script/src/bake.rs", "crates/render/src/stage.rs")}
    side = lambda m: "{" + ", ".join(f'"{k}": {v}' for k, v in sorted(m.items())) + "}"
    passing = "".join(f"\nrunning 40 tests\n{'.' * 40}\ntest result: ok. 40 passed; 0 failed; 0 ignored; 0 measured; "
                      f"0 filtered out; finished in 0.{i:02}s\n" for i in range(60))
    return (f"\nrunning 3 tests\n.F.\nfailures:\n\n---- {TEST} stdout ----\n\nthread '{TEST}' panicked at "
            f"crates\\doc\\src\\ext\\tests.rs:6:5:\nassertion `left == right` failed\n  left: {side(found)}\n"
            f" right: {side(allowed)}\nnote: run with `RUST_BACKTRACE=1` environment variable to display a backtrace\n\n\n"
            f"failures:\n    {TEST}\n\ntest result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; "
            f"finished in 0.01s\n\nerror: test failed, to rerun pass `-p app-doc --lib`\n{passing}\n"
            f"error: 1 target failed:\n    `-p app-doc --lib`\n")


with tempfile.TemporaryDirectory() as d:
    root, wt = Path(d) / "repo", Path(d) / "wt"
    root.mkdir()
    (Path(d) / "home" / "state").mkdir(parents=True)
    os.environ.update(HARNESS_HOME=str(Path(d) / "home"), HARNESS_WT=str(wt), HARNESS_REPO=str(root),
                      HARNESS_STACK="rust")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    commit(root, {LIST: listed(), "crates/doc/src/ext/tests.rs": GATE_SRC, "crates/doc/src/lib.rs": "lib",
                  "crates/draw/src/layer_set.rs": "m.extra 1", "crates/draw/src/style_set.rs": "m.extra 1",
                  "crates/render/src/stage.rs": "m.extra 1", "crates/output/src/a.rs": "a"}, "base")
    branches = {  # row: its own change, each on main
        "LIST1": {LIST: listed("crates/script/src/bake.rs")},  # lowers a line whose access is still there
        "DRAW1": {LIST: listed("crates/draw/src/layer_set.rs"), "crates/draw/src/layer_set.rs": "typed"},
        "SRC": {"crates/render/src/appearance.rs": "m.extra m.extra m.extra"},  # a new raw access, list untouched
        "CORE": {"crates/doc/src/lib.rs": "lib 2"},  # an unrelated file of the failing crate
        "LIST2": {LIST: listed("crates/render/src/stage.rs")},  # the second wrong edit of the same list
        "DRAW2": {LIST: listed("crates/draw/src/style_set.rs"), "crates/draw/src/style_set.rs": "typed"},
        "OUT": {"crates/output/src/a.rs": "a 2"},
    }
    batch = []
    for row, files in branches.items():
        git(root, "checkout", "-q", "-b", row.lower(), "main")
        batch.append({"row": row, "sha": commit(root, files, row)})
    git(root, "checkout", "-q", "main")
    sys.path.insert(0, str(HERE))
    import harness as h  # noqa: E402

    git(root, "worktree", "add", "-q", "--detach", str(h.TRAIN), "main")
    for q in batch:
        git(h.TRAIN, "merge", "-q", "--no-ff", "--no-edit", "-m", f"train: land {q['row']}", q["sha"])

    out = cargo_red_output()
    log = "failing crates: crates/doc/\n" + h.unit_red_log(out)  # gate(): the crate line sits above the red log
    check("the old 6000-char tail lost the assert sides", "left:" not in out[-6000:])
    entries = h.assert_diff_files(log)
    check("the red unit log carries the failing test's assert diff", entries == {
        "crates/render/src/appearance.rs", "crates/script/src/bake.rs", "crates/render/src/stage.rs"}, entries)
    check("the failing test's list is what it include_str!s", h.test_lists(["unit"], log) == {LIST},
          h.test_lists(["unit"], log))
    check("the red unit log still ends with the failures' names", log.rstrip().endswith(TEST), log[-200:])

    gated, verdicts = [], []
    h.quarantine = type("Q", (), {"heal": staticmethod(lambda *a: False)})()
    h.build_train = lambda b, rows: (list(b), [])
    h.train_spec = lambda rows: rows
    h.gate = lambda ids, rows, quick=False: (gated.append(sorted(ids)) or verdicts.pop(0))
    h.finish_spec = h.promote = lambda *a: None
    h.batch_review = lambda *a, **k: []
    h.save("queue.json", batch)
    rows = {q["row"]: {"id": q["row"], "tests": [], "file_list": [], "status": "cleanup", "deps": []} for q in batch}
    verdicts[:] = [(False, ["unit"], log), (True, [], "")]
    landed = h.try_batch(batch, rows, reviewed=True)

    events = [json.loads(line) for line in (h.ST / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    rejects = [e for e in events if e["event"] == "reject"]
    check("every row of the shared cause rejected in one step", sorted(e["row"] for e in rejects)
          == ["LIST1", "LIST2", "SRC"] and [e["event"] for e in events][:4] == ["reject"] * 3 + ["blame_entries"],
          [(e["event"], e.get("row")) for e in events])
    check("one reject reason for the group, naming the cause and the other rows",
          len({e["reason"] for e in rejects}) == 1 and all("bake.rs" in e["reason"] and "LIST2" in e["reason"]
                                                           for e in rejects), rejects[:1])
    check("the rest gated as ONE run, no split", gated[1:] == [["CORE", "DRAW1", "DRAW2", "OUT"]], gated)
    check("the rest landed", sorted(q["row"] for q in landed) == ["CORE", "DRAW1", "DRAW2", "OUT"])
    check("rejected rows leave the queue", sorted(q["row"] for q in h.load("queue.json", [])) == ["CORE", "DRAW1", "DRAW2", "OUT"])

    no_diff = chr(10).join(line for line in log.splitlines() if not line.lstrip().startswith(("left:", "right:")))
    check("no assert diff: no group", h.blame_entries(batch, ["unit"], no_diff)[0] == [])
    h.P["include_re"] = ""
    check("a profile without include_re reads no list: only rows touching an entry's own file",
          [q["row"] for q in h.blame_entries(batch, ["unit"], log)[0]] == ["SRC"])
    git(root, "worktree", "remove", "--force", str(h.TRAIN))

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
