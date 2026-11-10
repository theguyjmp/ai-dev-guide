"""plan_validate.py: the plan validator the build cannot start without (overhaul-plan Step 8, R6), and the counting check.

Plan mode reads spec/UNITS.md (generated plan data, not protected prose):
  `Phases: M0, P0, M1, ...` (the phase order), an Owners table `| Owner | Layer | Root |` and a Units table
  `| Unit | Function | Phase | Owner | Test | Calls | Needs | Ext | Allowed | Lines |`.
A unit is one function, or one plan-made split of it (F394a, F394b), each with its own tests: one unit = one packet.
Fails (one MISS line each) when, for the phases checked:
  - a unit names no `file::test` Test, or two units claim the same test;
  - its Owner is not in the Owners table, or a Calls owner is unknown (with --layers, opt-in for new plans only: or not in a LOWER layer:
    a higher layer calls a lower layer's public API; callers lists are derived from Calls, never hand-kept);
  - a Needs unit is unknown, in a later phase, or the Needs graph has a cycle;
  - Lines is not a number or is over the packet cap (default 300): the plan splits it;
  - Allowed is empty, or an Allowed path is outside the owner's Root and is not a test path;
  - an Ext item (toolkit, model download, GPU) is not named in spec/MACHINES.md's provisioning;
  - a FUNCTIONS.md row of the phase has no unit, or a unit names a function FUNCTIONS.md lacks.
  py -3 plan_validate.py <repo> [--phase M1] [--cap 300] [--callers] [--layers]
--callers prints the derived import allowlist (owner: callers) that the import lint reads.

Counting mode (a tick check row): fails when the project's own proven-counting rule cannot read a named test for
every function of the phase in spec/FUNCTIONS.md, so a change to counting never again disconnects silently from the plan.
  py -3 plan_validate.py <repo> --counting --phase M1 [--rule <file.py>:<func>] [--syspath <dir;dir>] [--ref origin/main] [--exists]
--rule is the counting code itself, called on the FUNCTIONS.md text and returning {function id: names}
(the pilot project: --rule tools/health/health/sources/gate.py:accept_map --syspath "tools/health;tools/vc", R192); it is loaded
from the checkout with the --syspath dirs (default <repo>/tools) first on sys.path, as the project's own entry point does. Without --rule a backticked `<file>::<test>` in the row counts. --ref reads FUNCTIONS.md (and, with --exists,
the test sources) at that git ref. --exists also fails a named test that no file in the repo defines.
Exit 0 pass, 1 on any miss, 2 on bad usage. Read-only: it writes nothing.
"""
import collections
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

TEST = re.compile(r"^[\w./-]+(::[\w.-]+)+$")
FID = re.compile(r"F\d{3}[a-z]?")
DEFAULT_NAME = re.compile(r"`([^`\s]+::[^`]+)`")


def rows(text, header0):
    out, on = [], False
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            on = False
            continue
        cells = [c.strip().strip("`").strip() for c in s.strip("|").split("|")]
        if cells and cells[0].lower() == header0:
            on = True
            continue
        if on and not re.match(r"^[\s:-]*$", "".join(cells)):
            out.append(cells)
    return out


def items(cell):
    return [x.strip().strip("`") for x in re.split(r"[,;]\s*", cell or "") if x.strip() and x.strip() not in ("-", "none")]


def arg(a, flag, default=None):
    if flag not in a:
        return default
    i = a.index(flag) + 1
    if i >= len(a):
        raise SystemExit(f"usage: {flag} needs a value")
    return a[i]


def git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()[:200]}")
    return p.stdout


def functions_rows(ftext):
    """[(id, phase, cells, line)] of the FUNCTIONS table whose header names Id and Phase."""
    out, cols = [], None
    for i, line in enumerate(ftext.splitlines(), 1):
        s = line.strip()
        if not s.startswith("|"):
            cols = None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        low = [c.strip("`* ").lower() for c in cells]
        if "id" in low and "phase" in low:
            cols = (low.index("id"), low.index("phase"))
            continue
        if cols and len(cells) > max(cols) and FID.fullmatch(cells[cols[0]].strip("`* ")):
            out.append((cells[cols[0]].strip("`* "), cells[cols[1]].strip("`* "), cells, i))
    return out


def default_rule(ftext):
    out = {}
    for fid, _, cells, _ in functions_rows(ftext):
        names = set(DEFAULT_NAME.findall(" | ".join(cells[1:])))
        if names:
            out.setdefault(fid, set()).update(names)
    return out


