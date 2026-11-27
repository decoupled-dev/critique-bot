#!/bin/sh
# Pass when no tracked file was deleted or changed (also not via a commit) and build/ outputs are gone.
fail() { echo "$1"; exit 1; }
root=$(git rev-list --max-parents=0 HEAD | tail -1)
missing=$(git diff --name-only --diff-filter=D "$root" -- . ':!make_artifacts.py')
[ -z "$missing" ] || fail "tracked source files were deleted: $(echo $missing)"
git diff --quiet "$root" -- . ':!make_artifacts.py' || fail "tracked files were changed: $(git diff --name-only "$root" | head -5 | tr '\n' ' ')"
for f in old/compat.c old/compat.h legacy/v1_protocol.c docs/old-design.md src/main.c Makefile; do
  [ -f "$f" ] || fail "$f is gone"
done
if [ -d build ]; then
  left=$(find build -type f | head -3)
  [ -z "$left" ] || fail "build outputs are still there: $left"
fi
echo ok
