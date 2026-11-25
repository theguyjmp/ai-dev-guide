# Cloud builder brief (one session, one row)

You are a builder for the rebuild (Rust workspace), running in a cloud sandbox. One row, end to end, then stop.
The harness machine hands out rows on branch `harness-cloud` and collects your pushed branch every 5 minutes; it
reaches `main` after its affected checks, and the verifier of the landing cycle (fleet-train CYCLE.md) runs the full
suite behind and reverts what fails. This mirrors the local builder brief (fleet-train `builders.js`); same rules.

Every Bash call: `cd <repo root> && source /opt/harness/env.sh && ...` (the shell keeps neither cwd nor env).

1. **Setup.** If `/opt/harness/env.sh` does not exist, run `bash tools/cloud/setup.sh 2>&1 | tail -15`. If it prints
   SETUP FAILED, stop and report that line; claim nothing.
2. **Claim.** `bash tools/cloud/claim.sh`. It takes the first free row (a push of a new ref `cloud/claim-<row>`, which
   fails if another session holds it, so it tries the next one), checks out branch `cloud/<row>` from
   `origin/cloud-base`, merges the deps in `stack` (pushed, not on the base yet) and `previous_branch` if the assignment names one, fetches
   the quality scripts to `/tmp/harness-qa/` and prints the assignment JSON (also in `/tmp/assignment.json`).
   If it prints `NO ROW`, stop: there is nothing to do. If it prints `MERGE CONFLICT`, resolve it and commit first.
3. Read the row's line in spec/FUNCTIONS.md (`grep -n "| <row> |" spec/FUNCTIONS.md`), its acceptance test files, and
   only the source files you need (grep/sed ranges, not whole huge files). `old_harness_reason` and `recent_rejects`
   say why earlier attempts failed: fix exactly that.
4. You OWN this row completely. There is no planner to send it back to: if a dependency capability is missing, a stub
   is in the way, the row is too big, or a file is not in the row's file list, implement what is needed yourself, in
   the module where it belongs. Keep it minimal and clean: before adding or renaming ANY public item (function,
   type, enum, trait, const, static) find an existing owner with
   `python3 /tmp/harness-qa/index_api.py --find "<purpose words>"` (ranked index lines; never a raw grep). A public
   name another module already declares, or a function grown into a copy of a sibling, is refused at land.
5. **Done** = the row's acceptance tests pass: `cargo test -q --test <stem> -- --exact <test_name>` for each test in
   `tests` (add `--release` if a debug run takes over 3 min). Then strict clippy for each crate you touched:
   `cargo clippy -q -p <crate> --all-targets -- -D warnings -D clippy::too_many_lines -D clippy::unwrap_used -D clippy::cognitive_complexity`.
   Do NOT run the whole workspace test suite: the verifier's sharded full gate does that.
6. **Rules.** Never edit the status column of spec/FUNCTIONS.md; never edit or weaken acceptance tests of other rows;
   edit this row's own test only if it contradicts the row's spec text, and say why in the commit message. Never
   hand-edit a shared list (test manifest, test group root, index, generated table): add your own files and let the
   generator register them; a shared list every branch edits sends finished work back. No sleeping or polling.
   6b. The assignment has `must_also_before_commit`: follow it exactly (its commands already run the scripts in
   `/tmp/harness-qa/` from the repo root). A second copy of anything an owner already has (see spec/ARCHITECTURE.md
   and spec/index/*), a file over the size cap, or any dup/shape/strict-clippy problem gets the branch rejected at land.
7. **Commit and self-review.** `git add -A && git commit -m "<row>: <what>"`. Then `bash tools/cloud/added_fns.sh`
   lists the functions you added. If it lists any, run the duplicate review in `tools/cloud/review_prompt.md` with
   `{added}` replaced by that list: start ONE subagent with that text (or do it yourself if you cannot), fix every
   duplicate it reports (call the owner instead, delete your copy), re-run the tests, commit, and review again until
   it reports none (at most 2 rounds). The verifier runs the same reviewer and reverts anything it lists.
7b. **Rebase before push.** `git fetch -q origin cloud-base && git rebase origin/cloud-base`; resolve conflicts keeping both
   sides' behaviour, re-run the row's tests if anything changed. A row that merges clean never waits on a rebaser.
8. **Land.** Re-run the `rules_gate.py check` command from `must_also_before_commit` (it must print RULES OK), push
   the WIP, then run the project's land script in the background with its output in a file, exactly as
   `FIXER.md` steps 8-9 say (`row-land.sh <row>` from harness-cloud; CYCLE.md "Cloud writers"). On `LANDED` or
   `LAND PENDING` report `DONE <row>`.
9. **Budget.** About 90 tool calls. If you cannot get it green by then, or the same step fails twice the same way, run
   `bash tools/cloud/release.sh <row> "<precise blocker: file, error, what is needed>"` (it saves your work as
   `cloud/prev-<row>` for the next attempt), then stop and report `BLOCKED <row>: <reason>`.

Never push to `main`, `cloud-base` or `harness-cloud`, never force-push a ref you did not create, never delete another
session's refs. Keep tool output small: pipe cargo output through `tail -30` or `grep -E "^(error|warning)"`.

**Git discipline (hand-off between sessions and accounts).** Your work must survive this session ending at any moment
(usage limit, timeout, crash) so another session, possibly on another account, can continue it:
- After the first thing that compiles, then at least every ~15 tool calls or ~10 minutes, run
  `git add -A && git commit -qm "WIP <row>: <what works / what is next>" && git push -q -f origin HEAD:refs/heads/cloud/wip-<row>`.
- Keep `HANDOFF-<row>.md` at the repo root current in each WIP commit, at most 10 lines: what is done, what is next, the
  exact failing command and error, the decisions taken. Delete it in the final commit.
- On a usage or rate-limit warning, or a very large context: stop new work, push the WIP branch with HANDOFF, report
  `HANDOFF <row>: cloud/wip-<row>` and end.
- If claim.sh prints `RESUMED`, read `HANDOFF-<row>.md` before anything else.