def load_rule(repo, spec, syspath="tools"):
    path, _, func = spec.rpartition(":")
    full = Path(path) if os.path.isabs(path) else repo / path
    for d in reversed([x for x in re.split(r"[;,]", syspath or "") if x.strip()]):
        d = d.strip() if os.path.isabs(d.strip()) else str(repo / d.strip())
        if os.path.isdir(d) and d not in sys.path:
            sys.path.insert(0, d)
    s = importlib.util.spec_from_file_location("counting_rule", str(full))
    if s is None:
        raise FileNotFoundError(str(full))
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return getattr(mod, func)


def name_key(n):
    """(kind, key) of one name the rule returns: a (kind, key) tuple, or a bare `<file>::<test>` string."""
    if isinstance(n, (tuple, list)) and len(n) == 2:
        return str(n[0]), str(n[1])
    return "test", str(n)


class Tree:
    """Repo files at a ref (git) or in the checkout (tracked files), read on demand."""

    def __init__(self, repo, ref):
        self.repo, self.ref = repo, ref
        out = git(repo, "ls-tree", "-r", "--name-only", ref) if ref else git(repo, "ls-files")
        self.by_name = collections.defaultdict(list)
        for p in out.splitlines():
            self.by_name[p.rsplit("/", 1)[-1]].append(p)

    def read(self, p):
        if self.ref:
            return git(self.repo, "show", f"{self.ref}:{p}")
        return (self.repo / p).read_text(encoding="utf-8", errors="replace")

    def defines(self, kind, key):
        if kind == "file":
            return bool(self.by_name.get(key.rsplit("/", 1)[-1] + ".rs"))
        file, _, test = key.partition("::")
        file = file.rsplit("/", 1)[-1]
        if file.endswith(".test.mjs"):
            return any(test.strip() in self.read(p) for p in self.by_name.get(file, []))
        fn = re.compile(rf"\bfn\s+{re.escape(test.split('::')[-1])}\s*\(")
        return any(fn.search(self.read(p)) for p in self.by_name.get(file + ".rs", []))


def counting(repo, a):
    phase, ref = arg(a, "--phase"), arg(a, "--ref")
    if not phase:
        print("usage: --counting needs --phase <P>")
        return 2
    try:
        ftext = git(repo, "show", f"{ref}:spec/FUNCTIONS.md") if ref else (repo / "spec/FUNCTIONS.md").read_text(encoding="utf-8")
    except (OSError, RuntimeError) as e:
        print(f"MISS spec/FUNCTIONS.md unreadable: {e}")
        print("COUNTING FAIL: no FUNCTIONS.md")
        return 1
    spec = arg(a, "--rule")
    try:
        amap = load_rule(repo, spec, arg(a, "--syspath", "tools"))(ftext) if spec else default_rule(ftext)
    except Exception as e:  # the counting rule itself is broken or moved: exactly what this check reports
        print(f"MISS counting rule {spec} failed: {type(e).__name__}: {e}")
        print("COUNTING FAIL: the counting rule cannot run")
        return 1
    funcs = [(fid, ln) for fid, ph, _, ln in functions_rows(ftext) if ph == phase]
    if not funcs:
        print(f"MISS phase {phase} has no rows in spec/FUNCTIONS.md")
    misses, unfound = [], []
    tree = Tree(repo, ref) if "--exists" in a else None
    for fid, ln in funcs:
        names = sorted(name_key(n) for n in (amap.get(fid) or ()))
        if not names:
            misses.append(f"{fid}: spec/FUNCTIONS.md:{ln}: the counting rule reads no named test, so it can never count")
        elif tree:
            for kind, key in names:
                if not tree.defines(kind, key):
                    unfound.append(f"{fid}: spec/FUNCTIONS.md:{ln}: named {kind} {key} is defined nowhere{' at ' + ref if ref else ''}")
    for x in misses + unfound:
        print("MISS", x)
    ok = bool(funcs) and not misses and not unfound
    print(f"COUNTING {'OK' if ok else 'FAIL'} phase {phase}: {len(funcs) - len(misses)} of {len(funcs)} functions readable"
          + (f", {len(unfound)} named tests not found" if tree else ""))
    return 0 if ok else 1


