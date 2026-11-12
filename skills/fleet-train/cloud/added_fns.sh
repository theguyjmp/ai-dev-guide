#!/usr/bin/env bash
# The functions this branch adds outside tests (file: signature): the list review_prompt.md checks. Same rule as the
# land train's reviewer (harness.py batch_review).
base=$(git merge-base "origin/${CLOUD_BASE:-cloud-base}" HEAD)
git diff -U0 "$base" HEAD -- crates src | awk '
  /^\+\+\+ b\// { f = substr($0, 7); next }
  /^\+[ \t]*(pub(\([a-z]+\))?[ \t]+)?fn[ \t]+[A-Za-z_0-9]+/ && f !~ /\/tests\// {
    s = substr($0, 2); sub(/^[ \t]+/, "", s); print f ": " substr(s, 1, 160) }'
