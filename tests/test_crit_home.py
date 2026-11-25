"""crit's one folder (~/.crit, %USERPROFILE%\\.crit, CRIT_HOME): what goes in it, its
permissions, the move from the old folders, and the browser profile's location."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from critique_bot import config as cfg
from critique_bot.config import load_config


def _fake_os(name: str) -> types.ModuleType:
    """An ``os`` for crit's modules only (pathlib must keep the real one)."""
    fake = types.ModuleType("os")
    fake.__dict__.update(os.__dict__)
    fake.name = name
    return fake


_CHAT = {"url": "https://ai.corp.example/", "selectors": {"prompt_input": "textarea", "assistant_messages": ".a"}}


class _Home(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        env = {
            "HOME": str(self.home), "USERPROFILE": str(self.home), "CRIT_HOME": "",
            "XDG_CONFIG_HOME": str(self.home / ".config"), "APPDATA": str(self.home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(self.home / "AppData" / "Local"), "CRITIQUE_CONFIG_FILE": "",
            "CRITIQUE_CONFIG": "", "CRITIQUE_USER_DATA_DIR": "",
        }
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        system = patch.object(cfg, "system_company_config_path", lambda: self.root / "etc" / "critique-bot" / "crit.json")
        system.start()
        self.addCleanup(system.stop)
        cfg._MIGRATED = False
        self.addCleanup(setattr, cfg, "_MIGRATED", False)
        from critique_bot import egress

        policy = egress.policy()
        self.addCleanup(setattr, egress, "_policy", policy)
        self.crit = self.home / ".crit"


class LayoutTests(_Home):
    def test_everything_is_in_one_folder(self) -> None:
        self.assertEqual(cfg.crit_home(), self.crit)
        self.assertEqual(cfg.user_config_path(), self.crit / "config.json")
        self.assertEqual(cfg.default_browser_profile(), self.crit / "browser-profile")
        self.assertEqual(cfg.dedicated_edge_user_data_dir(), self.crit / "msedge-user-data")
        self.assertEqual(cfg.company_cache_path(), self.crit / "cache" / "config-file.cache.json")

    def test_crit_home_moves_it(self) -> None:
        with patch.dict(os.environ, {"CRIT_HOME": str(self.root / "elsewhere")}):
            self.assertEqual(cfg.user_config_path(), self.root / "elsewhere" / "config.json")

    def test_windows_uses_userprofile(self) -> None:
        with patch.object(cfg, "os", _fake_os("nt")), patch.dict(os.environ, {"USERPROFILE": str(self.root / "Users" / "me")}):
            self.assertEqual(cfg.crit_home(), self.root / "Users" / "me" / ".crit")
            self.assertEqual(Path(cfg._resolve_user_data_dir("")), self.root / "Users" / "me" / ".crit" / "browser-profile")

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_private_permissions(self) -> None:
        cfg.write_config_file(cfg.user_config_path(), {"backend": "local"})
        self.assertEqual(self.crit.stat().st_mode & 0o777, 0o700)
        self.assertEqual(cfg.user_config_path().stat().st_mode & 0o777, 0o600)
        cfg.write_config_file(cfg.company_cache_path(), {})
        self.assertEqual((self.crit / "cache").stat().st_mode & 0o777, 0o700)


class ProfileTests(_Home):
    def _load(self, data: dict, path: Path | None = None):
        path = path or cfg.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return load_config(path)

    def test_linux_default_and_the_old_relative_default(self) -> None:
        self.assertEqual(self._load(_CHAT).user_data_dir, str((self.crit / "browser-profile").resolve()))
        self.assertEqual(
            self._load({**_CHAT, "user_data_dir": ".edge-profile"}).user_data_dir, str((self.crit / "browser-profile").resolve())
        )

    def test_system_absolute_and_env_still_work(self) -> None:
        self.assertEqual(self._load({**_CHAT, "user_data_dir": "system"}).user_data_dir, str(cfg.system_edge_user_data_dir()))
        mine = self.root / "my-profile"
        self.assertEqual(self._load({**_CHAT, "user_data_dir": str(mine)}).user_data_dir, str(mine.resolve()))
        with patch.dict(os.environ, {"CRITIQUE_USER_DATA_DIR": str(self.root / "env-profile")}):
            self.assertEqual(self._load(_CHAT).user_data_dir, str((self.root / "env-profile").resolve()))

    def test_a_config_files_user_data_dir_is_ignored(self) -> None:
        team = self.root / "share" / "crit.json"
        team.parent.mkdir()
        team.write_text(json.dumps({**_CHAT, "user_data_dir": "/shared/profile"}), encoding="utf-8")
        with patch.object(cfg.log, "info") as info:
            loaded = self._load({"config_file": str(team)})
        self.assertEqual(loaded.user_data_dir, str((self.crit / "browser-profile").resolve()))
        self.assertTrue(any("user_data_dir in the config file is ignored" in str(c.args[0]) for c in info.call_args_list))

    def test_the_shared_file_used_directly_is_never_the_profiles_base(self) -> None:
        system = cfg.system_company_config_path()
        loaded = self._load({**_CHAT, "user_data_dir": "profile"}, system)
        self.assertEqual(loaded.user_data_dir, str((self.crit / "browser-profile").resolve()))
        self.assertFalse(loaded.user_data_dir.startswith(str(system.parent)))


class MigrationTests(_Home):
    def _old(self) -> Path:
        old = self.home / ".config" / "critique-bot"
        (old / ".edge-profile" / "Default").mkdir(parents=True)
        (old / ".edge-profile" / "Default" / "Cookies").write_text("c")
        (old / "config.json").write_text(json.dumps(_CHAT))
        (old / "config-file.cache.json").write_text("{}")
        return old

    def _migrate(self) -> tuple[bool, list[str]]:
        said: list[str] = []
        return cfg.migrate_legacy_files(said.append), said

    def test_config_cache_and_profile_move_once(self) -> None:
        old = self._old()
        dedicated = old / "msedge-user-data"
        dedicated.mkdir()
        moved, said = self._migrate()
        self.assertTrue(moved)
        self.assertEqual(said, [f"Moved crit's files to {self.crit}."])
        self.assertEqual(json.loads((self.crit / "config.json").read_text()), _CHAT)
        self.assertEqual((self.crit / "browser-profile" / "Default" / "Cookies").read_text(), "c")
        self.assertTrue((self.crit / "msedge-user-data").is_dir())
        self.assertTrue((self.crit / "cache" / "config-file.cache.json").is_file())
        self.assertFalse((old / "config.json").exists())
        self.assertFalse((old / ".edge-profile").exists())
        if os.name != "nt":
            self.assertEqual(self.crit.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.crit / "config.json").stat().st_mode & 0o777, 0o600)
        cfg._MIGRATED = False
        self.assertEqual(self._migrate(), (False, []))  # nothing left to move

    def test_windows_old_folders(self) -> None:
        appdata = self.home / "AppData" / "Roaming" / "critique-bot"
        appdata.mkdir(parents=True)
        (appdata / "config.json").write_text("{}")
        local = self.home / "AppData" / "Local" / "critique-bot" / "msedge-user-data"
        local.mkdir(parents=True)
        with patch.object(cfg, "os", _fake_os("nt")):
            moved, said = self._migrate()
        self.assertTrue(moved)
        self.assertTrue((self.crit / "config.json").is_file())
        self.assertTrue((self.crit / "msedge-user-data").is_dir())
        self.assertFalse(local.exists())

    def test_a_locked_profile_stays_and_the_config_is_copied(self) -> None:
        old = self._old()
        real_move = shutil.move

        def move(src, dst):
            if src.endswith(".edge-profile"):
                raise PermissionError(13, "in use")
            return real_move(src, dst)

        with patch.object(cfg.shutil, "move", side_effect=move):
            moved, said = self._migrate()
        self.assertTrue(moved)
        self.assertEqual(said[0], f"Moved crit's files to {self.crit}.")
        self.assertIn("could not be moved (in use); it stays there", said[1])
        self.assertTrue((old / ".edge-profile").is_dir())
        self.assertFalse((self.crit / "browser-profile").exists())
        self.assertTrue((old / "config.json").is_file())  # copied, not moved
        self.assertTrue((self.crit / "config.json").is_file())

    def test_nothing_happens_when_the_new_config_exists(self) -> None:
        self._old()
        cfg.write_config_file(cfg.user_config_path(), {"backend": "local"})
        self.assertEqual(self._migrate(), (False, []))
        self.assertEqual(json.loads(cfg.user_config_path().read_text()), {"backend": "local"})

    def test_the_command_line_moves_them_without_a_word(self) -> None:
        self._old()
        from critique_bot.cli import _migrate_legacy_files

        err = io.StringIO()
        with redirect_stderr(err):
            _migrate_legacy_files()
        self.assertEqual(err.getvalue(), "")
        self.assertIn(f"Moved crit's files to {self.crit}.", __import__("critique_bot.log", fromlist=["x"]).log_file_path().read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
