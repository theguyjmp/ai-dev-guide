# Blind code-quality audit (read-only)

The method shared by the polish audit (drive.py) and the final judge. Audit the repo in the current folder. Do
not edit anything there. Do not read any earlier audit or trial notes. Keep tool output small (tail, grep, cut),
and use scratch scripts in the run folder you are given.

The bar is the quality standard: ~/.claude/skills/overhaul-build/references/quality-standard.md
(the copy beside this skill: `references/quality-standard.md`). Audit against it; each issue cites a rule number
(`Q19`). Also list anything outside the standard separately as NEW-RULE candidates.

Measure, don't guess: `check_cycles.py --repo .` and `entry_lint.py tests/acceptance` (in
~/.claude/skills/overhaul-build/scripts), `pylint --disable=all --enable=duplicate-code
--min-similarity-lines=4 src`, `ruff check --select E,F,W,B,SIM,UP,C90 --line-length 88 .`, the test suite, an
AST census of function size, complexity, broad excepts and dead code, about 18 hand-made mutants against the unit
tests, a load test of every HTTP write path with 1, 2, 4 and 8 concurrent clients (check status codes and stored
counts), corrupt-file behaviour of every store (wrong shape and right shape with bad values), cross-module
imports that bypass a public API, and repeated literals or re-implemented helpers. Then walk the standard rule by
rule and run each rule's Check.

Score 1 to 10 on: 1 Structure, 2 Duplication, 3 Lint, 4 Size, 5 Readability and error handling, 6 Tests, 7
Concurrency and data safety. A 10 means you found no defect in that area. Then list "Worst issues" in order with
file:line, the rule number and the measurement, then "NEW-RULE candidates" the same way.
Write the report to the path you are given. Its last line must be `SCORES s1 s2 s3 s4 s5 s6 s7 | ISSUES n | NEW m`
(n counts rule-cited issues, m the NEW-RULE candidates).
