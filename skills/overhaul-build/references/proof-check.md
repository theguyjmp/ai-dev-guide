# Blind proof-check (overhaul-build, after drive.py exits 0)

You judge whether each proven function really works through the product. You are blind: read only this file, `spec/FUNCTIONS.md`, `spec/ARCHITECTURE.md`, `overhaul.toml`, `tests/acceptance/` and the product code on main. Never read `.overhaul/` (briefs, reviews, logs, state), git history, hand-backs or any chat. Cap: 100 tool calls. Small output: grep and line ranges, no whole files over 300 lines.

For each function whose status is proven (only the ids your prompt names, if it names any):
1. Start at its entry as the product wires it (the CLI command, the HTTP route on the started server, the UI action on the page) and follow the calls to the code that does the work the row describes.
2. Judge it, citing file:line:
   - REAL: the entry reaches product code that does that work, and the acceptance test drives that entry.
   - PARTIAL: real code does the work, but the entry does not reach it, or the test reaches it another way (an import, a helper, another route).
   - STAND-IN: the path ends in a stub, a fake, a hardcoded or canned result, or a branch that special-cases the test.
   - UNCHECKED: not reached within the cap; it counts as not REAL.
3. When unsure, break the implementation in a throwaway `git worktree` and run the function's acceptance there (the `acceptance` command in overhaul.toml): a REAL function's test goes red. Remove the worktree after.

Write `.overhaul/proof-check.txt`: line 1 `PROOF real R/N partial P stand-in S unchecked U`, then one line per function: `<id> REAL|PARTIAL|STAND-IN|UNCHECKED <file:line> <reason in one clause>`. Change nothing else. Hand back line 1 only.
