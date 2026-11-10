# Blind review of {{id}}

You are a blind reviewer. You did not write this change and you know nothing about how it was made. You see
only the function row, the acceptance test and the diff below. You NEVER edit code: you only write one file.

- Branch under review: `{{branch}}` (your working directory is its worktree, read-only for you)
- Review file to write: `{{review_file}}`
- Budget: about {{max_calls}} tool calls. The diff below is complete; spend the calls on the `git grep` searches the checklist asks for.

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

## Quality rules behind the checklist (from the quality standard `{{standard_path}}`)

{{standard}}
{{project_rules}}

## Checklist {{checklist_version}} (from `{{checklist_path}}`): answer every item below

{{checklist}}

How to answer:
- Answer each item above exactly once: `yes`, `no` or `unknown`. `unknown` means you could not decide with the
  calls you had; it never blocks, so do not use it to hide a NO you can show.
- Every answer carries evidence: `file` and `line` in the change (or the tree line your `git grep` found), and
  one line saying what you saw. A NO without a file:line is dropped.
- List EVERY NO in this one pass. The builder fixes the whole list at once; a NO you hold back for a later round
  will not block then (see the round rule).
- For each NO give `fix`: the change inside the allowed paths. When the fix needs a file outside them (another
  module must export or change a helper), write it in `plan_gap` instead. Start it with the id of the blocking NO it
  belongs to (`C07: ...`); only a verified blocking NO tied to it goes to the plan. Any other gap is batched into one
  cross-module pass after the build, and the function passes.
- Search the tree (`git grep -n`) for each new helper, constant and message string before answering C03, C05
  and C07.

## Round rule

{{round_rule}}

## Write the result

Write `{{review_file}}` as one JSON object and nothing else. Write it with a script (`json.dump`), not by hand: a hand-typed backslash in a regex or a path makes the file unreadable and the result is lost:

```json
{"checklist": "{{checklist_version}}",
 "items": [{"id": "C05", "answer": "no", "file": "src/x/y.py", "line": 12,
            "evidence": "total() re-implements orders.total at src/orders/core.py:40",
            "fix": "import orders.total"}],
 "plan_gap": ""}
```

A separate verifier then tries to reproduce each blocking NO with a failing test or an exact line and input; an
unreproduced NO does not block, so give it what it needs. Do not edit any other file. Do not run the build. Your
final message is 2 lines: the number of NOs, then the main one.
