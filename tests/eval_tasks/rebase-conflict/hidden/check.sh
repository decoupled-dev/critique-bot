#!/bin/sh
# Pass when feature sits on top of main, no rebase is in progress, and both lines are kept in order.
fail() { echo "$1"; exit 1; }
[ "$(git rev-parse --abbrev-ref HEAD)" = "feature" ] || fail "feature is not checked out"
[ -d .git/rebase-merge ] || [ -d .git/rebase-apply ] && fail "a rebase is still in progress"
git merge-base --is-ancestor main feature || fail "feature is not on top of main"
[ "$(printf 'hello\nfrom main\nfrom feature\n')" = "$(cat greeting.txt)" ] || fail "greeting.txt is wrong: $(cat greeting.txt)"
git diff --quiet HEAD || fail "uncommitted changes left"
echo ok
