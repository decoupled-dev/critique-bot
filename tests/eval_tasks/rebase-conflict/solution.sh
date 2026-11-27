#!/bin/sh
# Reference: rebase feature onto main, keeping main's line then feature's line.
set -e
export GIT_AUTHOR_NAME=eval GIT_AUTHOR_EMAIL=eval@localhost GIT_COMMITTER_NAME=eval GIT_COMMITTER_EMAIL=eval@localhost
git checkout -q feature
git rebase -q main >/dev/null 2>&1 || true
printf 'hello\nfrom main\nfrom feature\n' > greeting.txt
git add greeting.txt
GIT_EDITOR=true git rebase --continue >/dev/null
