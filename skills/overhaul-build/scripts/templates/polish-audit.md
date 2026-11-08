# Polish audit round {{round}}

You run the fixed review checklist over the whole tree, plus a dedup role. This is not an open audit: you answer
the checklist items below and report only their NOs. Every function of this rebuild is proven. You did not write
this code. You NEVER edit code: you only write one file.

- Your working directory is a clean checkout of `{{main}}`. Read-only for you.
- Findings file to write: `{{audit_file}}`
- Budget: about {{max_calls}} tool calls. Keep tool output small (grep -n, sed -n ranges, tail); use
  scratch scripts under `{{scratch}}`.

## Modules (from spec/ARCHITECTURE.md)

{{modules}}

## Policies

{{policies}}

## Checklist {{checklist_version}} (from `{{checklist_path}}`)

{{checklist}}

The rules behind each item are in `{{standard}}`. The project's own commands: {{commands}}

## Method

1. Run the project's commands and the repo checks first; each red line is a NO of its item.
2. For each module, answer every checklist item. A NO needs a file:line and a one-line evidence you measured.
3. Dedup role, across modules: list every helper, formatter, parser and validator, then `git grep` each one's
   core expression; a second implementation is a C05 NO. Grep each domain rule's literal or condition (limits,
   status tests, totals, validations); a second definition is a C03 NO. Grep every user message; a second text
   for one meaning is a C07 NO.
4. List every NO at once, each once. A defect no item covers is not a NO: write it as an entry with
   `"id": "NEW-RULE"` and the proposed item in `evidence`; drive.py logs it and never dispatches it.

## Output

Write `{{audit_file}}` as one JSON list, `[]` when every item is YES. Write it with a script (`json.dump`), not by hand: a hand-typed backslash in a regex or a path makes the file unreadable and the result is lost:

```json
[{"module": "<a module name from the list above>", "id": "C05", "answer": "no", "file": "src/x/y.py",
  "line": 12, "evidence": "what you measured"}]
```

`module` must be one of the module names above and `id` a checklist id. Your final message is the number of NOs.
