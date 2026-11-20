"""One owner for the duplicate review's input: added_functions lists what a tree adds (tests left out), review_prompt
renders the reviewer's text, batch_review sends exactly that text, and `review-prompt` prints it (nothing when no
function is added). Temp repo and state only; the reviewer binary is stubbed.

  py -3 tests/test_review_prompt.py
"""
from __future__ import annotations

import argparse, contextlib, io, json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
FAILS: list[str] = []


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def g(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def commit(repo, files, msg):
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
                      HARNESS_STACK="node")
    (root / "home" / "state").mkdir(parents=True)
    g(repo, "init", "-q", "-b", "main")
    g(repo, "config", "user.email", "t@t")
    g(repo, "config", "user.name", "t")
    commit(repo, {"lib/math.js": "export function clamp(x, a, b) { return Math.min(b, Math.max(a, x)); }\n"}, "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    sys.path.insert(0, str(HERE))
    import harness as h  # noqa: E402

    def printed(**kw) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            h.cmd_review_prompt(argparse.Namespace(**{"dir": str(repo), "base": base, **kw}))
        return out.getvalue()

    check("no added function: added list empty, prompt empty, command prints nothing",
          h.added_functions(repo, base) == [] and h.review_prompt([]) == "" and printed() == "")

    g(repo, "checkout", "-qb", "row")
    commit(repo, {"kit/wheel.js": "export function num(v, d) {\n  return Number.isFinite(v) ? v : d;\n}\n"
                                  "const half = (x) => x / 2;\n",
                  "kit/test/wheel.test.mjs": "function helperInTest() {}\n"}, "row")
    added = h.added_functions(repo, base)
    check("added functions listed with file and signature, test files left out",
          added == ["kit/wheel.js: export function num(v, d) {", "kit/wheel.js: const half = (x) => x / 2;"], added)
    text = h.review_prompt(added)
    check("prompt carries every added function", all(a in text for a in added) and "duplicate" in text, text[:200])
    check("review-prompt prints exactly review_prompt(added_functions)", printed() == text)
    check("review-prompt with --base defaulting to main sees the same diff", printed(base="") == text)

    sent, real_sh = {}, h.sh

    def fake_sh(cmd, cwd=None, check=True, env=None, timeout=None, stdin=None):
        if cmd[0] != h.CLAUDE_BIN:  # git and everything else run for real; only the reviewer is stubbed
            return real_sh(cmd, cwd=cwd, check=check, env=env, timeout=timeout, stdin=stdin)
        sent["stdin"] = stdin
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": '{"duplicates": []}'}), "")

    h.sh, h.event = fake_sh, lambda *a, **k: None
    check("batch_review sends the same prompt and parses the answer",
          h.batch_review([], cwd=repo, base=base) == [] and sent.get("stdin") == text)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
