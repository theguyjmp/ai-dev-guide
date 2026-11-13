# fleet-train changelog

- 2026-11-11: train conflict resolver (`resolve_conflict`, HARNESS_RESOLVE_MAX): 16 of 18 rows in one train of the pilot project were rejected on one hot file.
- 2026-11-12: train start rule (HARNESS_BATCH_WAIT_BUSY): wait for a full batch while claims are in flight, start at once when none; a 1-row train had cost a full gate.
- 2026-11-12: `unit_flaky`: new unit failures are rerun alone, single-threaded, before the batch is blamed (a timing test failed two gates under load).
- 2026-11-12: `fmt_tree`: batched rustfmt replaces `cargo fmt --all`, which overflowed the Windows command line and failed silently.
- 2026-11-12: `read_events`: the event log reader skips blank or half-written lines (one blank line took status and the dashboard down).
- 2026-11-12: `self-review --slot N` and builders.js step 7: builders run the train's duplicate reviewer before submit; builders.js `slot_list` arg.
- 2026-11-12: the plain clippy stage is skipped when the quality stage runs (its strict clippy is a superset; saved about 10 min a train).
- 2026-11-12: size gate `oversized_files` (HARNESS_FILE_LINES / HARNESS_TEST_FILE_LINES): files added or grown past the cap are rejected, existing ones grandfathered until split.
- 2026-11-12: cloud fleet: `cloud.py` (assign / collect / status through the `harness-cloud` branch, atomic `cloud/claim-<row>` refs, `cloud-base` mirror of the train's main) and the `cloud/` kit templates; local builders off the gate machine.
- 2026-11-12: dash.py takes the planned-row total from `harness.py status`.
- 2026-11-13: one harness for every stack: every stack-specific command and pattern sits in one profile (`STACKS`, `HARNESS_STACK=rust|node`, `harness.toml` overrides); rust behaviour unchanged (checked call for call against the previous file); node runs `node --test` per test file with TAP failure parsing.
- 2026-11-13: fix: gate()'s quick-path return had been dedented out of `if quick:`, so every full gate crashed on `fails`.
- 2026-11-13: `disjoint()` picks a batch by trial merges (git merge-tree), file-disjoint as fallback: 39 of 106 queued rows instead of 23.
- 2026-11-13: fleet_beat: FIRE cap 8 while 20+ rows are queued, 30 once it drains; SUPPLY alarm when builders starve behind the queue.
