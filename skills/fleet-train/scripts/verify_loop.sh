#!/usr/bin/env bash
# The verifier loop (CYCLE.md "The minimum cycle" step 3): one round every 5 minutes, refs mirrored to origin.
# Start it detached through keepalive.py: {"name": "verifier", "cmd": ["bash", "<this file>"], "cwd": "."}.
set -uo pipefail
S=$(cd "$(dirname "$0")" && pwd)
PY=${PY:-$(command -v py >/dev/null 2>&1 && echo "py -3" || echo python3)}
while true; do
  $PY "$S/trunk.py" verify --gate-cmd "$PY \"$S/gate_sha.py\" {sha}"
  git -C "${HARNESS_REPO:-.}" push -q origin integ main verified
  sleep "${VERIFY_EVERY:-300}"
done
