"""Task loader of the landing cycle: which packets a session may claim. Plan format and rules: CYCLE.md "Task loader".

    py -3 task_loader.py --plan PLAN.md --state loader.json [--repo R --gate-dir DIR]   # prints the Listing JSON
    py -3 task_loader.py --plan PLAN.md --check [--repo R --gate-dir DIR]               # exit 1 on a refusal"""
from __future__ import annotations

import argparse, json, re, subprocess, sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

GATE, ONE_OFF = "gate", "one-off"
_ID = re.compile(r"[A-Za-z][\w.-]*")


@dataclass(frozen=True)
class Footprint:
    task: str
    areas: frozenset = frozenset()
    deps: tuple = ()
    klass: str = ""
    gate: str = ""
    absorbs: tuple = ()

    def overlap(self, other: "Footprint") -> frozenset:
        return self.areas & other.areas


@dataclass(frozen=True)
class ClassPlan:
    classes: dict = field(default_factory=dict)  # class id -> frozenset of gate names
    groups: dict = field(default_factory=dict)  # area -> group
    packets: tuple = ()  # Footprints in plan order

    @property
    def gate_packets(self) -> tuple:
        return tuple(p.task for p in self.packets if p.klass == GATE)


@dataclass
class Listing:
    listed: list = field(default_factory=list)  # claimable now, plus claimed packets (a claim is never displaced)
    held: dict = field(default_factory=dict)  # packet -> why it is not listed
    release: list = field(default_factory=list)  # claims to drop: their push is on the published base
    unknown: list = field(default_factory=list)  # packets with no areas: overlap nothing, logged


def _cells(line: str) -> list:
    return [c.strip().strip("`") for c in line.strip().strip("|").split("|")]


def tables(text: str) -> list:
    """[(header cells lower-cased, [row cell lists])] of every Markdown table in text."""
    out, lines = [], text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            head, rows, i = [c.lower() for c in _cells(lines[i])], [], i + 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            out.append((head, rows))
        else:
            i += 1
    return out


def _ids(cell: str, own: str = "") -> tuple:
    return tuple(dict.fromkeys(t for t in _ID.findall(cell) if t != own and t != "-"))


def read_plan(text: str) -> ClassPlan:
    classes, groups, packets = {}, {}, []
    for head, rows in tables(text):
        cols = set(head)
        for r in rows:
            row = dict(zip(head, r + [""] * (len(head) - len(r))))
            if {"id", "areas", "class"} <= cols:
                t = row["id"]
                packets.append(Footprint(t, frozenset(_ids(row["areas"])), _ids(row.get("deps", ""), t),
                                         row["class"], row.get("gate", "").strip(" -"), _ids(row.get("replaces", ""), t)))
            elif {"class", "gates"} <= cols:
                classes[row["class"]] = frozenset(_ids(row["gates"]))
            elif {"area", "group"} <= cols:
                groups[row["area"]] = row["group"]
    return ClassPlan(classes, groups, tuple(packets))


def class_refusal(fp: Footprint, plan: ClassPlan, on_main: Callable[[str], bool]) -> str:
    """Why the packet's own class and gate cells refuse it, or ""."""
    if not fp.klass:
        return "no class cell: fix by class (CYCLE.md); a per-instance packet is never published"
    if fp.klass == ONE_OFF:
        return "" if fp.gate else "a one-off must name its regression test or check in its gate cell"
    if fp.klass == GATE:
        if plan.gate_packets[:1] != (fp.task,):
            return f"class gate belongs to the plan's one gate packet ({', '.join(plan.gate_packets[:1]) or 'none'})"
        return "" if fp.gate else "the gate packet must name its mechanism in its gate cell"
    if fp.klass not in plan.classes:
        return f"class {fp.klass!r} is none of the plan's classes ({', '.join(sorted(plan.classes))}), gate, one-off"
    named = plan.classes[fp.klass] & set(_ids(fp.gate))
    if not named:
        return f"its gate cell names none of class {fp.klass}'s gates ({', '.join(sorted(plan.classes[fp.klass]))})"
    delivered = any(d in plan.gate_packets for d in fp.deps)
    missing = sorted(g for g in named if not delivered and not on_main(g))
    return f"gate {', '.join(missing)} is not on main and no packet in its deps delivers it" if missing else ""


