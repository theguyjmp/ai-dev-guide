"""Shrink-only lists only shrink: after a land the train runs the profile's `shrink_cmd` and lowers every tracked
`.txt` list it prints between the SHRINK marks (tighten_shrink_lists), never raising a count or adding a line; a red
run, output without marks, an untracked name or a land touching no product source leaves every list as it is.
Temp repo and state only; the product's test command is faked.

  py -3 tests/test_shrink_lists.py
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


def commit(repo, files):
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(text.encode())
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", "c")


KEYS = "# head\na.rs 3\nb.rs 1\nc.rs 2\n"
LOOSE = "x.rs 9\n"
CMD = "PRODUCT-SHRINK-TESTS"

with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    repo = root / "repo"
    repo.mkdir()
    os.environ.update(HARNESS_HOME=str(root / "home"), HARNESS_WT=str(root / "wt"), HARNESS_REPO=str(repo),
                      HARNESS_STACK="rust")
    (root / "home" / "state").mkdir(parents=True)
    g(repo, "init", "-q", "-b", "main")
    g(repo, "config", "user.email", "t@t")
    g(repo, "config", "user.name", "t")
    g(repo, "config", "core.autocrlf", "false")
    commit(repo, {"src/keys_left.txt": KEYS, "crates/b/src/b_left.txt": "x.rs 2\ny.rs 1\n", "spec/notes.md": "n\n"})
    before = g(repo, "rev-parse", "HEAD")
    commit(repo, {"crates/b/src/lib.rs": "fn f() {}\n"})
    (repo / "loose.txt").write_text(LOOSE)  # printed but untracked: never written
    sys.path.insert(0, str(HERE))
    import harness as h  # noqa: E402

    h.event = lambda *a, **k: None
    real_sh, ran = h.sh, []

    def fake(out, rc=0):
        def run(cmd, **k):
            if cmd != CMD:
                return real_sh(cmd, **k)
            ran.append(cmd)
            return subprocess.CompletedProcess(cmd, rc, out, "")
        return run

    B, E = "--- tightened {} ---", h.SHRINK_END
    PRINTED = (f"running 13 tests\n..{B.format('src/keys_left.txt')}\n# head\na.rs 1\nc.rs 5\nnew.rs 2\n{E}\n"
               f".{B.format('crates/b/src/b_left.txt')}\nx.rs 1\ny.rs 1\n{E}\n{B.format('loose.txt')}\n{E}\n"
               f"{B.format('an-in-source-list')}\n{E}\ntest result: ok\n")
    h.P["shrink_cmd"] = ""
    h.sh = fake(PRINTED)
    check("off by default: no shrink_cmd runs nothing", h.tighten_shrink_lists(repo, before) == [] and ran == [], ran)
    h.P["shrink_cmd"] = CMD
    cut = h.tighten_shrink_lists(repo, before)
    check("a land lowers every printed list to its counts, never raising one (progress dots kept apart)",
          (repo / "src/keys_left.txt").read_bytes() == b"# head\na.rs 1\nc.rs 2\n"
          and (repo / "crates/b/src/b_left.txt").read_bytes() == b"x.rs 1\ny.rs 1\n"
          and cut == ["src/keys_left.txt: a.rs 3->1", "src/keys_left.txt: b.rs 1->0",
                      "crates/b/src/b_left.txt: x.rs 2->1"], (cut, ran))
    check("a printed name that is not a tracked .txt list is never written",
          (repo / "loose.txt").read_text() == LOOSE and not (repo / "an-in-source-list").exists())
    for name, f in (("a red product test", fake(PRINTED, rc=101)), ("output without the marks", fake("running 0\n"))):
        (repo / "src/keys_left.txt").write_bytes(KEYS.encode())
        h.sh = f
        check(f"{name} leaves every list as it is",
              h.tighten_shrink_lists(repo, before) == [] and (repo / "src/keys_left.txt").read_bytes() == KEYS.encode())
    g(repo, "checkout", "--", ".")
    before = g(repo, "rev-parse", "HEAD")
    commit(repo, {"spec/notes.md": "z\n"})
    ran.clear()
    h.sh = fake(PRINTED)
    check("a land touching no product source or list runs nothing",
          h.tighten_shrink_lists(repo, before) == [] and ran == [], ran)
    h.sh = real_sh
    src = inspect.getsource(h.finish_spec)
    check("finish_spec lowers the lists on the proven tree before its commit",
          src.index("tighten_shrink_lists(TRAIN") < src.index('"commit"'))
    check("a CRLF list keeps its line endings",
          h.lowered_list("a.rs 3\r\n# c\r\nb.rs 2\r\n", "a.rs 1\nb.rs 2\n") == ("a.rs 1\r\n# c\r\nb.rs 2\r\n", ["a.rs 3->1"]))
    check("printed counts above the list never raise it", h.lowered_list("a.rs 1\n", "a.rs 4\nz.rs 1\n") == ("a.rs 1\n", []))

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
