"""A project's ``.crit`` folder: settings, notes, cache, sessions, and the code index.

Versions up to 1.0.4 called it ``.bot``: a project with ``.bot`` and no ``.crit`` gets its folder
renamed the first time crit looks at it (the log file says so). When both exist, ``.crit`` is
used and ``.bot`` is left alone.

The home folder as a project would put its folder at ``~/.crit``, which is crit's own folder;
that project's data goes in ``~/.crit/projects/home`` instead.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from critique_bot import log
from critique_bot.code_index import IndexStats, rebuild_index
from critique_bot.output import private_dir, restrict_dir

BOT_DIR_NAME = ".crit"
#: The folder's name up to 1.0.4.
LEGACY_DIR_NAME = ".bot"
#: Inside crit's own folder: the data of a project whose folder is the home folder.
PROJECTS_DIR_NAME = "projects"
HOME_PROJECT_NAME = "home"
SETTINGS_NAME = "settings.json"
NOTES_NAME = "AGENT.md"
# Transcripts, the prompt history, and caches hold code and tasks: never committed.
# settings.json is the user's own: a committed copy loses its permission keys (SECURITY_KEYS).
_GITIGNORE_LINES = (".crit/cache/", ".crit/sessions/", ".crit/history", ".crit/settings.json")
_LOCAL_GITIGNORE = "cache/\nsessions/\nhistory\nsettings.json\n"
NOTES_TEMPLATE = """# Project notes for crit

Lines below the marker are sent to the model at the start of every session,
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


# Settings that loosen what the agent may do or where the conversation goes. A cloned
# repository could ship them in a committed ``.crit/settings.json``; they count only when
# the file is the user's own (not tracked by git).
SECURITY_KEYS = frozenset(
    {
        "permissions",
        "mode",
        "allow",
        "confirm_risky",
        "web_fetch",
        "web_fetch_hosts",
        "network_commands",
        "check_command",
        "auto_questions",
    }
)


class BotHomeError(RuntimeError):
    """The workspace ``.crit`` folder could not be created or read."""


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
        """The config named in settings. Raises BotHomeError when git tracks it in this project.

        A committed config.json would let a cloned repository choose the chat address,
        allowed hosts, or a remote browser, and every file the agent reads would go there.
        """
        raw = self.settings.get("config")
        if not raw or not isinstance(raw, str):
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = self.root / path
        if git_tracks(self.root, path):
            raise BotHomeError(
                f"{path} is committed in this repository (or git could not confirm it is not; see the "
                "warning above), so crit does not use it: a config that comes "
                "with the code could send your files to another site. Keep config.json out of git "
                "(git rm --cached it and add it to .gitignore), or pass --config with your own copy."
            )
        return path

    def project_notes(self) -> str:
        """Text after the marker in ``.crit/AGENT.md``, or the whole file without one."""
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
    """Settings win. Otherwise the test line in ``.crit/AGENT.md`` is the check."""
    raw = settings.get("check_command") if isinstance(settings, dict) else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    found = check_command_from_notes(notes)
    return found or None


def _is_home_folder(root: Path) -> bool:
    """True when ``root`` is the user's home folder (or the folder that holds crit's own folder)."""
    from critique_bot.config import crit_home

    try:
        here = Path(os.path.realpath(root))
        if here == Path(os.path.realpath(os.path.expanduser("~"))):
            return True
        return Path(os.path.realpath(crit_home())) == here / BOT_DIR_NAME
    except OSError:
        return False


def project_dir(root: Path, *, migrate: bool = True) -> Path:
    """The ``.crit`` folder of the project at ``root`` (``~/.crit/projects/home`` for the home folder).

    With ``migrate``, a leftover ``.bot`` folder (and no ``.crit``) is renamed to it first.
    """
    root = Path(root)
    if _is_home_folder(root):
        from critique_bot.config import crit_home

        target = crit_home() / PROJECTS_DIR_NAME / HOME_PROJECT_NAME
    else:
        target = root / BOT_DIR_NAME
    if migrate:
        _migrate_legacy_dir(root, target)
    return target


def legacy_project_dir(root: Path) -> Path:
    """Where versions up to 1.0.4 kept the project's folder (``.bot``)."""
    return Path(root) / LEGACY_DIR_NAME


def _migrate_legacy_dir(root: Path, target: Path) -> None:
    old = legacy_project_dir(root)
    try:
        if target.exists() or target.is_symlink() or not old.is_dir() or old.is_symlink():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old, target)
    except OSError as exc:
        log.info(f"could not rename {old} to {target}: {exc}")
        return
    log.info(f"renamed {old} to {target}")


def find_bot_home(start: Path) -> BotHome | None:
    """Walk upward from ``start`` until a ``.crit/settings.json`` (or an old ``.bot`` one) exists."""
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        settings_path = project_dir(candidate, migrate=False) / SETTINGS_NAME
        if not settings_path.is_file() and (legacy_project_dir(candidate) / SETTINGS_NAME).is_file():
            settings_path = project_dir(candidate) / SETTINGS_NAME
        if settings_path.is_file():
            return _load(candidate, settings_path)
    return None


