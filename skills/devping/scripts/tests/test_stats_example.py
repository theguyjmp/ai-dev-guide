"""stats_example.py: the whole-platform total counts each row once, the card survives a re-plan, and an unknown probe
or an unmeasured count never reads as 0. Run: py -3 tests/test_stats_example.py (prints ALL PASS)."""
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import devping as dp  # noqa: E402
import stats_example as se  # noqa: E402

PLAN = """# Plan

| id | module | status | replaces |
|---|---|---|---|
| C01 | core | planned | P01, P02, `P03` |
| C02 | core | proven | - |
| C02 | core | planned | - |
| C03 | ui | planned | |
"""
ROADMAP = """# Roadmap

| id | phase | note |
|---|---|---|
| C01 | 1 | already in the plan |
| R01 | 2 | open |
| R01 | 2 | listed twice |
| R02 | 2 | split, moved to FUNCTIONS as C02/C03 |
| R03 | 3 | moved to phase 3, still not planned |

## Track

| id | track | note |
|---|---|---|
| T01 | gui | |
| T02 | gui | |
"""
NOW = 1_800_000_000.0


def snap(**kw):
    base = dict(now=NOW, total=se.Total(3, 1, 2), proven=1, units_total=6, units_done=2, pushes=(), main_ts=NOW - 60)
    return se.Snapshot(**{**base, **kw})


def test_total_counts_whole_platform_once():
    plan = se.plan_index(se.table_rows(PLAN))
    assert sorted(plan) == ["C01", "C02", "C03"], plan
    assert plan["C02"]["status"] == "proven"
    assert se.plan_index(se.table_rows("﻿" + PLAN.split("\n", 2)[2])) == plan, "a BOM must not hide the table"
    total = se.platform_total(plan, se.table_rows(ROADMAP), se.moved_marker("spec/FUNCTIONS.md"))
    assert total == se.Total(planned=3, roadmap_open=2, track=2), total
    assert total.whole == 7


def test_replan_keeps_landed_work():
    plan = se.plan_index(se.table_rows(PLAN))
    units = se.work_units(plan)
    assert sorted(units) == ["C01", "C02", "C03", "P01", "P02", "P03"], units
    tagged, pushes = se.landings("%d [P01] old packet\n%d [P02] old packet\n%d docs only\n"
                                 % (NOW - 600, NOW - 300, NOW - 100))
    assert tagged == {"P01", "P02"} and len(pushes) == 2
    assert se.units_done(units, tagged) == 2
    assert se.units_done(units, tagged | {"C01"}) == 4
    s = snap(pushes=pushes)
    assert s.since(se.RATE_H) == 2, "the rate counts every tagged push, old plan ids included"


def test_unmeasured_and_unknown_never_zero():
    c = se.card(snap(problems_base=1200, problems_left=None))
    problems = next(f["value"] for f in c["fields"] if f["name"] == "Problems")
    assert problems.startswith("1,200") and "not measured" in problems, problems
    assert se.gate_count("# header\na.rs 3\nb.rs 4\n") == 7
    assert se.gate_count("") == 0
    unknown = snap(verified_ref="verified", verified_ts=None, unverified=None)
    c = se.card(unknown)
    assert c["health"] == "amber" and c["quiet_ok"] is False, c
    assert next(f["value"] for f in c["fields"] if f["name"] == "verified") == "unknown"
    assert se.card(snap(main_ts=None))["fields"][3]["value"] == "unknown"


def test_quiet_ok_only_while_moving():
    assert se.quiet_ok(snap(main_ts=NOW - 60)) is True
    assert se.quiet_ok(snap(main_ts=NOW - se.MOVING_S - 1)) is False
    stale = snap(verified_ref="verified", verified_ts=NOW - 9000, unverified=2,
                 oldest_unverified_ts=NOW - se.STALE_VERIFIED_S - 1)
    assert se.health(stale) == "amber" and se.quiet_ok(stale) is False


def test_card_text_changes_only_with_facts():
    def sig(now):
        s = snap(now=now, verified_ref="verified", verified_ts=NOW - 900, unverified=0)
        p = dp.Project(name="X", dir=".", chat_dir=".")
        return dp.signature(dp.card(p, dp.CardState(), dp.Stats.parse(se.card(s)), NOW - 60, now)["embeds"][0])
    assert sig(NOW) == sig(NOW + 300), "the same facts 5 min apart must not edit the card"


def test_end_to_end_on_a_repo():
    with tempfile.TemporaryDirectory() as d:
        def git(*a):
            subprocess.run(["git", "-C", d, "-c", "user.name=test", "-c", "user.email=test@localhost", *a],
                           check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        (Path(d) / "spec").mkdir()
        (Path(d) / "spec" / "FUNCTIONS.md").write_text(PLAN, encoding="utf-8")
        (Path(d) / "spec" / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "[P01] old packet")
        git("branch", "verified")
        git("commit", "-q", "--allow-empty", "-m", "[C01] absorbs P01-P03")
        out = se.main(["--repo", d, "--roadmap", "spec/ROADMAP.md", "--verified", "verified",
                       "--problems", "gates/left.txt", "--problems-baseline", "40"])
        assert out["progress"] == [1, 7], out
        assert out["health"] == "green" and out["quiet_ok"] is True, out
        values = {f["name"]: f["value"] for f in out["fields"]}
        assert values["Plan"].startswith("**5 of 6**"), values
        assert values["verified"].endswith("1 commits waiting"), values
        assert "not measured" in values["Problems"], values
        bad = se.main(["--repo", d, "--functions", "spec/MISSING.md"])
        assert bad["health"] == "red" and "MISSING" in bad["headline"], bad


if __name__ == "__main__":
    failed = 0
    for name, fn in [(k, v) for k, v in dict(globals()).items() if k.startswith("test_")]:
        try:
            fn()
            print("ok   ", name)
        except Exception:  # noqa: BLE001 - report every failing test, not only the first
            failed += 1
            print("FAIL ", name)
            traceback.print_exc()
    print("ALL PASS" if not failed else "%d FAILED" % failed)
    sys.exit(1 if failed else 0)
