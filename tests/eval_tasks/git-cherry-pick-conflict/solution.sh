#!/bin/sh
# Reference: cherry-pick with -x, resolve fan.py per the rules, continue.
set -e
export GIT_AUTHOR_NAME=Dev GIT_AUTHOR_EMAIL=dev@example.com GIT_COMMITTER_NAME=Dev GIT_COMMITTER_EMAIL=dev@example.com
fix=$(git rev-list -1 --grep='^fan: clamp the level' release)
if ! git cherry-pick -x "$fix" >/dev/null 2>&1; then
  cat > hvac/fan.py <<'PY'
"""Blower fan control for one HVAC zone."""

import logging

log = logging.getLogger("hvac.fan")

MAX_FAN_LEVEL = 7


class Fan:
    def __init__(self, name):
        self.name = name
        self.level = 0


def set_level(fan, level):
    """Set the fan level and return the level applied."""
    level = max(0, min(level, MAX_FAN_LEVEL))
    log.info("fan %s -> %d", fan.name, level)
    fan.level = level
    return fan.level
PY
  git add hvac/fan.py CHANGELOG.md
  GIT_EDITOR=true git cherry-pick --continue >/dev/null
fi
