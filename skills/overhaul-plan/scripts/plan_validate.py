"""plan_validate.py: the plan validator the build cannot start without (overhaul-plan Step 8, R6).

Reads spec/UNITS.md (generated plan data, not protected prose):
  `Phases: M0, P0, M1, ...` (the phase order), an Owners table `| Owner | Layer | Root |` and a Units table
  `| Unit | Function | Phase | Owner | Test | Calls | Needs | Ext | Allowed | Lines |`.
A unit is one function, or one plan-made split of it (F394a, F394b), each with its own tests: one unit = one packet.
Fails (one MISS line each) when, for the phases checked:
  - a unit names no `file::test` Test, or two units claim the same test;
  - its Owner is not in the Owners table, or a Calls owner is unknown or not in a LOWER layer (the one layer rule:
    a higher layer calls a lower layer's public API; callers lists are derived from Calls, never hand-kept);
  - a Needs unit is unknown, in a later phase, or the Needs graph has a cycle;
  - Lines is not a number or is over the packet cap (default 300): the plan splits it;
  - Allowed is empty, or an Allowed path is outside the owner's Root and is not a test path;
  - an Ext item (toolkit, model download, GPU) is not named in spec/MACHINES.md's provisioning;
  - a FUNCTIONS.md row of the phase has no unit, or a unit names a function FUNCTIONS.md lacks.
  py -3 plan_validate.py <repo> [--phase M1] [--cap 300] [--callers]
--callers prints the derived import allowlist (owner: callers) that the import lint reads. Exit 1 on any miss.
"""
import collections
import re
import sys
from pathlib import Path

TEST = re.compile(r"^[\w./-]+::[\w.-]+$")


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


def main():
    a = sys.argv[1:]
    repo = Path(a[0])
    phase = a[a.index("--phase") + 1] if "--phase" in a else None
    cap = int(a[a.index("--cap") + 1]) if "--cap" in a else 300
    text = (repo / "spec/UNITS.md").read_text(encoding="utf-8")
    m = re.search(r"^Phases:\s*(.+)$", text, re.M)
    order = [p.strip() for p in m.group(1).split(",")] if m else []
    rank = {p: i for i, p in enumerate(order)}
    owners = {r[0]: (int(r[1]) if r[1].isdigit() else None, r[2] if len(r) > 2 else "") for r in rows(text, "owner")}
    units = {}
    misses = []
    for r in rows(text, "unit"):
        r += [""] * (10 - len(r))
        if r[0] in units:
            misses.append(f"{r[0]}: duplicate unit id")
        units[r[0]] = dict(zip(["unit", "function", "phase", "owner", "test", "calls", "needs", "ext", "allowed", "lines"], r))
    machines = (repo / "spec/MACHINES.md").read_text(encoding="utf-8", errors="replace") if (repo / "spec/MACHINES.md").exists() else ""
    ftext = (repo / "spec/FUNCTIONS.md").read_text(encoding="utf-8", errors="replace")
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
            elif c != u["owner"] and (lay is None or owners[c][0] is None or owners[c][0] >= lay):
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
