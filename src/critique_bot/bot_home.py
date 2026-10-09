"""``.bot`` folder for the agent: settings, notes, cache, sessions, and the code index."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from critique_bot import log
from critique_bot.code_index import IndexStats, rebuild_index

BOT_DIR_NAME = ".bot"
SETTINGS_NAME = "settings.json"
NOTES_NAME = "AGENT.md"
# Transcripts, the prompt history, and caches hold code and tasks: never committed.
_GITIGNORE_LINES = (".bot/cache/", ".bot/sessions/", ".bot/history")
_LOCAL_GITIGNORE = "cache/\nsessions/\nhistory\n"
NOTES_TEMPLATE = """# Project notes for crit

Lines below the marker are sent to the chat at the start of every session,
like CLAUDE.md or .cursorrules. Nothing is sent while the space below is
empty. Keep the notes short and specific, for example:

- Test command: python -m unittest discover -s tests
- Kotlin uses 4-space indent; do not reformat files you did not change.
- Never edit files under generated/.

<!-- notes start -->
"""
_NOTES_MARKER = "<!-- notes start -->"
_CHECK_LINE = re.compile(
    r"(?im)^[ \t>*-]*(?:test with|test command|check with|check command)\s*:\s*(.+?)\s*$"
)


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


def check_command_from_notes(notes: str) -> str:
    """The finish-line command written in project notes, or "" if there is none.

    A line such as ``Test with: pytest -q`` is the command. ``Build with:`` is
    not. A ``check_command`` in settings overrides this.
    """
    match = _CHECK_LINE.search(notes or "")
    if match is None:
        return ""
    return match.group(1).strip().strip("`")


def resolve_check_command(settings: dict, notes: str) -> str | None:
    """Settings win. Otherwise the test line in ``.bot/AGENT.md`` is the check."""
    raw = settings.get("check_command") if isinstance(settings, dict) else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    found = check_command_from_notes(notes)
    return found or None


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
    for private in (bot_dir, cache_dir, cache_dir / "undo", sessions_dir):
        _private_dir(private)
    local_ignore = bot_dir / ".gitignore"
    if not local_ignore.exists():
        local_ignore.write_text(_LOCAL_GITIGNORE, encoding="utf-8")
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


def update_settings(home: BotHome, **values: object) -> None:
    """Merge ``values`` into ``.bot/settings.json`` and keep ``home.settings`` in step.

    The file is written to a temp file and moved into place, so a crash or
    Ctrl+C never leaves half a JSON file behind.
    """
    home.settings.update(values)
    _atomic_write_text(home.settings_path, json.dumps(home.settings, indent=2) + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _private_dir(path: Path) -> None:
    """Create ``path``; on POSIX only the owner can read it (transcripts, undo copies)."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            log.warn(f"could not restrict {path}: {exc}")


def _append_gitignore(workspace: Path) -> None:
    path = workspace / ".gitignore"
    if not path.is_file():
        return
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BotHomeError(f"could not read {path}: {exc}") from exc
    text = raw.decode("utf-8", errors="surrogateescape")
    present = {line.strip() for line in text.splitlines()}
    missing = [line for line in _GITIGNORE_LINES if line not in present]
    if not missing:
        return
    newline = "\r\n" if "\r\n" in text else "\n"
    suffix = "" if text == "" or text.endswith(("\n", "\r")) else newline
    addition = suffix + newline.join(missing) + newline
    try:
        with path.open("ab") as handle:
            handle.write(addition.encode("utf-8"))
    except OSError as exc:
        raise BotHomeError(f"could not update {path}: {exc}") from exc
    log.info(f"updated {path}")


def _print_stats(workspace: Path, stats: IndexStats) -> None:
    message = f"indexed {stats.files} files, {stats.symbols} symbols in {workspace}"
    log.info(message)
    print(message, flush=True)
