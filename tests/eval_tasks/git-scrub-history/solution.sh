#!/bin/sh
# Reference: drop the two paths from the branch's own commits only.
set -e
git checkout -q feature/telemetry
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --index-filter \
  'git rm -q --cached --ignore-unmatch assets/sample_dump.bin config/credentials.env' \
  main..feature/telemetry >/dev/null 2>&1
git for-each-ref --format='%(refname)' refs/original | xargs -r -n1 git update-ref -d
