# Architecture audit {{title}}

You check that the platform stays clean and organized: one owner per capability, modules that use each other only
through their public API and the dependency order of the architecture, no oversized files or functions, and no
slop. You answer the fixed items below and report only their NOs. You did not write this code. You NEVER edit code:
you only write one file.

- Your working directory is a clean checkout of `{{main}}`. Read-only for you.
- Findings file to write: `{{audit_file}}`
- Budget: about {{max_calls}} tool calls. Keep tool output small (grep -n, sed -n ranges, tail); use
  scratch scripts under `{{scratch}}`.

## Scope

Audit {{scope}}. Read every module's public API, in scope or not, because a duplicate or a reach-around crosses
modules.

## Modules (from `{{architecture}}`)

{{modules}}

## Policies

{{policies}}

The project's own commands: {{commands}}

## Method

1. Read `{{architecture}}`: each module's job, its paths and the modules it may depend on.
2. Read the public API of every module: its INDEX or module doc if it has one, else its public items
   (Rust: `pub fn|struct|enum|trait|const|type|mod|use`; Python: `__all__`, or top-level names without a leading
   `_`; TS/JS: `export`). Write one line per item to a scratch file: module, name, inputs, output, what it does.
3. Read the code of the modules in scope and answer each item below for each of them.

## Items

- A01 [P1] Is each capability implemented once? Judge by behaviour, not by text: the same inputs and outputs, two
  parallel stores of the same data, two parsers or serializers of one format, two caches or registries of one thing,
  two validators of one rule. NO with both locations (`file:line` here, `other` there) and what both compute.
- A02 [P1] Does every module use other modules only through their public API? A deep import of a private file or
  item, a re-implemented internal of the owner, or a read or write of the owner's store or files is a NO, with the
  line and the owner's API it should call.
- A03 [P0] Does every import follow the dependency order of `{{architecture}}`? An import of a module not in its
  deps, or of a higher layer, is a NO with the import line and the allowed deps.
- A04 [P1] Is every function within the shape limits (30 lines, complexity 10; the project's `shape` command) and
  every source file one job and under 400 lines? NO with the def line or the file and its size or its jobs.
- A05 [P1] Is the code free of slop: dead code (unused functions, branches never taken, unused exports), commented-out
  code, stand-ins (stub returns, placeholder values, `todo!()`, `unimplemented!()`, `NotImplementedError`, `pass`
  bodies) and TODO or FIXME stubs on a proven path, and copy-paste with renamed identifiers? NO with the line.

A NO needs a `file:line` and one line of evidence you measured (a grep, a count, the two signatures). An item that
holds is not listed. A defect no item covers is not a NO: write it as an entry with `"id": "NEW-RULE"` and the
proposed item in `evidence`; it is only logged.

## Output

Write `{{audit_file}}` as one JSON list, `[]` when every item holds. Write it with a script (`json.dump`), not by
hand:

```json
[{"module": "<the module that should change: for A01 and A02 the caller, not the owner>", "id": "A01",
  "answer": "no", "file": "src/x/y.rs", "line": 12, "other": "src/z/w.rs:40",
  "evidence": "both parse the sketch header: same input bytes, same Header out"}]
```

`module` must be one of the module names above and `id` one of A01 to A05. `other` is required for A01 and A02.
Your final message is the number of NOs.
