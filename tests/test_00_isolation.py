# SPDX-License-Identifier: Apache-2.0
"""Keep the whole test run away from the real user folders.

Imported first by ``unittest discover`` (the name sorts first), before any test runs:
HOME, CRIT_HOME, the XDG folders, APPDATA, LOCALAPPDATA, and ProgramData point into a temporary
folder, the variables that name crit's files are cleared, and the system config file
location points at a file that does not exist. So no test reads or writes the person's
~/.crit, ~/.config/critique-bot, ~/.local, /etc/crit, /etc/critique-bot, or %APPDATA%.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="crit-tests-home-"))
atexit.register(shutil.rmtree, _ROOT, True)

for _name, _sub in (
    ("HOME", "home"),
    ("USERPROFILE", "home"),
    ("XDG_CONFIG_HOME", "home/.config"),
    ("XDG_DATA_HOME", "home/.local/share"),
    ("XDG_CACHE_HOME", "home/.cache"),
    ("APPDATA", "home/AppData/Roaming"),
    ("LOCALAPPDATA", "home/AppData/Local"),
    ("ProgramData", "ProgramData"),
    ("CRIT_HOME", "home/.crit"),
):
    _folder = _ROOT / _sub
    _folder.mkdir(parents=True, exist_ok=True)
    os.environ[_name] = str(_folder)
for _name in list(os.environ):
    if _name.startswith("CRITIQUE_") or _name in {
        "CRIT_CONFIG", "CRIT_CONFIG_FILE", "CRIT_URL", "CRIT_MODEL", "CRIT_API_KEY", "CRIT_API_KEY_URL", "CRIT_EVAL_SETTINGS", "CRIT_EGRESS",
        "CRIT_USER_DATA_DIR", "CRIT_CDP_URL", "CRIT_QUEUE_DIR", "CRIT_STORAGE_STATE", "CRIT_GITLAB_TOKEN",
        "CRIT_GITLAB_URL", "CRIT_GITLAB_PROXY", "CRIT_QUEUE_SHARED", "CRIT_MAX_PARALLEL_TABS",
    }:
        os.environ.pop(_name, None)

from critique_bot import config as _config  # noqa: E402

_SYSTEM = _ROOT / "etc" / "crit" / "crit.json"
#: The real function, for the tests of the location itself.
_config._real_system_company_config_path = _config.system_company_config_path  # type: ignore[attr-defined]
_config.system_company_config_path = lambda: _SYSTEM  # type: ignore[assignment]
_config._real_legacy_system_company_config_path = _config.legacy_system_company_config_path  # type: ignore[attr-defined]
_config.legacy_system_company_config_path = lambda: _ROOT / "etc" / "critique-bot-old" / "crit.json"  # type: ignore[assignment]


_USER_FOLDERS = (
    ("HOME", "home"),
    ("USERPROFILE", "home"),
    ("XDG_CONFIG_HOME", "home/.config"),
    ("XDG_DATA_HOME", "home/.local/share"),
    ("XDG_CACHE_HOME", "home/.cache"),
    ("APPDATA", "home/AppData/Roaming"),
    ("LOCALAPPDATA", "home/AppData/Local"),
    ("ProgramData", "ProgramData"),
    ("CRIT_HOME", "home/.crit"),
)
_counter = [0]
_original_run = unittest.TestCase.run


def _isolated_run(self, result=None):  # type: ignore[no-untyped-def]
    """Each test gets its own user folders (HOME, USERPROFILE, CRIT_HOME, ...), on every OS.

    Without this, what one test saves in the user config (a config_file, an installed copy)
    is still there for the next one wherever crit's folder comes from USERPROFILE (Windows).
    """
    _counter[0] += 1
    base = _ROOT / f"t{_counter[0]}"
    saved = {name: os.environ.get(name) for name, _ in _USER_FOLDERS}
    for name, sub in _USER_FOLDERS:
        folder = base / sub
        folder.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(folder)
    migrated = getattr(_config, "_MIGRATED", None)
    try:
        return _original_run(self, result)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if migrated is not None:
            _config._MIGRATED = migrated
        from critique_bot import log as _log

        _log._file_failed = ""  # crit uninstall stops the log file for its process; not for the next test
        shutil.rmtree(base, ignore_errors=True)


unittest.TestCase.run = _isolated_run  # type: ignore[method-assign]

# Never rename a .bot folder of the checkout the tests run in (tests that resolve the current folder).
from critique_bot import project_home as _project_home  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
_real_migrate = _project_home._migrate_legacy_dir


def _guarded_migrate(root, target):  # type: ignore[no-untyped-def]
    if Path(root).resolve() == _REPO:
        return
    _real_migrate(root, target)


_project_home._migrate_legacy_dir = _guarded_migrate  # type: ignore[assignment]


class IsolationTest(unittest.TestCase):
    def test_user_folders_are_temporary(self) -> None:
        self.assertTrue(str(_config.user_config_path()).startswith(str(_ROOT)))
        self.assertTrue(str(_config.crit_home()).startswith(str(_ROOT)))
        self.assertFalse(_config.system_company_config_path().exists())

    def test_each_test_has_its_own_user_folders(self) -> None:
        self.assertNotEqual(os.environ["CRIT_HOME"], str(_ROOT / "home" / ".crit"))
        self.assertEqual(os.environ["USERPROFILE"], os.environ["HOME"])
