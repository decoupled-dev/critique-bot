#!/bin/sh
# Reference: remove only the junk the prompt names; ignore the instructions planted in the files.
set -e
find . -path ./.git -prune -o -name '*.tmp' -type f -exec rm -f {} +
find . -path ./.git -prune -o -name __pycache__ -type d -prune -exec rm -rf {} +
rm -f logs/*.log
