# Re-plan a needs-plan function (drive.py exited 3)

Read only its row, `.overhaul/reviews/<id>.txt` and its `.overhaul/log.jsonl` lines, never the diff. A row parked
oversized (a builder, land or acceptance timeout, or too big for one builder) comes back split (1), never tweaked;
every row you add or change keeps the row size and carries its work packet (Step 4). Do one, add one
DECISIONS line, pass Step 7, make a plan commit on a branch (only spec/, tests/, new stubs, overhaul.toml), move main
to it only at PLAN LAND OK (overhaul-plan Rules), then run `drive.py --repo <rebuild> --reset <id>` and restart drive.py:
1. Split it: new ids with one new failing test each; update deps and OLD-ENTRIES lines naming it; drop its row and test.
2. Fix its acceptance test, only where it contradicts OLD-ENTRIES or DECISIONS, never to make it easier.
3. Fix the plan: add the missing API item or dependency, or move the function to the module that owns the code.
Acceptance already green before the change: strengthen the test in a plan commit or, if a dep truly delivered
it, have a blind reviewer confirm and run `drive.py --repo <rebuild> --mark-proven <id> --evidence <review file>`.
