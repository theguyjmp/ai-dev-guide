#!/usr/bin/env bash
# The writer's only push path (CYCLE.md "The minimum cycle" step 2). Run it in the writer's committed worktree:
#   bash push.sh <slot> <name> [rows...]
# Exit codes are trunk.py push's: 0 PUSHED, 1 REFUSED, 3 HELD, 4 RECHECK (check again), 5 RACE (run again).
set -uo pipefail
S=$(cd "$(dirname "$0")" && pwd)
PY=${PY:-$(command -v py >/dev/null 2>&1 && echo "py -3" || echo python3)}
slot=$1; name=$2; shift 2
git rebase -q integ || { git rebase --abort; echo "REFUSED conflict with integ: rebase, resolve, push again"; exit 1; }
base=$(git rev-parse integ); sha=$(git rev-parse HEAD)
log=$(mktemp)
FINISH_NO_SUBMIT=1 $PY "$S/finish.py" --slot "$slot" --name "$name" > "$log" 2>&1
[ "$(tail -1 "$log" | tr -d '\r')" = "GATES OK" ] || { tail -5 "$log"; echo "REFUSED the writer checks"; exit 1; }
pid=$(git diff --binary "$base" "$sha" | git patch-id --stable | cut -d' ' -f1)
{ echo "CHECKED $sha ${pid:-empty}"; cat "$log"; } > "$log.receipt"
$PY "$S/trunk.py" push --sha "$sha" --base "$base" --log "$log.receipt" --name "$name" --rows "$@"; rc=$?
[ $rc -eq 0 ] && $PY "$S/trunk.py" mirror
rm -f "$log" "$log.receipt"
exit $rc
