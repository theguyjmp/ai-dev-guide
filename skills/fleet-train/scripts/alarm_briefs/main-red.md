# Alarm {{key}}: main is red

Alarm (from fleet_beat): {{facts}}

First rerun the failing stage on main once (state/unit-baseline.json and state/last-gate.txt name the tests). Green: it was
load or a flake; say so in your note and stop. Red: find the commit that broke it (`git log` on the files the failing test
covers) and fix the CODE, never the test. Work in a clean slot: `py -3 {{home}}/harness.py clean-start --slot {{slot}} --name {{name}}` (CARGO_BUILD_JOBS=2, the env it prints, one foreground cargo command at a time); `harness.py quality-check`; `git merge-tree --write-tree main HEAD` exits 0; `harness.py clean-submit --slot {{slot}} --name {{name}}`; move that queue entry to the front (under h.lock("state") set its ts in state/queue.json to the oldest ts - 1). Delete the slot-{{slot}} target dir when done.
