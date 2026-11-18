"""Tests of cargo_failures.py (the one `cargo test` output parser) on recorded outputs, and of the unit gate's
pass-alone retry (harness.unit_flaky) that consumes it. Fixtures in tests/fixtures/cargo_test/: probe_* are full
`cargo test -q -p <crate> --lib --no-fail-fast` runs (stdout then stderr, as harness.sh joins them) with a forced
failure, an abort and a verbose run; the rest are recorded reject-reason tails. Names and paths are anonymised.

  py -3 tests/test_cargo_failures.py
"""
from __future__ import annotations

import os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace

HOME = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="test-cargofail-"))
os.environ.update({"HARNESS_HOME": str(TMP / "home"), "HARNESS_REPO": str(TMP / "repo"), "HARNESS_WT": str(TMP / "wt"),
                   "HARNESS_STACK": "rust"})
sys.path.insert(0, str(HOME))
import harness as h  # noqa: E402

cf = h.cf
FX = HOME / "tests" / "fixtures" / "cargo_test"
FAILS = []
DOC, VIEW, LIB = "-p acme-doc --lib", "-p acme-view --lib", "-p acme-library --lib"
EVAL = "variable_eval::tests::test_a_malformed_expression_is_refused_with_its_field_and_position"
PROBE = "probe_forced_failure::probe_fails_on_purpose"


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
    if not ok:
        FAILS.append(name)


def fx(name):
    return (FX / f"{name}.txt").read_text(encoding="utf-8")


# ------------------------------------------------------------------ the formats cargo prints
f = cf.parse(fx("probe_fail"))
check("quiet: `name --- FAILED`, `---- name stdout ----` and the failures list agree", f.tests == (PROBE, EVAL), f)
check("quiet: names account for the target", f.targets == (DOC,) and not f.unnamed, f)
f = cf.parse(fx("probe_verbose"))
check("verbose: `test name ... FAILED`", f.tests == (PROBE, EVAL) and not f.unnamed, f)
f = cf.parse(fx("quiet_named_failure"))
check("recorded quiet failure is named", f.tests == ("frame::tests::a_loaded_frame_submits_load_times_the_scene",), f)
for name, target, code in (("probe_abort", DOC, "0xc0000409"), ("crash_heap_corruption", LIB, "0xc0000374"),
                           ("crash_access_violation_slow_tests", VIEW, "0xc0000005")):
    f = cf.parse(fx(name))
    check(f"{name}: the crashed binary is reported by its target, never an empty set",
          f.found and f.unnamed == (target,) and code in f.crashes.get(target, "") and not f.tests, f)
    check(f"{name}: the reason names the target and the exit", target in f.describe() and code in f.describe(),
          f.describe())
f = cf.parse(fx("three_targets_tail_without_names"))
check("three failed targets, no name in the tail: all three kept", f.unnamed == (DOC, "-p acme-motion --lib",
                                                                                  "-p acme-part --lib"), f)
f = cf.parse(fx("probe_verbose").replace("test probe_forced_failure::probe_fails_on_purpose ... FAILED",
                                         "test probe_forced_failure::probe_fails_on_purpose - should panic ... FAILED"))
check("verbose should_panic line is named", PROBE in f.tests, f)

# a named failure in one binary and a crash in another (composed from the two recorded runs): the crash is not lost
fail_out, crash_out = fx("probe_fail"), fx("crash_heap_corruption")
fail_stdout, fail_stderr = fail_out.split("error: test failed", 1)
crash_stdout, crash_stderr = crash_out.split("error: test failed", 1)
mixed = (fail_stdout + crash_stdout + "error: test failed" + fail_stderr.split("error: 1 target")[0]
         + "error: test failed" + crash_stderr.split("error: 1 target")[0]
         + f"error: 2 targets failed:\n    `{DOC}`\n    `{LIB}`\n")
f = cf.parse(mixed)
check("mixed: names from the doc binary and the crashed library target both kept",
      f.tests == (PROBE, EVAL) and f.unnamed == (LIB,) and f.targets == (DOC, LIB), f)
