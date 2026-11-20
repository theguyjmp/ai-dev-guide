"""sh() on a timeout kills the child's whole tree through kill_tree: on POSIX the child leads its own session and the
group goes (a grandchild holding the pipes open would otherwise hang the caller until it exits), on Windows taskkill /T.
POSIX-only checks run on POSIX (CI, WSL, a cloud sandbox); the Windows path keeps its one taskkill call.

  python3 tests/test_kill_tree.py
"""
from __future__ import annotations

import os, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
FAILS: list[str] = []


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # default: it exists but belongs to someone else
        return True
    return True


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    (root / "home" / "state").mkdir(parents=True)
    (root / "home" / "harness.toml").write_text("[profile]\n", encoding="utf-8")
    (root / "repo").mkdir()
    os.environ.update(HARNESS_HOME=str(root / "home"), HARNESS_WT=str(root / "wt"), HARNESS_REPO=str(root / "repo"),
                      HARNESS_STACK="node")
    sys.path.insert(0, str(HERE))
    import harness as h  # noqa: E402

    h.kill_tree(2 ** 22 + 12345)  # a pid nobody has: no exception
    check("kill_tree on a pid that is already gone does not raise", True)

    if os.name != "nt":
        pidfile = root / "grandchild.pid"
        t0 = time.time()
        try:
            h.sh(f"sleep 300 & echo $! > {pidfile}; wait", cwd=root, check=False, timeout=2)
            check("a command over its timeout raises TimeoutExpired", False)
        except subprocess.TimeoutExpired:
            check("a command over its timeout raises TimeoutExpired", True)
        secs = time.time() - t0
        check("the caller is back within seconds, not when the grandchild ends", secs < 30, f"{secs:.1f}s")
        gpid = int(pidfile.read_text().strip())
        for _ in range(50):
            if not alive(gpid):
                break
            time.sleep(0.1)
        check("the grandchild (same session) is killed with the child", not alive(gpid), gpid)

        lone = subprocess.Popen(["sleep", "300"])  # not a group leader: kill_tree kills just that pid
        h.kill_tree(lone.pid)
        lone.wait(timeout=10)
        check("a pid that leads no group is killed alone", lone.returncode is not None)

        ok = h.sh(["true"], cwd=root, check=False, timeout=30)
        check("a fast command is unaffected", ok.returncode == 0)
    else:
        print("SKIP POSIX process-group checks on Windows (the taskkill /T path is unchanged)")

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
