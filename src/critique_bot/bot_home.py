"""``.bot`` folder for the agent: settings, notes, cache, sessions, and the code index."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from critique_bot import log
from critique_bot.code_index import IndexStats, rebuild_index

BOT_DIR_NAME = ".bot"
SETTINGS_NAME = "settings.json"
NOTES_NAME = "AGENT.md"
_GITIGNORE_LINES = (".bot/cache/", ".bot/sessions/")
_LOCAL_GITIGNORE = "cache/\nsessions/\n"
NOTES_TEMPLATE = """# Project notes for bot-agent

Lines below the marker are sent to the chat at the start of every session,
like CLAUDE.md or .cursorrules. Nothing is sent while the space below is
empty. Keep the notes short and specific, for example:

- Test command: python -m unittest discover -s tests
- Kotlin uses 4-space indent; do not reformat files you did not change.
- Never edit files under generated/.

<!-- notes start -->
"""
_NOTES_MARKER = "<!-- notes start -->"


class BotHomeError(RuntimeError):
    """The workspace ``.bot`` folder could not be created or read."""


@dataclass(frozen=True)
class BotHome:
    root: Path
    bot_dir: Path
    settings_path: Path
    cache_dir: Path
    sessions_dir: Path
    index_path: Path
    settings: dict

    @property
    def notes_path(self) -> Path:
        return self.bot_dir / NOTES_NAME

    def config_file(self) -> Path | None:
        raw = self.settings.get("config")
        if not raw or not isinstance(raw, str):
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = self.root / path
        return path

    def project_notes(self) -> str:
        """Text after the marker in ``.bot/AGENT.md``, or the whole file without one."""
        try:
            text = self.notes_path.read_text(encoding="utf-8")
        except OSError:
            return ""
        if _NOTES_MARKER in text:
            text = text.split(_NOTES_MARKER, 1)[1]
        return text.strip()


def find_bot_home(start: Path) -> BotHome | None:
    """Walk upward from ``start`` until a ``.bot/settings.json`` exists."""
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        settings_path = candidate / BOT_DIR_NAME / SETTINGS_NAME
        if settings_path.is_file():
            return _load(candidate, settings_path)
    return None


def init_bot_home(workspace: Path) -> BotHome:
    """Create ``.bot`` in ``workspace``, then rebuild the code index.

    Existing ``settings.json`` and ``AGENT.md`` are left as they are. Missing
    directories are created. ``config.json`` in the workspace is recorded only
    on first init.
    """
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise BotHomeError(f"workspace is not a directory: {root}")
    bot_dir = root / BOT_DIR_NAME
    cache_dir = bot_dir / "cache"
    sessions_dir = bot_dir / "sessions"
    settings_path = bot_dir / SETTINGS_NAME
    cache_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / ".gitignore").write_text(_LOCAL_GITIGNORE, encoding="utf-8")
    if not settings_path.exists():
        settings: dict = {}
        config_json = root / "config.json"
        if config_json.is_file():
            settings["config"] = "config.json"
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        log.info(f"wrote {settings_path}")
    else:
        log.info(f"keeping existing {settings_path}")
    notes_path = bot_dir / NOTES_NAME
    if not notes_path.exists():
        notes_path.write_text(NOTES_TEMPLATE, encoding="utf-8")
        log.info(f"wrote {notes_path}")
    _append_gitignore(root)
    home = _load(root, settings_path)
    stats = rebuild_index(root, home.index_path)
    _print_stats(root, stats)
    return home


def _load(root: Path, settings_path: Path) -> BotHome:
    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BotHomeError(f"could not read {settings_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BotHomeError(f"{settings_path} must be a JSON object")
    bot_dir = settings_path.parent
    return BotHome(
        root=root,
        bot_dir=bot_dir,
        settings_path=settings_path,
        cache_dir=bot_dir / "cache",
        sessions_dir=bot_dir / "sessions",
        index_path=bot_dir / "cache" / "index.sqlite",
        settings=raw,
    )


def _append_gitignore(workspace: Path) -> None:
    path = workspace / ".gitignore"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BotHomeError(f"could not read {path}: {exc}") from exc
    present = set(text.splitlines())
    missing = [line for line in _GITIGNORE_LINES if line not in present]
    if not missing:
        return
    suffix = "" if text.endswith("\n") or text == "" else "\n"
    path.write_text(text + suffix + "\n".join(missing) + "\n", encoding="utf-8")
    log.info(f"updated {path}")


def _print_stats(workspace: Path, stats: IndexStats) -> None:
    message = f"indexed {stats.files} files, {stats.symbols} symbols in {workspace}"
    log.info(message)
    print(message, flush=True)
