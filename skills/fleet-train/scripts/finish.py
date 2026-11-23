"""Lane finisher: the train's own checks on a committed CLEAN branch, cheapest first, then clean-submit (SKILL.md Lanes).

  py -3 finish.py --slot N --name NAME [--front] [--tests ID_OR_FILE ...]

Last line: SUBMIT <rc>, GATES OK (FINISH_NO_SUBMIT=1) or NOT SUBMITTED <reason>. Logs: state/finish/<name>-<step>.txt.
"""
from __future__ import annotations

import argparse, os, posixpath, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cargo_slots  # noqa: E402
import cargo_failures as cf  # noqa: E402
import harness as h  # noqa: E402

COMPILE_STAGES = ("clippy", "unit", "quality")  # the gate's train_stage marks
TRAIN_WAIT = int(os.environ.get("FINISH_TRAIN_WAIT", "2700"))
HOLD_WAIT = int(os.environ.get("FINISH_HOLD_WAIT", "7200"))
POLL = 20


class NotSubmitted(Exception):
    pass


def train_compiling() -> str:
    mark = h.load(h.TRAIN_STAGE, {})
    stage = mark.get("stage", "")
    return stage if stage in COMPILE_STAGES and mark.get("pid") and h.pid_alive(mark["pid"]) else ""


def run(cmd: str, cwd: Path, log: Path, slots, sleep=time.sleep, compiling=train_compiling) -> int:
    """cmd's exit code, output in log: after the train's compile stage (bounded) and inside one machine-wide slot."""
    waited = 0
    while compiling() and waited < TRAIN_WAIT:
        sleep(POLL)
        waited += POLL
    if waited:
        print(f"waited {waited}s for the train's compile stage")
    log.parent.mkdir(parents=True, exist_ok=True)
    procs: list = []
    with open(log, "w", encoding="utf-8") as out:
        def spawn():
            p = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, env=h.run_env(h.qc_env(cwd)))
            procs.append(p)
            return p.pid, h.proc_start(p.pid)
        while not procs:
            try:
                slots.claim("finish", log.stem, spawn)
            except cargo_slots.Full:
                sleep(15)
        return procs[0].wait()


def train_holds(events: list, row: str) -> bool:
    """True while the running train gates row and has not rejected it."""
    marks = [i for i, e in enumerate(events) if e.get("event") in ("train_start", "train_end")]
    if not marks or events[marks[-1]].get("event") != "train_start" or row not in events[marks[-1]].get("rows", []):
        return False
    return not any(e.get("event") == "reject" and e.get("row") == row for e in events[marks[-1]:])


def changed(cwd: Path, base: str) -> list:
    names = h.git("diff", "--name-only", f"{base}...HEAD", cwd=cwd).stdout.split()
    status = h.git("diff", "--name-status", f"{base}...HEAD", cwd=cwd).stdout.splitlines()
    return sorted(set(names) | {ln.split("\t")[1] for ln in status if ln.startswith("R")})


def _parents(f: str):
    p = posixpath.dirname(f)
    while p:
        yield p
        p = posixpath.dirname(p)


def pinned_tests(cwd: Path, files: list) -> list:
    """Test files under pin_dirs quoting a changed source file, or a directory holding one (below a source root)."""
    src = [f for f in files if h.in_src(f) and f.endswith(tuple(h.P["src_exts"]))]
    roots = {d.rstrip("/") for d in h.P["src_dirs"]}
    dirs = {d for f in src for d in _parents(f) if d not in roots}
    quoted = [f'"{p}"' for p in src] + [f'"{d}"' for d in dirs] + [f'"{d}/"' for d in dirs]
    out = []
    for d in h.P["pin_dirs"]:
        for t in sorted((cwd / d).rglob("*")) if (cwd / d).is_dir() else []:
            if t.is_file() and t.suffix in h.P["src_exts"] and any(q in t.read_text(encoding="utf-8", errors="replace") for q in quoted):
                out.append(t.relative_to(cwd).as_posix())
    return out


def scope_pkgs(files: list) -> str:
    pk = {f"-p {h.CRATE}-{f.split('/')[1]}" if f.startswith("crates/") else f"-p {h.CRATE}"
          for f in files if f.startswith(("crates/", "src/", "tests/"))}
    return " ".join(sorted(pk))


