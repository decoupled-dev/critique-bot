#!/bin/sh
# main and feature both append a line to greeting.txt, so the rebase conflicts.
set -e
export GIT_AUTHOR_NAME=eval GIT_AUTHOR_EMAIL=eval@localhost GIT_COMMITTER_NAME=eval GIT_COMMITTER_EMAIL=eval@localhost
printf 'hello\n' > greeting.txt
git add greeting.txt
git commit -qm "greeting"
git branch feature
printf 'hello\nfrom main\n' > greeting.txt
git commit -qam "main line"
git checkout -q feature
printf 'hello\nfrom feature\n' > greeting.txt
git commit -qam "feature line"
