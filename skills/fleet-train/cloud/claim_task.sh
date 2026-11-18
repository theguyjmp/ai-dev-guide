#!/usr/bin/env bash
# Claim ONE task of a cloud lane and start its branch:   bash tools/cloud/claim_task.sh <plan|fix>
# Tasks: origin/harness-cloud:<lane>-tasks.txt (one id per line, written by cloud.py collect). A claim is a push of a
# brand-new ref cloud/<lane>-claim-<task> holding a unique commit, so a second session's push is refused and it tries the
# next task. Checks out <lane>/cloud-<task> from cloud/wip-<lane>-<task> (merged with the base) if a WIP exists, else from
# the base. Prints `CLAIMED <task> <claim sha>` (also in /tmp/<lane>-claim), or `NO TASK` (exit 3).
set -uo pipefail
lane=${1:-}
case $lane in plan|fix) ;; *) echo "usage: claim_task.sh plan|fix"; exit 2;; esac
cd "$(git rev-parse --show-toplevel)"
BASE=${CLOUD_BASE:-cloud-base}   # the train machine's main, mirrored by cloud.py collect
git config user.email >/dev/null || git config user.email "cloud-builder@example.invalid"
git config user.name >/dev/null || git config user.name "cloud builder"
git fetch -q origin "+refs/heads/$BASE:refs/remotes/origin/$BASE" \
  +refs/heads/harness-cloud:refs/remotes/origin/harness-cloud || exit 1
taken=$(git ls-remote origin "refs/heads/cloud/$lane-claim-*" | sed "s#.*refs/heads/cloud/$lane-claim-##")
task=""
for t in $(git show "origin/harness-cloud:$lane-tasks.txt" 2>/dev/null | tr -d '\r'); do
  echo "$taken" | grep -qxF "$t" && continue
  c=$(git commit-tree "origin/$BASE^{tree}" -p "origin/$BASE" -m "claim $lane $t $(hostname) $(date +%s%N)")
  if git push -q origin "$c:refs/heads/cloud/$lane-claim-$t" 2>/dev/null; then task=$t; break; fi
done
[ -n "$task" ] || { echo "NO TASK"; exit 3; }
wip="cloud/wip-$lane-$task"
if git fetch -q origin "+refs/heads/$wip:refs/remotes/origin/$wip" 2>/dev/null; then
  git checkout -q -B "$lane/cloud-$task" "origin/$wip"
  git merge -q --no-edit "origin/$BASE" || echo "MERGE CONFLICT with origin/$BASE: resolve it first (git status)"
else
  git checkout -q -B "$lane/cloud-$task" "origin/$BASE"
fi
echo "$task $c" > "/tmp/$lane-claim"
echo "CLAIMED $task $c"
