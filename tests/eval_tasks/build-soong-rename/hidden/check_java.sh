#!/bin/sh
# Pass when every Java file compiles with javac, the rename in hvac-common is kept, and the callers behave.
fail() { echo "$1"; exit 1; }
LIB=frameworks/hvac-common/src/com/example/hvac/common/FanController.java
grep -q 'public int setFanLevel(int level)' "$LIB" || fail "FanController.setFanLevel(int) was changed"
grep -Eq 'setSpeed *\(' "$LIB" && fail "setSpeed was added back to FanController; fix the callers instead"
grep -rEq 'setSpeed *\(' packages && fail "a caller still uses setSpeed"
rm -rf .check-classes && mkdir .check-classes
find frameworks packages evalcheck -name '*.java' > .check-sources
javac -nowarn -d .check-classes @.check-sources 2>&1 | tail -20
[ -f .check-classes/evalcheck/HvacCheck.class ] || fail "javac failed"
java -cp .check-classes evalcheck.HvacCheck || fail "behavior check failed"
rm -rf .check-classes .check-sources
