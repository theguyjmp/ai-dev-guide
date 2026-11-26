#!/usr/bin/env bash
# The verifier loop (CYCLE.md "The minimum cycle" step 3): one round every 5 minutes, refs mirrored (trunk.py mirror).
# Start it detached through keepalive.py: {"name": "verifier", "cmd": ["bash", "<this file>"], "cwd": "."}.
set -uo pipefail
S=$(cd "$(dirname "$0")" && pwd)
PY=${PY:-$(command -v py >/dev/null 2>&1 && echo "py -3" || echo python3)}
while true; do
  $PY "$S/trunk.py" verify --gate-cmd "$PY \"$S/gate_sha.py\" {sha}"
  $PY "$S/trunk.py" mirror
  sleep "${VERIFY_EVERY:-300}"
done
