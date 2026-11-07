"""Stand-in for land.py in test_drive.py: land.py's lock protocol, a merge, state.json and log.jsonl lines."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--repo", required=True)
ap.add_argument("--fn", required=True)
ap.add_argument("--main", default="main")
ap.add_argument("--polish", action="store_true")
a = ap.parse_args()
repo = Path(a.repo)
(repo / ".overhaul").mkdir(parents=True, exist_ok=True)
(repo / ".overhaul" / "land-argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
scenario = json.loads((Path(__file__).resolve().parent.parent / "scenario.json").read_text(encoding="utf-8"))
seq = scenario.get(a.fn, {}).get("land") or ["ok"]  # the Nth land of a function uses entry N, the last repeats
counter = repo / ".overhaul" / f"land-count-{a.fn}.txt"
calls = int(counter.read_text(encoding="utf-8")) + 1 if counter.exists() else 1
counter.write_text(str(calls), encoding="utf-8")
mode = seq[min(calls, len(seq)) - 1]
lock, end = repo / ".overhaul" / "lock", time.monotonic() + 5
while True:  # like land.py: O_EXCL create, wait a little, else one reason line
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"land.py pid {os.getpid()} since now\n".encode())
        os.close(fd)
        break
    except FileExistsError:
        if time.monotonic() > end:
            print("lock held: .overhaul/lock (" + lock.read_text().strip() + ")")
            sys.exit(1)
        time.sleep(0.1)


def log(event, detail):
    with (repo / ".overhaul" / "log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": "now", "fn": a.fn, "event": event, "detail": detail}) + "\n")


def git(*args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


try:
    if mode == "green_before":
        log("land_fail", "acceptance already green before change")
        print(a.fn + ": acceptance already green before change")
        sys.exit(1)
    if mode == "conflict":  # main moved under the branch: land.py logs its own land_fail and prints the reason
        log("land_fail", f"merge conflict merging {a.main} into fn/{a.fn}")
        print(f"merge conflict merging {a.main} into fn/{a.fn}")
        sys.exit(1)
    if mode == "die":  # exits without writing its own log line
        print(a.fn + ": land died")
        sys.exit(1)
    if mode == "slow":
        time.sleep(1.5)  # a long land keeps the lock this long
    git("merge", "--no-edit", "fn/" + a.fn)
    if a.polish:  # no FUNCTIONS row, no state.json entry
        log("polish_land_ok", "landed")
        print("landed " + a.fn)
        sys.exit(0)
    spec = repo / "spec" / "FUNCTIONS.md"
    text = re.sub(r"^(\| " + re.escape(a.fn) + r" \|.*)\| planned \|[ \t]*$", r"\1| proven |",
                  spec.read_text(encoding="utf-8"), flags=re.M)
    spec.write_text(text, encoding="utf-8", newline="\n")
    git("add", "spec/FUNCTIONS.md")
    git("commit", "-q", "-m", "land " + a.fn)
    sp = repo / ".overhaul" / "state.json"
    st = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {"cycle": 0, "functions": {}, "proven_history": []}
    st["functions"].setdefault(a.fn, {})["status"] = "proven"
    st["proven_history"].append(["now", sum(v.get("status") == "proven" for v in st["functions"].values())])
    sp.write_text(json.dumps(st), encoding="utf-8")
    log("land_ok", "landed")
    print("landed " + a.fn)
finally:
    lock.unlink(missing_ok=True)
