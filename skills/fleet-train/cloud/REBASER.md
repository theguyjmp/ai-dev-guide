# Cloud rebaser brief (one session, up to 4 rows)

The land train merges queued rows onto `main` in batches. A queued row that no longer merges cleanly is pushed as
`cloud/rebase-<row>`. Rebase it onto the current `cloud-base` (= the train's main) so the train merges it clean.

Every Bash call: `cd /home/user/<repo> && source <env file> && ...` (the shell keeps neither cwd nor env).

1. **Setup.** If the toolchain env file is missing, run `bash tools/cloud/setup.sh 2>&1 | tail -15`. SETUP FAILED: stop, report it.
2. **List.** `git fetch -q origin cloud-base '+refs/heads/cloud/*:refs/remotes/origin/cloud/*'`. Candidates: refs
   `origin/cloud/rebase-<row>` with no `origin/cloud/rebased-<row>` and no `origin/cloud/rebase-claim-<row>`. Shuffle them
   (`shuf`). None: stop, report `NO ROW`.
3. **Claim one.** `git checkout -q --detach origin/cloud-base && git commit -q --allow-empty -m "rebase-claim <row> $(date +%s%N) $RANDOM"
   && git push -q origin HEAD:refs/heads/cloud/rebase-claim-<row>` (no -f). Rejected: someone has it, try the next.
4. **Rebase.** `git checkout -q -B work origin/cloud/rebase-<row> && git rebase origin/cloud-base`. Resolve every conflict
   keeping BOTH sides' behaviour (main's code is landed and proven: never drop it; re-apply the row's change on top).
   Lock files: take main's and let the package manager regenerate. `git add` + `git rebase --continue` until done.
5. **Check.** Build the tests (errors only, `| head`), run the row's own tests and the unit tests of each package the
   row touches (`| tail -5`). Fix what the rebase broke, commit. Do not run the whole suite: the train does that.
6. **Push.** `git push -q origin HEAD:refs/heads/cloud/rebased-<row>`. Then the next row (back to 2), at most 4 rows.
7. **Can't make it green** (the same step fails twice): `git push -q origin --delete cloud/rebase-claim-<row>`, skip the row.

Never push `main`, `cloud-base`, the harness branch or another session's refs; never force-push. Keep output small.
About 120 tool calls in total. End with `REBASED <rows>` or `NO ROW`.
