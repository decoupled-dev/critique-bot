# SPDX-License-Identifier: Apache-2.0
"""crit's environment variables: ``CRIT_*``, with the old ``CRITIQUE_*`` names still read.

The new name wins. When only the old name is set, its value is used and the log file says so
(once per name); nothing is shown on screen.
"""

from __future__ import annotations

import os

from critique_bot import log

#: New name -> the name versions up to 1.0.4 read.
OLD_NAMES = {
    "CRIT_URL": "CRITIQUE_CHAT_URL",
    "CRIT_MODEL": "CRITIQUE_MODEL",
    "CRIT_STORAGE_STATE": "CRITIQUE_STORAGE_STATE",
    "CRIT_USER_DATA_DIR": "CRITIQUE_USER_DATA_DIR",
    "CRIT_CDP_URL": "CRITIQUE_CDP_URL",
    "CRIT_QUEUE_DIR": "CRITIQUE_QUEUE_DIR",
    "CRIT_QUEUE_SHARED": "CRITIQUE_QUEUE_SHARED",
    "CRIT_MAX_PARALLEL_TABS": "CRITIQUE_MAX_PARALLEL_TABS",
    "CRIT_CONFIG": "CRITIQUE_CONFIG",
    "CRIT_CONFIG_FILE": "CRITIQUE_CONFIG_FILE",
    "CRIT_EGRESS": "CRITIQUE_EGRESS",
    "CRIT_GITLAB_URL": "CRITIQUE_GITLAB_URL",
    "CRIT_GITLAB_TOKEN": "CRITIQUE_GITLAB_TOKEN",
    "CRIT_GITLAB_PROXY": "CRITIQUE_GITLAB_PROXY",
}

_reported: set[str] = set()


def old_name(name: str) -> str | None:
    return OLD_NAMES.get(name)


def get(name: str, default: str | None = None) -> str | None:
    """``os.environ.get(name)``, falling back to the old ``CRITIQUE_*`` name of the variable."""
    if name in os.environ:
        return os.environ[name]
    old = OLD_NAMES.get(name)
    if old and old in os.environ:
        if old not in _reported:
            _reported.add(old)
            log.info(f"using {old} (the old name of {name}; set {name} instead)")
        return os.environ[old]
    return default
