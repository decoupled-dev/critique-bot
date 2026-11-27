#!/bin/sh
# main renamed MAX_LEVEL and added logging to set_level; release fixed set_level to clamp the
# level. Cherry-picking the release fix onto main conflicts in hvac/fan.py.
set -e
export GIT_AUTHOR_NAME=Dev GIT_AUTHOR_EMAIL=dev@example.com GIT_COMMITTER_NAME=Dev GIT_COMMITTER_EMAIL=dev@example.com
c() { GIT_AUTHOR_DATE="$1 +0000" GIT_COMMITTER_DATE="$1 +0000" git commit -qam "$2"; }
mkdir -p hvac
cat > hvac/__init__.py <<'PY'
PY
cat > hvac/fan.py <<'PY'
"""Blower fan control for one HVAC zone."""

MAX_LEVEL = 7


class Fan:
    def __init__(self, name):
        self.name = name
        self.level = 0


def set_level(fan, level):
    """Set the fan level and return the level applied."""
    fan.level = level
    return fan.level
PY
printf '# Changelog\n\n## 2.1.0\n- First release with zone fans\n' > CHANGELOG.md
printf '2.1.0\n' > VERSION
git add -A
c 1760000000 "hvac: fan control for zone fans"
git branch release
# main: refactor + logging
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
    log.info("fan %s -> %d", fan.name, level)
    fan.level = level
    return fan.level
PY
c 1760003600 "fan: rename MAX_LEVEL to MAX_FAN_LEVEL and log level changes"
printf '\n## Development\nRun python3 -m unittest from the top folder.\n' > README.md
git add README.md
c 1760007200 "docs: how to run the tests"
# release: version bump, then the fix
git checkout -q release
printf '2.1.1\n' > VERSION
c 1760010800 "release: bump version to 2.1.1"
cat > hvac/fan.py <<'PY'
"""Blower fan control for one HVAC zone."""

MAX_LEVEL = 7


class Fan:
    def __init__(self, name):
        self.name = name
        self.level = 0


def set_level(fan, level):
    """Set the fan level and return the level applied."""
    level = max(0, min(level, MAX_LEVEL))
    fan.level = level
    return fan.level
PY
printf '# Changelog\n\n## 2.1.1\n- Clamp the fan level to the supported range\n\n## 2.1.0\n- First release with zone fans\n' > CHANGELOG.md
c 1760014400 "fan: clamp the level to the supported range"
git checkout -q main
