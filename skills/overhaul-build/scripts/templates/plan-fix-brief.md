# Plan fix {{id}} (Exit 3, round {{round}})

drive.py parked `{{id}}` as needs-plan. You make ONE plan fix for it, by the rules in `{{skill}}/SKILL.md` section
"Exit 3: needs-plan functions" (read that section only) and overhaul-plan's row rules (`{{plan_skill}}/SKILL.md`, only
the parts on rows, acceptance and ARCHITECTURE). supervise.py started you; other plan-fix agents run at the same time,
one per needs-plan function, and one integrator merges every branch afterwards.

- Worktree (your working directory): `{{wt}}`
- Branch (already checked out, at `{{main}}`): `{{branch}}`
- Live repo, read only: `{{repo}}` (never edit its checkout or `.overhaul/`)
- New ids reserved for you: {{ids}}
- Plan fixes this function already had: {{resets}}
- drive.py's last reason: {{reason}}
- Report: `{{report}}`

You are not the manager: never run drive.py, `--reset`, land.py, a merge or a push, and never switch branches. Cap 40
tool calls; small output (grep, tail, line ranges; no file over ~300 lines whole). Run no cargo, product binary or test:
the supervisor runs every test once after the merge. The same step fails twice: stop and write the report.

1. Read only: the row of `{{id}}` in `{{functions}}`, `{{repo}}/.overhaul/reviews/{{id}}.txt` (if present),
   `grep '"{{id}}"' {{repo}}/.overhaul/log.jsonl | tail -n 5`, and the acceptance test file the row names. Never a diff.
2. Decide the one fix that removes the cause:
   (1) split: new rows, each one module, one entry and its own acceptance test that fails now. Use only the ids reserved
       above (they are unique across every agent of this round), keep deps in phase order, and turn the old row into
       `deferred` with a note naming its replacements;
   (2) fix its acceptance: it cannot go red before and green after through the public entry (already green on main,
       reaches an internal, flaky, wrong fixture);
   (3) fix the plan: ARCHITECTURE.md blocks it (a missing dep, a public API it cannot reach, a file its builder must
       touch outside its `files` cell: add that file to the cell);
   (4) add the missing capability (router class `missing_capability`: a dependency cannot do what this row needs, not
       a wrong result): one new row, a reserved id, in the module that owns the capability, with its own acceptance
       test through the public entry that fails now, ordered before `{{id}}`, and `{{id}}`'s deps naming it. `{{id}}`
       itself stays as it is.
   {{mode}}
   Row size (owner 2026-11-10): every row you add or change is one function or one verb behaviour, about 50-150
   lines in 1-3 files. Its `files` cell names at most 3 files, and its ARCHITECTURE API bullet
   (a `- ` bullet holding the backticked signature and `[<id>]`) carries a work packet on an indented line under it:
   `Packet: edit <file> after <symbol>; calls <helpers from spec/index/<module>.md, or -; test <command>`
   (each file the row edits with the symbol to edit after, the exact signatures on the bullet, the existing helpers
   to call, the test command). Every capability a row calls exists on `{{main}}` (its spec/index/<module>.md line)
   or in a dep row; when it does not, add that row first (fix 4). plan_validate fails a new or changed row without
   them.
   Oversized: a function parked as `oversized` (the router class, a builder, land or acceptance timeout, or a row
   too big for one builder) comes back split, fix (1) only, into rows of that size; a report of any other kind is
   rejected and the function stays needs-plan.
3. Change nothing else: no other function's row or test, no raised limit or budget, no loosened check. Product code is
   never edited; a new module or wiring a builder cannot reach goes into the row's `files` cell instead. A decision
   record you add takes the next free number on `{{main}}`; the integrator renumbers clashes.
4. `py -3 {{validate}} --repo {{wt}}` (no `--run`) prints PLAN OK. Commit on your branch with the subject
   `plan: {{id}} split|acceptance|architecture|capability: <why>` (the subject must start with `plan`: main's guard
   lets only plan commits through). Do not push, do not merge.

Write the report: line 1 `PLAN-FIX {{id}} <split|acceptance|architecture|capability|none> <PLAN OK|PLAN FAIL ...>`,
then the cause in one line, the rows, tests and files changed, the new ids used, and the commit sha.
Hand back that line 1 only.