check("mixed: keys round-trip through JSON-plain strings", cf.from_keys(f.keys()).keys() == f.keys(), f.keys())

f = cf.parse("error[E0432]: unresolved import `x`\n --> crates\\view\\src\\look.rs:288:49\nerror: could not compile")
check("a build error finds nothing (the gate stays red)", not f.found and not f.targets, f)
cap = "failures:\n    not_a_test_just_printed\n\nsome other text\n"
check("a test's printed `failures:` text is not a name", not cf.parse(cap).tests, cf.parse(cap))

base = cf.from_keys([EVAL, LIB])
left = cf.parse(mixed).without(base)
check("without(baseline) drops main's own names and crashed targets", left.tests == (PROBE,) and not left.unnamed, left)
check("passed() sums every binary", cf.passed("test result: ok. 3 passed; 0 failed\ntest result: ok. 0 passed;") == 3)


# ------------------------------------------------------------------ the pass-alone retry
def fake_sh(results):  # the public unit_flaky runs through unit_runs (desk-aware)
    calls = []

    def runs(cmds, **_):
        calls.extend(cmds)
        return [SimpleNamespace(returncode=rc, stdout=out, stderr="")
                for rc, out in (next(v for k, v in results.items() if k in c) for c in cmds)]
    return runs, calls


OK1 = "running 1 test\n.\ntest result: ok. 1 passed; 0 failed; 0 ignored\n"
OK0 = "running 0 tests\n\ntest result: ok. 0 passed; 0 failed; 0 ignored\n"
real_sh = h.unit_runs
try:
    h.unit_runs, calls = fake_sh({PROBE: (0, OK1), LIB: (0, OK1)})
    ok = h.unit_flaky(cf.from_keys([PROBE, LIB]))
    check("flaky: a name passing alone and a crashed target passing alone", ok and any(
        c.startswith(f"cargo test -q {LIB} -- --test-threads=1") for c in calls) and any(
        f"--exact {PROBE}" in c for c in calls), calls)
    h.unit_runs, calls = fake_sh({PROBE: (0, OK0)})
    check("flaky: a rerun that ran zero tests is not a pass", not h.unit_flaky(cf.from_keys([PROBE])), calls)
    h.unit_runs, calls = fake_sh({LIB: (101, "")})
    check("flaky: a target red alone stays red", not h.unit_flaky(cf.from_keys([LIB])), calls)
    check("flaky: nothing found never counts as flaky", not h.unit_flaky(cf.from_keys([])))
finally:
    h.unit_runs = real_sh

# the names reach the reject reason (unit_red_log's last 1500 chars)
red = h.unit_red_log(fx("probe_abort"))
check("reject reason names the crashed target", "unit failures (1): -p acme-doc --lib" in red[-1500:], red[-300:])
red = h.unit_red_log(fx("probe_fail"))
check("reject reason names the failing tests", PROBE in red[-1500:] and EVAL in red[-1500:], red[-300:])

# a build or link error is no red (CargoFailures.build_failed, unbuilt by crate name: the desk's incremental clean)
link = cf.parse("app.exe : fatal error LNK1120: 2 unresolved externals\r\n"
                'error: could not compile `app` (bin "app") due to 1 previous error\n')
check("build: a bin link failure is a build failure of target app", link.build_failed and link.unbuilt == ("app",), link)
both = cf.parse('error: could not compile `app-doc` (lib) due to 3 previous errors\n'
                'error: could not compile `app` (test "s01_one") due to 1 previous error\n')
check("build: a lib by its crate ('-' as '_'), a test target by its name", both.unbuilt == ("app_doc", "s01_one"), both)
check("build: test names found beat a compile line (a red)",
      not cf.parse(fx("probe_fail") + '\nerror: could not compile `app` (bin "app")\n').build_failed)
check("build: nothing failed is no build failure", not cf.parse("").build_failed)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
