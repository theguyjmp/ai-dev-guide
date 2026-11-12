#!/usr/bin/env bash
# Hand a row back: push cloud/release-<row> holding a REASON file (the train machine releases the claim with it) and
# keep the work in progress as cloud/prev-<row> so the next attempt merges it.
#   bash tools/cloud/release.sh <row> "<reason>"
set -euo pipefail
row=$1; reason=$2
base=origin/${CLOUD_BASE:-cloud-base}
git add -A && git commit -qm "$row: WIP (released)" >/dev/null 2>&1 || true
if [ "$(git rev-list --count "$base"..HEAD)" != 0 ]; then git push -q -f origin "HEAD:refs/heads/cloud/prev-$row" 2>/dev/null; fi
blob=$(printf '%s\n' "$reason" | git hash-object -w --stdin)
tree=$(printf '100644 blob %s\tREASON\n' "$blob" | git mktree)
c=$(git commit-tree "$tree" -p HEAD -m "release $row")
git push -q -f origin "$c:refs/heads/cloud/release-$row" 2>/dev/null
echo "released $row"
