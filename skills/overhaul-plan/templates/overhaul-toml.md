# overhaul.toml: optional keys, Rust and JS

Read the part your project needs (overhaul-plan Step 6 points here).

Optional, all backward compatible: `[commands.acceptance_by]` maps a test-file glob to its own template (scale tests:
`"tests/acceptance/scale_*.rs" = "cargo test --release --test {stem} -- --exact {test} --test-threads=1"`); `[tiers]
scale = [globs]` reruns those proven tests at land only when the footprint touches their module; `[commands.extra]`
(name = command) adds project lints land and pre-review run. A FUNCTIONS `rules` column (`P07, S18`) with `[paths]
patterns` / `standards` cites project rule ids: plan_validate checks each id exists, each `ui:` row (or entry naming a
`[plan] interactive_verbs` word) cites a pattern id, and each cited pattern id is named in its acceptance test.
Rolling plan: status `deferred` marks a row planned later (id, phase, module, deps and entry checked; no files or
tests yet; no planned row may depend on it); FUNCTIONS may hold one table per phase. `[plan] checks = ["ui_verb",
"float_roundtrip"]` opts in to: each `ui:` row deps on a `cli:`/`http:` row; every serde_json dependency enables
float_roundtrip. Policies `Fault hook: NAME`: every crash test body names NAME. `[plan] policy_lists = {cancellable =
"cancel"}`: a `Cancellable:` Policies line whose ids each need a test matching the regex. `[plan] traces =
["spec/PERFORMANCE.md"]`: each table row names an ARCHITECTURE module (owner) and a test node in a FUNCTIONS cell.
Reuse before new (X16): a module may carry `Forbidden types: *Undo*, *Redo*` (type stems only it defines; each stem
has one owner), `Capability: <what it does>` and `Why new: <one line>`; no module name or Capability may match another
owner's stems. A new owner (a `Why new:` line, or missing from ARCHITECTURE at git ref `[plan] owners_since`) needs
the reason. `spec/ROADMAP.md` (`[paths] roadmap`) holds the later phases as `| phase | behaviour | owner | why new |`:
an owner not in ARCHITECTURE is new, needs a why-new on one row and must not match other owners' stems; phases obey
max_phase_size. Plan the whole phase map there so plan_validate checks it, not only the current phase.
Brevity: `[text] line_words`, `decision_line_words` (DECISIONS `D<n>:` lines), `module_section_words` (an ARCHITECTURE `## Module` section), `functions_prose_words` (FUNCTIONS text outside tables), `always_read_words` (CLAUDE.md); fleet-train's rules_gate.py fails a tree over a cap, and a cap may only be tightened.
Rust (plan_validate checks it): when documents store floats, serde_json gets `features = ["float_roundtrip"]` (else 0.059000000000000004 reads back as 0.059 and byte-equal undo/redo breaks); acceptance in `tests/acceptance/<f>.rs`, each a root Cargo.toml `[[test]]` named by its stem, through `mod entry;`;
stubs `todo!()`; unit tests in-crate `#[cfg(test)] mod tests`; property `tests/property/test_<id>.rs` with `proptest!`; no conftest.py. Commands: test `cargo test -q --workspace --lib --bins`,
acceptance `cargo test -q --test {stem} -- --exact {test}`, property `cargo test -q --test {stem}`, lint `cargo clippy -q --workspace --all-targets -- -D warnings`, the scripts on `crates src`. JS: `npx eslint .`, `npx jscpd src`, `node --test`.
