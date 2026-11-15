# Alarm {{key}}: the land train is not landing

Alarm (from fleet_beat): {{facts}}

Find the one cause: `py -3 {{home}}/harness.py status`, the tail of state/last-gate.txt and state/train.err, the last 40
train events in state/events.jsonl (grep, never the whole file). Then act on it:
- a row that breaks the gate: `harness.py release` it with the failing test as the reason (its builder redoes it);
- a test red on main that no row is to blame for: add it to state/accept-known-red.json (h.lock("state")) with a reason
  naming notes/requests/fix-test-<stem>.md, and write that request (the dispatcher starts its fix agent);
- a wedged train: restart it as fleet_beat.py restarts it (stop it by its pid between batches), never mid-land.
Never touch main, train.lock or cloud-base yourself.