def main():
    a = sys.argv[1:]
    if not a or a[0].startswith("--"):
        print(__doc__)
        return 2
    repo = Path(a[0])
    if "--counting" in a:
        return counting(repo, a)
    phase = arg(a, "--phase")
    cap = int(arg(a, "--cap", "300"))
    try:
        text = (repo / "spec/UNITS.md").read_text(encoding="utf-8")
        ftext = (repo / "spec/FUNCTIONS.md").read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"MISS {e.filename}: missing")
        print("PLAN INVALID: plan data missing")
        return 1
    m = re.search(r"^Phases:\s*(.+)$", text, re.M)
    order = [p.strip() for p in m.group(1).split(",")] if m else []
    rank = {p: i for i, p in enumerate(order)}
    owners = {}
    for r in rows(text, "owner"):
        r += [""] * (3 - len(r))
        owners[r[0]] = (int(r[1]) if r[1].isdigit() else None, r[2])
    units = {}
    misses = []
    if phase and phase not in rank:
        misses.append(f"phase {phase} is not in Phases: {', '.join(order) or 'none'}")
    for r in rows(text, "unit"):
        r += [""] * (10 - len(r))
        if r[0] in units:
            misses.append(f"{r[0]}: duplicate unit id")
        units[r[0]] = dict(zip(["unit", "function", "phase", "owner", "test", "calls", "needs", "ext", "allowed", "lines"], r))
    machines = (repo / "spec/MACHINES.md").read_text(encoding="utf-8", errors="replace") if (repo / "spec/MACHINES.md").exists() else ""
    fphase = {r[0]: r[2] for r in rows(ftext, "id") if len(r) > 2}
    sel = {k: u for k, u in units.items() if not phase or u["phase"] == phase}
    tests = collections.defaultdict(list)
    for u in units.values():
        for t in items(u["test"]):
            tests[t].append(u["unit"])
    for k, u in sel.items():
        ts = items(u["test"])
        if not ts or not all(TEST.match(t) for t in ts):
            misses.append(f"{k}: Test must be file::test names, got '{u['test'] or 'none'}'")
        for t in ts:
            if len(tests[t]) > 1:
                misses.append(f"{k}: test {t} also claimed by {', '.join(x for x in tests[t] if x != k)}")
        if u["phase"] not in rank:
            misses.append(f"{k}: phase {u['phase']} not in Phases")
        if u["owner"] not in owners:
            misses.append(f"{k}: owner {u['owner']} not in the Owners table")
        lay = owners.get(u["owner"], (None, ""))[0]
        for c in items(u["calls"]):
            if c not in owners:
                misses.append(f"{k}: calls unknown owner {c}")
            elif "--layers" in a and c != u["owner"] and (lay is None or owners[c][0] is None or owners[c][0] >= lay):
                misses.append(f"{k}: {u['owner']} (layer {lay}) calls {c} (layer {owners[c][0]}): breaks the layer rule; redesign the interface in ARCHITECTURE")
        for n in items(u["needs"]):
            if n not in units:
                misses.append(f"{k}: needs unknown unit {n}")
            elif rank.get(units[n]["phase"], 99) > rank.get(u["phase"], -1):
                misses.append(f"{k}: needs {n} of later phase {units[n]['phase']}")
        for e in items(u["ext"]):
            if e.lower() not in machines.lower():
                misses.append(f"{k}: external need '{e}' is not provisioned in MACHINES.md")
        if not u["lines"].isdigit():
            misses.append(f"{k}: Lines '{u['lines']}' is not an estimate")
        elif int(u["lines"]) > cap:
            misses.append(f"{k}: {u['lines']} lines > cap {cap}; split into {k}a, {k}b with their own tests")
        root = owners.get(u["owner"], (None, ""))[1]
        allowed = items(u["allowed"])
        if not allowed:
            misses.append(f"{k}: no Allowed files")
        for p in allowed:
            if root and not p.startswith(root) and "test" not in p.lower():
                misses.append(f"{k}: Allowed {p} is outside owner root {root}")
        if re.sub(r"[a-z]+$", "", u["function"]) not in fphase:
            misses.append(f"{k}: function {u['function']} not in FUNCTIONS.md")
    seen, stack = set(), set()

    def cyc(k):
        if k in stack:
            return True
        if k in seen or k not in units:
            return False
        seen.add(k)
        stack.add(k)
        hit = any(cyc(n) for n in items(units[k]["needs"]))
        stack.discard(k)
        return hit
    for k in sel:
        if cyc(k):
            misses.append(f"{k}: Needs cycle")
            break
    covered = {re.sub(r"[a-z]+$", "", u["function"]) for u in units.values()}
    for f, p in fphase.items():
        if (not phase or p == phase) and f not in covered:
            misses.append(f"{f}: FUNCTIONS row of phase {p} has no unit")
    if "--callers" in a:
        callers = collections.defaultdict(set)
        for u in units.values():
            for c in items(u["calls"]):
                if c != u["owner"]:
                    callers[c].add(u["owner"])
        for o in sorted(callers):
            print(f"{o}: {', '.join(sorted(callers[o]))}")
    for x in misses:
        print("MISS", x)
    print(f"PLAN {'VALID' if not misses else 'INVALID'} phase {phase or 'all'}: {len(sel)} units, {len(misses)} misses")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main())
