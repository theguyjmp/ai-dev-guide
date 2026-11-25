"""task_loader.py: fix by class, footprint leases, released claims of landed work, the gate packet first.

Usage: py -3 tests/test_task_loader.py"""
from __future__ import annotations

import subprocess, sys, tempfile
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOME))
import task_loader as tl  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {detail}"))
    if not ok:
        FAILS.append(name)


PLAN = """
| class | problem | gates |
|---|---|---|
| K1 | untyped stored data | stored_untyped |
| K2 | raw parameter reads | raw_params |

| area | group |
|---|---|
| alpha | G1 |
| beta | G1 |
| gamma | G2 |

| id | areas | deps | class | gate | replaces |
|---|---|---|---|---|---|
| P1 | core | - | gate | shrink-only counters stored_untyped, raw_params | - |
| P2 | alpha | P1 | K1 | stored_untyped | OLD9 |
| P3 | beta | P1 | K1 | stored_untyped | - |
| P4 | gamma | P1 | K1 | stored_untyped | - |
| P5 | gamma | P1 | K2 | raw_params | - |
| P6 | alpha | - | one-off | tests/regress_x | - |
| P7 | gamma | - | one-off | - | - |
| P8 | delta | - |  | - | - |
| P9 | delta | - | K2 | raw_params | - |
"""
plan = tl.read_plan(PLAN)
none_on_main = lambda g: False  # noqa: E731
check("reads three tables", (len(plan.classes), len(plan.groups), len(plan.packets)) == (2, 3, 9),
      (plan.classes, plan.groups))
check("deps and absorbs parsed", plan.packets[1].deps == ("P1",) and plan.packets[1].absorbs == ("OLD9",))
bad = tl.class_problems(plan, [p.task for p in plan.packets], none_on_main)
check("no class cell is refused", "no class" in bad.get("P8", ""), bad)
check("one-off without its check is refused", "one-off" in bad.get("P7", ""), bad)
check("one-off naming its check passes", "P6" not in bad, bad)
check("same class on one group: the later is held", "duplicate class K1 in G1 with P2" in bad.get("P3", ""), bad)
check("same class on another group is free", "P4" not in bad, bad)
check("a gate not on main and no gate dep is refused", "not on main" in bad.get("P9", ""), bad)
check("a gate delivered by the gate packet passes", "P5" not in bad, bad)
check("the gate on main passes without the dep", "P9" not in tl.class_problems(plan, ["P9"], lambda g: True))

first = tl.listing(plan, set(), set(), set(), set(), none_on_main)
check("the gate packet lists first and alone (sweeps wait for it)", first.listed[:1] == ["P1"] and
      all("waits for P1" in first.held.get(t, "") for t in ("P2", "P4", "P5")), first)
after = tl.listing(plan, set(), set(), {"P1"}, set(), none_on_main)
check("after the gate: disjoint sweeps list, overlapping ones hold",
      "P2" in after.listed and "P4" in after.listed and "overlaps P4" in after.held.get("P5", ""), after)
check("a one-off overlapping a listed sweep holds", "overlaps P2" in after.held.get("P6", ""), after)
check("a refused packet never lists", "P8" not in after.listed and "P8" in after.held, after)
check("no areas: reported unknown", tl.listing(tl.read_plan(PLAN.replace("| P6 | alpha", "| P6 | -")),
                                                set(), set(), {"P1"}, set(), none_on_main).unknown == ["P6"])

flying = tl.listing(plan, {"P4"}, set(), {"P1"}, set(), none_on_main)
check("a claimed packet stays listed and leases its areas",
      "P4" in flying.listed and "overlaps P4" in flying.held.get("P5", ""), flying)
landed = tl.listing(plan, {"P1", "P4"}, set(), {"P1", "P4"}, set(), none_on_main)
check("a claim whose push the base holds is released and leases nothing",
      landed.release == ["P1", "P4"] and "P5" in landed.listed, landed)
pushed = tl.listing(plan, set(), {"P4"}, {"P1"}, set(), none_on_main)
check("pushed but not on the base is still in flight", "overlaps P4" in pushed.held.get("P5", ""), pushed)
absorb = tl.listing(plan, {"OLD9"}, set(), {"P1"}, set(), none_on_main)
check("a packet waits while one it replaces is in flight", "waits for OLD9" in absorb.held.get("P2", ""), absorb)
prev = tl.listing(plan, set(), set(), {"P1"}, {"P5"}, none_on_main)
check("the last publish keeps its place (never displaced)", "P5" in prev.listed and "P4" in prev.held, prev)

paths = tl.read_plan(PLAN.replace("| P2 | alpha |", "| P2 | crates/core, src/io.rs |")
                     .replace("| P4 | gamma |", "| P4 | crates/ui |"))
check("path areas stay whole", paths.packets[1].areas == frozenset({"crates/core", "src/io.rs"}), paths.packets[1])
check("different paths under one folder do not overlap", not paths.packets[1].overlap(paths.packets[3]))

with tempfile.TemporaryDirectory() as d:
    g = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True)  # noqa: E731
    g("init", "-q", "-b", "main")
    Path(d, "gates.txt").write_text("stored_untyped\n", encoding="utf-8")
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "gates")
    on = tl.git_on_main(d, "gates.txt")
    check("git_on_main finds a declared gate", on("stored_untyped") and not on("raw_params"))
    Path(d, "plan.md").write_text(PLAN, encoding="utf-8")
    rc = tl.main(["--plan", str(Path(d, "plan.md")), "--repo", d, "--gate-dir", "gates.txt", "--check"])
    check("--check exits 1 on a plan with refusals", rc == 1)

print("ALL PASS" if not FAILS else f"FAILED {FAILS}")
sys.exit(1 if FAILS else 0)
