"""Keep the whole test run away from the real user folders.

Imported first by ``unittest discover`` (the name sorts first), before any test runs:
HOME, CRIT_HOME, the XDG folders, APPDATA, LOCALAPPDATA, and ProgramData point into a temporary
folder, the variables that name crit's files are cleared, and the system config file
location points at a file that does not exist. So no test reads or writes the person's
~/.crit, ~/.config/critique-bot, ~/.local, /etc/critique-bot, or %APPDATA%.
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
for _name in ("CRITIQUE_CONFIG", "CRITIQUE_CONFIG_FILE", "CRIT_CONFIG_FILE"):
    os.environ.pop(_name, None)

from critique_bot import config as _config  # noqa: E402

_SYSTEM = _ROOT / "etc" / "critique-bot" / "crit.json"
#: The real function, for the tests of the location itself.
_config._real_system_company_config_path = _config.system_company_config_path  # type: ignore[attr-defined]
_config.system_company_config_path = lambda: _SYSTEM  # type: ignore[assignment]


class IsolationTest(unittest.TestCase):
    def test_user_folders_are_temporary(self) -> None:
        self.assertTrue(str(_config.user_config_path()).startswith(str(_ROOT)))
        self.assertTrue(str(_config.crit_home()).startswith(str(_ROOT)))
        self.assertFalse(_config.system_company_config_path().exists())