def class_problems(plan: ClassPlan, open_ids: list, on_main: Callable[[str], bool]) -> dict:
    """{open packet: why refused}: its cells, or a later packet of one class on a shared group (unless deps link them)."""
    fps = {p.task: p for p in plan.packets}
    out = {t: why for t in open_ids if t in fps and (why := class_refusal(fps[t], plan, on_main))}
    groups = lambda fp: {plan.groups.get(a, a) for a in fp.areas}  # noqa: E731
    seen = []
    for t in open_ids:
        fp = fps.get(t)
        if fp is None or fp.klass not in plan.classes:
            continue
        for o in seen:
            common = groups(fp) & groups(o) if o.klass == fp.klass else set()
            if common and t not in o.deps and o.task not in fp.deps:
                out.setdefault(t, f"duplicate class {fp.klass} in {', '.join(sorted(common))} with {o.task}")
                break
        seen.append(fp)
    return out


def listing(plan: ClassPlan, claimed: set, unbased: set, done: set, previous: set,
            on_main: Callable[[str], bool]) -> Listing:
    """`done` = pushes the published base holds: not in flight, claims released. Lease order: gate packet, the last
    publish's packets, plan order."""
    fps = {p.task: p for p in plan.packets}
    order = [p.task for p in plan.packets]
    flying = (set(claimed) - set(done)) | set(unbased)
    out = Listing(release=sorted(set(claimed) & set(done) - set(unbased)))
    open_ids = [t for t in order if t not in done]
    refused = {t: w for t, w in class_problems(plan, open_ids, on_main).items() if t not in flying}
    leased = [fps[t] for t in order if t in flying]
    out.listed = [t for t in order if t in flying and t in claimed]
    free = [t for t in open_ids if t not in flying]
    free.sort(key=lambda t: (fps[t].klass != GATE, t not in previous, order.index(t)))
    for t in free:
        fp = fps[t]
        hit = next(((o.task, s) for o in leased if (s := fp.overlap(o))), None)
        waits = [*(d for d in fp.deps if d not in done), *(a for a in fp.absorbs if a in flying)]
        if t in refused:
            out.held[t] = refused[t]
        elif waits:
            out.held[t] = "waits for " + ", ".join(dict.fromkeys(waits))
        elif hit:
            out.held[t] = f"overlaps {hit[0]} ({', '.join(sorted(hit[1]))})"
        else:
            out.listed.append(t)
            leased.append(fp)
    out.unknown = sorted(t for t in open_ids if not fps[t].areas)
    return out


def git_on_main(repo: str, gate_dir: str) -> Callable[[str], bool]:
    """True when main's gate_dir names the gate as a word; a failed grep raises (no verdict is no pass)."""
    def on_main(name: str) -> bool:
        r = subprocess.run(["git", "-C", repo, "grep", "-q", "-w", "-F", name, "main", "--", gate_dir],
                           capture_output=True, text=True)
        if r.returncode > 1:
            raise RuntimeError(f"git grep {name} failed: {r.stderr.strip()[-200:]}")
        return r.returncode == 0
    return on_main


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan", required=True)
    ap.add_argument("--state")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--gate-dir", default=".", help="where main declares its gates (counters, owner names)")
    ap.add_argument("--check", action="store_true", help="refuse the plan if any packet fails the class check")
    a = ap.parse_args(argv)
    plan = read_plan(Path(a.plan).read_text(encoding="utf-8"))
    on_main = git_on_main(a.repo, a.gate_dir)
    if a.check:
        bad = class_problems(plan, [p.task for p in plan.packets], on_main)
        print("\n".join(f"{t}: {w}" for t, w in bad.items()) or f"PLAN OK {len(plan.packets)} packets")
        return 1 if bad else 0
    st = json.loads(Path(a.state).read_text(encoding="utf-8")) if a.state else {}
    res = listing(plan, set(st.get("claimed", [])), set(st.get("unbased", [])), set(st.get("done", [])),
                  set(st.get("previous", [])), on_main)
    print(json.dumps(asdict(res), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
