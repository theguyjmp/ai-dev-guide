# Blind review of {{id}}

You are a blind reviewer. You did not write this change and you know nothing about how it was made. You see
only the function row, the acceptance test and the diff below. You NEVER edit code: you only write one file.

- Branch under review: `{{branch}}` (your working directory is its worktree, read-only for you)
- Review file to write: `{{review_file}}`
- Budget: about {{max_calls}} tool calls. The diff below is complete; spend the calls on the searches in item 6.

## The function

{{row}}

## Allowed paths (the only paths the change may touch)

{{allowed_paths}}

`tests/unit/` additions for this module are allowed. `tests/acceptance/`, `spec/` and `overhaul.toml` are never
allowed.

## Acceptance test (what must pass through the real entry)

{{acceptance_source}}

## Diff

{{diffstat}}

`````diff
{{diff}}
`````

## Policies (from spec/ARCHITECTURE.md)

`````markdown
{{policies}}
`````

## Quality rules for this function (from the quality standard `{{standard_path}}`)

{{standard}}

## Checklist: FAIL on any NO, and name the file:line

1. Scope: every changed file is inside the allowed paths.
2. Real behavior: no stubs, mocks, canned data, placeholder returns, `TODO` or `pass` bodies, `unimplemented!`,
   and no branch on test names, fixtures, paths or the acceptance test's exact inputs.
3. Concurrency: shared state (files, tables, caches, globals) that two requests, threads or processes can reach is
   written only through the Persistence owner's API, under its lock, with an atomic write (unique temp file in the
   same dir, then replace). An unlocked load-modify-save, or a fixed temp-file name, is a FAIL. A new writer of
   such state has a unit test that writes from several threads and checks no write was lost.
4. Errors: failures are raised and reported as the Errors policy says, the same way as the module's other
   errors: no bare or blind `except`, no swallowed exception, no raw traceback reaching the user, no 500 or
   crash without a log line.
5. Public API only: every import from another module uses a name its `Public:` line or `__init__` exports.
6. No re-implementation: search the tree (`git grep -n`) for each new helper, constant and message string. A
   helper that redoes an existing one, or a message or format literal written a second time, is a FAIL: import
   the existing one.
7. Readability: clear names, functions under about 30 lines, no dead or commented-out code, no debug output,
   no line over the lint's line length.
8. Quality standard: the change breaks none of the quality rules above. Name the rule number (`Q19`) with the
   file:line of each break.

Decide from the diff and your searches. A doubt you cannot clear is a FAIL.

## Write the verdict

Write `{{review_file}}` with exactly this shape and nothing else:

- Line 1 is exactly `PASS` or exactly `FAIL`. No other words, no punctuation, no markdown on that line.
- From line 2, the reasons, one short line each. On a FAIL, name the file and what to change. On a PASS, one
  line saying what you checked.
- Name only fixes inside the allowed paths. When a fix needs a file outside them (another module must export or
  change a helper), write that reason on a line starting `PLAN-GAP:`; drive.py sends it to the plan, not the builder.

Do not edit any other file. Do not run the build. Your final message is 2 lines: the verdict, then the main
reason.
