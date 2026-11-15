# Alarm {{key}}: the last gate ran in a broken environment

Alarm (from fleet_beat): {{facts}}

Read the tail of state/last-gate.txt and state/train.err. A queued row whose native code does not build on this machine:
find it (merge the queued rows touching native code onto a scratch worktree of main, build with CARGO_BUILD_JOBS=2, read
the first error), `harness.py release` that row with the error as the reason, and requeue rows blamed wrongly. A train
started from the wrong shell or env: stop it by its pid between batches; fleet_beat.py restarts it.