def init_bot_home(workspace: Path) -> BotHome:
    """Create ``.crit`` in ``workspace``, then rebuild the code index.

    Existing ``settings.json`` and ``AGENT.md`` are left as they are. Missing
    directories are created. ``config.json`` in the workspace is recorded only
    on first init.
    """
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise BotHomeError(f"workspace is not a directory: {root}")
    bot_dir = project_dir(root)
    if bot_dir.parent != root:
        from critique_bot.config import ensure_crit_home

        ensure_crit_home()
        _private_dir(bot_dir.parent)
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
    if bot_dir.parent == root:
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
    _restrict_existing(bot_dir)
    ignored = sorted(key for key in raw if key in SECURITY_KEYS)
    if ignored and git_tracks(root, settings_path):
        log.warn(
            f"{settings_path} is committed in this repository (or git could not confirm it is not); "
            f"ignoring {', '.join(ignored)} from it. "
            "Settings that loosen permissions count only in your own, untracked settings file."
        )
        raw = {key: value for key, value in raw.items() if key not in SECURITY_KEYS}
    return BotHome(
        root=root,
        bot_dir=bot_dir,
        settings_path=settings_path,
        cache_dir=bot_dir / "cache",
        sessions_dir=bot_dir / "sessions",
        index_path=bot_dir / "cache" / "index.sqlite",
        settings=raw,
    )


def git_tracks(root: Path, path: Path) -> bool:
    """True when git tracks ``path``, or when git cannot say for sure.

    This guards settings and configs that a cloned repository could ship, so it
    fails closed: no git, a timeout, or a git error all count as tracked. Only a
    clear answer gives False: ``path`` is not inside any git work tree, or git
    lists it as untracked. The repository is found from ``path`` itself, so a
    workspace that is a subfolder of a repository (or a path outside ``root``)
    is still checked against the right repository.
    """
    del root  # the repository is the one that holds ``path``
    target = Path(os.path.abspath(path))
    probe = target.parent
    while not probe.is_dir() and probe != probe.parent:
        probe = probe.parent
    exe = shutil.which("git")
    if exe is None:
        log.warn(f"git is not installed; cannot confirm {target} is not committed, so treating it as committed")
        return True
    try:
        top = subprocess.run(
            [exe, "-C", str(probe), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warn(f"git failed ({exc}); cannot confirm {target} is not committed, so treating it as committed")
        return True
    if top.returncode != 0:
        if "not a git repository" in (top.stderr or "").lower():
            return False
        log.warn(
            f"git rev-parse failed in {probe}: {(top.stderr or '').strip()}; "
            f"treating {target} as committed"
        )
        return True
    toplevel = Path(top.stdout.strip())
    try:
        # The link itself (not where it points) is what git would track.
        real = Path(os.path.realpath(target.parent)) / target.name
        relative = real.relative_to(Path(os.path.realpath(toplevel)))
    except ValueError:
        log.warn(f"{target} is outside the git work tree {toplevel}; treating it as committed")
        return True
    try:
        proc = subprocess.run(
            [exe, "-C", str(toplevel), "ls-files", "--error-unmatch", "--", relative.as_posix()],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warn(f"git ls-files failed ({exc}); treating {target} as committed")
        return True
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    log.warn(f"git ls-files failed: {(proc.stderr or '').strip()}; treating {target} as committed")
    return True


def update_settings(home: BotHome, **values: object) -> None:
    """Merge ``values`` into ``.crit/settings.json`` and keep ``home.settings`` in step.

    The file is written to a temp file and moved into place, so a crash or
    Ctrl+C never leaves half a JSON file behind.
    """
    home.settings.update(values)
    # Merge into the file as it is: keys left out of home.settings (see SECURITY_KEYS) stay.
    try:
        on_disk = json.loads(home.settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        on_disk = {}
    if not isinstance(on_disk, dict):
        on_disk = {}
    on_disk.update(values)
    _atomic_write_text(home.settings_path, json.dumps(on_disk, indent=2) + "\n")


def add_allow_rule(home: BotHome, rule: str) -> None:
    """Append ``rule`` to ``"allow"`` in ``.crit/settings.json`` (once), keeping the file's other keys."""
    try:
        on_disk = json.loads(home.settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        on_disk = {}
    current = on_disk.get("allow") if isinstance(on_disk, dict) else None
    rules = [item for item in current if isinstance(item, str)] if isinstance(current, list) else []
    if rule in rules:
        return
    update_settings(home, allow=rules + [rule])


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


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` if needed and make it owner-only; return it.

    Call this every time a folder under ``.crit`` that holds code or transcripts
    (sessions, cache, undo copies) is created or written to. POSIX: 0700.
    Windows: icacls, best effort (a warning is logged on failure).
    """
    return private_dir(path)


def _private_dir(path: Path) -> None:
    ensure_private_dir(path)


def _restrict_existing(bot_dir: Path) -> None:
    """Narrow ``.crit``, ``.crit/sessions``, ``.crit/cache`` (and undo) when they already exist."""
    for folder in (bot_dir, bot_dir / "sessions", bot_dir / "cache", bot_dir / "cache" / "undo"):
        if folder.is_dir() and not folder.is_symlink():
            restrict_dir(folder)


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
    # Diagnostics only (--logs): the terminal shows plain messages, not bookkeeping.
    log.info(f"indexed {stats.files} files, {stats.symbols} symbols in {workspace}")
