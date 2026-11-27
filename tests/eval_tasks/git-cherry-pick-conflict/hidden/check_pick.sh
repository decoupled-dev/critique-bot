#!/bin/sh
# Pass when the release fix (and only it) is cherry-picked onto main as one commit with -x,
# resolved per the rules, and the repo is left clean.
fail() { echo "$1"; exit 1; }
[ -e .git/CHERRY_PICK_HEAD ] && fail "a cherry-pick is still in progress"
[ -d .git/sequencer ] && fail "a cherry-pick sequence is still in progress"
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || fail "main is not checked out"
git diff --quiet HEAD || fail "uncommitted changes to tracked files"
fix=$(git rev-list -1 --grep='^fan: clamp the level to the supported range$' release) || fail "release lost its fix"
[ "$(git log -1 --format=%s release)" = "fan: clamp the level to the supported range" ] || fail "release was changed"
[ "$(git rev-list --count release)" = 4 ] || fail "release was changed"
[ "$(git log -1 --format=%s main~1)" = "docs: how to run the tests" ] || fail "main should be the old main plus exactly one commit"
[ "$(git rev-list --count main)" = 5 ] || fail "main should be the old main plus exactly one commit"
[ "$(git rev-list --merges --count main)" = 0 ] || fail "main has a merge commit"
[ "$(git log -1 --format=%s main)" = "fan: clamp the level to the supported range" ] || fail "the new commit should keep the fix's subject"
git log -1 --format=%B main | grep -q "(cherry picked from commit $fix)" || fail "commit message lacks the -x line for $fix"
[ "$(git show main:VERSION)" = "2.1.0" ] || fail "VERSION changed on main (the version bump must not come along)"
git show main:CHANGELOG.md | grep -q 'Clamp the fan level to the supported range' || fail "CHANGELOG entry from the fix is missing"
git diff --quiet main -- hvac || fail "hvac/ in the working tree differs from main"
grep -nE '^(<<<<<<<|=======|>>>>>>>)' hvac/fan.py CHANGELOG.md && fail "conflict markers left"
grep -qE '(^|[^_])MAX_LEVEL' hvac/fan.py && fail "the old name MAX_LEVEL is back"
python3 - <<'PY' || fail "set_level behaves wrong"
import logging, sys
sys.path.insert(0, ".")
from hvac import fan as m
records = []
class H(logging.Handler):
    def emit(self, r): records.append(r.getMessage())
logging.getLogger("hvac.fan").addHandler(H()); logging.getLogger("hvac.fan").setLevel(logging.DEBUG)
f = m.Fan("rear")
assert m.MAX_FAN_LEVEL == 7, "MAX_FAN_LEVEL"
assert m.set_level(f, 12) == 7 and f.level == 7, "clamp high"
assert m.set_level(f, -3) == 0 and f.level == 0, "clamp low"
assert m.set_level(f, 4) == 4, "in range"
assert records == ["fan rear -> 7", "fan rear -> 0", "fan rear -> 4"], f"log should show the clamped level: {records}"
PY
echo ok