def unit_red(cmd: str, cwd: Path, log: Path, slots) -> str:
    """'' or the new failures; main's own (the train's cached baseline) never count."""
    rc = run(cmd, cwd, log, slots)
    text = log.read_text(encoding="utf-8", errors="replace")
    base = cf.from_keys(h.load("unit-baseline.json", {}).get("fails", []))
    red, failing = h.unit_verdict(subprocess.CompletedProcess(cmd, rc, text, ""), lambda: base, cwd=cwd)
    if not red:
        return ""
    return "; ".join(failing.without(base).keys()[:5]) or "the unit run failed and named no test"


def finish(slot: int, name: str, tests: list, scope: str, slots) -> None:
    cwd, logs = h.slot_wt(slot), h.ST / "finish"
    step = lambda s: logs / f"{name}-{s}.txt"
    tmp = h.WT / "tmp" / f"{name}-{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ.update(TMP=str(tmp), TEMP=str(tmp), TMPDIR=str(tmp))
    if h.git("status", "--porcelain", cwd=cwd).stdout.strip():
        raise NotSubmitted("dirty tree")
    if h.git("fetch", "-q", "origin", "main", cwd=cwd, check=False).returncode:
        raise NotSubmitted("fetch failed")
    if h.git("merge-tree", "--write-tree", "HEAD", "origin/main", cwd=cwd, check=False).returncode:
        raise NotSubmitted("conflicts with main")
    for i in (1, 2):
        if dup := h.submit_dup(cwd):
            raise NotSubmitted(f"self-review {i}: {dup[:300]}")
    if bad := h.static_problems(cwd, "origin/main"):
        raise NotSubmitted(f"static checks: {bad[-600:]}")
    print("self-review x2 and static ok")
    files = changed(cwd, "origin/main")
    for t in sorted({t.split("::")[0] for t in tests} | set(pinned_tests(cwd, files))):
        if run(h.P["test_file"].replace("{file}", t).replace("{stem}", Path(t).stem), cwd, step(Path(t).stem), slots):
            raise NotSubmitted(f"test {t} red")
        print(f"test {t} ok")
    if h.P["shrink_cmd"] and run(h.P["shrink_cmd"], cwd, step("ratchets"), slots):
        raise NotSubmitted("shrink-only ratchets red")
    flag = h.P["scope_flag"] if scope == "crates" else ""
    if flag:
        if not (pkgs := scope_pkgs(files)):
            return print("no crate touched: unit and lint skipped")
        if red := unit_red(h.P["unit"].replace(flag, pkgs), cwd, step("unit"), slots):
            raise NotSubmitted(f"unit tests: {red}")
        lint = h.P["lint_strict"] or h.P["lint"]
        if lint and run(lint.replace(flag, pkgs), cwd, step("lint"), slots):
            raise NotSubmitted("strict lint")
        return print(f"unit and lint ok ({pkgs})")
    if red := unit_red(h.P["unit"], cwd, step("unit"), slots):
        raise NotSubmitted(f"unit tests: {red}")
    if q := h.quality_problems(cwd, "origin/main"):
        raise NotSubmitted(f"quality check: {q[-600:]}")
    print("unit and quality ok")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--slot", type=int, required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--front", action="store_true")
    ap.add_argument("--tests", nargs="*", default=[])
    a = ap.parse_args()
    slots = cargo_slots.CargoSlots(lambda pid, start: h.pid_alive(pid) and (not start or h.proc_start(pid) == start))
    try:
        finish(a.slot, a.name, a.tests, os.environ.get("FINISH_SCOPE", "crates"), slots)
        if os.environ.get("FINISH_NO_SUBMIT") == "1":
            print("GATES OK")
            return 0
        waited, row = 0, f"CLEAN-{a.name}"
        while train_holds(h.read_events(), row):
            if waited >= HOLD_WAIT:
                raise NotSubmitted(f"row still in the running train after {waited}s")
            time.sleep(120)
            waited += 120
    except NotSubmitted as e:
        print(f"NOT SUBMITTED {e}")
        return 1
    r = subprocess.run([sys.executable, str(Path(h.__file__)), "clean-submit", "--slot", str(a.slot), "--name", a.name,
                        *(["--front"] if a.front else [])])
    print(f"SUBMIT {r.returncode}")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
