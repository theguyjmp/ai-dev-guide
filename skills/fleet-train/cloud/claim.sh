#!/usr/bin/env bash
# Claim ONE assigned row and start its branch. Prints the assignment JSON, or "NO ROW" (exit 3) when none is free.
# A claim is a push of a brand-new ref cloud/claim-<row>: a unique commit, so a second session's push is rejected.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"
BASE=${CLOUD_BASE:-cloud-base}   # the train machine's main, mirrored by cloud.py collect
QA=/tmp/harness-qa               # cloud.py rewrites the quality commands to run from here
KIT=${HARNESS_CLOUD_KIT:-tools/cloud}
git config user.email >/dev/null || git config user.email "cloud-builder@example.invalid"
git config user.name >/dev/null || git config user.name "cloud builder"
A=/tmp/assignment.json
# until the kit is on the base branch it is copied in from harness-cloud: keep it out of the row's commits
git ls-files --error-unmatch "$KIT/BUILDER.md" >/dev/null 2>&1 || grep -qx "$KIT/" .git/info/exclude 2>/dev/null \
  || echo "$KIT/" >> .git/info/exclude
field() { python3 -c 'import json,sys;v=json.load(open(sys.argv[1])).get(sys.argv[2]) or "";print(" ".join(v) if isinstance(v,list) else v["branch"] if isinstance(v,dict) else v)' "$A" "$1"; }
git fetch -q origin "+refs/heads/$BASE:refs/remotes/origin/$BASE" +refs/heads/harness-cloud:refs/remotes/origin/harness-cloud || exit 1
taken=$(git ls-remote origin 'refs/heads/cloud/claim-*' | sed 's#.*refs/heads/cloud/claim-##')
row=""
for f in $(git ls-tree --name-only origin/harness-cloud assign/ | sort); do
  r=$(basename "$f" .json)
  echo "$taken" | grep -qx "$r" && continue
  c=$(git commit-tree "origin/$BASE^{tree}" -p "origin/$BASE" -m "claim $r $(hostname) $(date +%s%N)")
  if git push -q origin "$c:refs/heads/cloud/claim-$r" 2>/dev/null; then row=$r; break; fi
done
[ -n "$row" ] || { echo "NO ROW"; exit 3; }
mkdir -p "$QA" && for p in $(git ls-tree --name-only origin/harness-cloud qa/); do
  git show "origin/harness-cloud:$p" > "$QA/$(basename "$p")"; done
git show "origin/harness-cloud:assign/$row.json" > "$A"
git checkout -q -B "cloud/$row" "origin/$BASE"
# deps still waiting for the land train (stack, merged clean or skipped), then the earlier attempt to salvage
for s in $(field stack); do
  git fetch -q origin "+refs/heads/${s#origin/}:refs/remotes/$s" && git merge -q --no-edit -m "stack: ${s##*-}" "$s" \
    || { git merge --abort 2>/dev/null; echo "stack $s did not merge clean: skipped"; }
done
prev=$(field previous_branch)
if [ -n "$prev" ]; then
  git fetch -q origin "+refs/heads/${prev#origin/}:refs/remotes/$prev"
  git merge -q --no-edit "$prev" || echo "MERGE CONFLICT with $prev: resolve it first (git status)"
fi
cat "$A"
