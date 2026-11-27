# SPDX-License-Identifier: Apache-2.0
"""Where extensions live, and which project files the user has approved.

Two scopes, the same layout in each:

```
~/.crit/ (user)            .crit/ in the project (project)
  tools/*.toml|*.json        manifest tools
  agents/*.md                agents
  commands/*.md              slash commands
  hooks.toml | hooks.json    hooks
  mcp.json                   MCP servers
```

The user's own files are trusted. A project's files came with the code: a cloned
repository must not run anything just by being opened (the same rule as
``project_home.SECURITY_KEYS``). So project files that grant power (tools, hooks, MCP
servers) stay inactive until the user approves them, once per content hash, with
``crit trust <file>``. Editing an approved file makes it inactive again until it is
approved again. Project agents and commands are prompts: they load without approval,
but cannot loosen the permission mode unless approved.

An approval covers what the file runs, not only the file: every project file it names
(``[python3, .crit/hooks/check.py]``, ``./tools/run.sh``) is hashed with it, and a change
to any of them (or one that appears later where the file points) turns the file off again.

The same store holds the approvals of a project's ``.crit/settings.json``: the values that
loosen crit (``project_home.APPROVAL_KEYS``) count only once the user approved them
(:func:`approve_digest`), whether or not git tracks anything.

The approvals live in ``~/.crit/trust.json``, never in the project.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

__all__ = [
    "KINDS",
    "ExtFile",
    "approve_digest",
    "covered_files",
    "digest_approved",
    "discover",
    "file_hash",
    "grants_power",
    "is_trusted",
    "protected_files",
    "referenced_files",
    "trust",
    "trust_path",
    "trusted",
    "untrust",
    "user_dir",
    "project_ext_dir",
]

#: Extension kinds: the folder or file name in a scope.
KINDS = ("tools", "agents", "commands", "hooks", "mcp")
#: Kinds whose project files stay off until approved.
POWER_KINDS = frozenset({"tools", "hooks", "mcp"})
#: Larger files are skipped (an extension file is a few KB).
MAX_FILE_BYTES = 512_000


@dataclass(frozen=True)
class ExtFile:
    kind: str
    #: "user", "project", or "builtin".
    scope: str
    path: Path

    @property
    def needs_trust(self) -> bool:
        return self.scope == "project" and self.kind in POWER_KINDS

    def active(self) -> bool:
        """True when this file may be used now."""
        return not self.needs_trust or is_trusted(self.path)


def user_dir() -> Path:
    from critique_bot.config import crit_home

    return crit_home()


def project_ext_dir(root: Path) -> Path:
    from critique_bot.project_home import project_dir

    return project_dir(Path(root), migrate=False)


def _files_in(base: Path, kind: str, scope: str) -> list[ExtFile]:
    found: list[ExtFile] = []
    if kind in {"tools", "agents", "commands"}:
        folder = base / kind
        patterns = ("*.toml", "*.json") if kind == "tools" else ("*.md",)
        if folder.is_dir():
            for pattern in patterns:
                for path in sorted(folder.glob(pattern)):
                    if path.is_file():
                        found.append(ExtFile(kind, scope, path))
    elif kind == "hooks":
        for name in ("hooks.toml", "hooks.json"):
            if (base / name).is_file():
                found.append(ExtFile(kind, scope, base / name))
    elif kind == "mcp":
        if (base / "mcp.json").is_file():
            found.append(ExtFile(kind, scope, base / "mcp.json"))
    return found


def discover(root: Path | None, kinds: Iterable[str] = KINDS) -> list[ExtFile]:
    """Every extension file, user scope first, then the project's (which wins on a name clash)."""
    wanted = [kind for kind in kinds if kind in KINDS]
    out: list[ExtFile] = []
    user = user_dir()
    for kind in wanted:
        out.extend(_files_in(user, kind, "user"))
    if root is not None:
        project = project_ext_dir(root)
        try:
            same = project.resolve() == user.resolve()
        except OSError:
            same = False
        if not same:
            for kind in wanted:
                out.extend(_files_in(project, kind, "project"))
    return out


def grants_power(file: ExtFile) -> bool:
    return file.kind in POWER_KINDS


# --------------------------------------------------------------------------- approvals

def trust_path() -> Path:
    return user_dir() / "trust.json"


def file_hash(path: Path) -> str:
    data = Path(path).read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"{path} is larger than {MAX_FILE_BYTES} bytes")
    return hashlib.sha256(data).hexdigest()


def _key(path: Path) -> str:
    return str(Path(os.path.abspath(path)))


def _load() -> dict:
    try:
        data = json.loads(trust_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"files": {}}
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return {"files": {}}
    return data


def _save(data: dict) -> None:
    path = trust_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".trust-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- what a file runs

#: A program an extension names is hashed in full up to this size (a larger one is refused).
MAX_COVERED_BYTES = 64_000_000


def project_root_of(path: Path) -> Path:
    """The project a ``.crit`` (or ``.bot``) file belongs to: the folder that holds that folder.

    Relative paths in the file are run from there (hooks, tools and MCP servers start in the
    workspace). A file outside any ``.crit`` folder: its own folder.
    """
    from critique_bot.config import crit_home

    path = Path(os.path.abspath(path))
    try:
        home_project = Path(os.path.abspath(crit_home())) / "projects" / "home"
        if path == home_project or home_project in path.parents:
            return Path(os.path.abspath(os.path.expanduser("~")))
    except OSError:
        pass
    for parent in path.parents:
        if parent.name in {".crit", ".bot"}:
            return parent.parent
    return path.parent


def _strings_in(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings_in(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings_in(item)


def _parse(data: bytes, name: str) -> object:
    text = data.decode("utf-8", errors="replace")
    if name.endswith(".toml"):
        import tomllib

        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _path_like(token: str) -> bool:
    return "/" in token or "\\" in token or token.startswith(".")


def referenced_files(path: Path, root: Path | None = None, *, data: bytes | None = None) -> list[Path]:
    """Files inside ``root`` that the extension file at ``path`` names: what it would run or load.

    Every string in the file (and every word of a string with spaces) is read as a path
    relative to ``root`` (and to any ``cwd`` the file sets) or as an absolute path. Those that
    land inside the project count, whether they exist now or not: a script that appears later
    where an approved hook points must not run unapproved. Folders do not count.
    """
    root = Path(os.path.abspath(root if root is not None else project_root_of(path)))
    try:
        raw = data if data is not None else Path(path).read_bytes()
    except OSError:
        return []
    parsed = _parse(raw, Path(path).name)
    if parsed is None:
        return []
    strings = list(_strings_in(parsed))
    bases = [root]
    if isinstance(parsed, dict):
        for cwd in _cwd_values(parsed):
            base = Path(cwd) if Path(cwd).is_absolute() else root / cwd
            bases.append(Path(os.path.abspath(base)))
    real_root = Path(os.path.realpath(root))
    found: dict[str, Path] = {}
    for text in strings:
        if len(text) > 4096 or "\0" in text or "://" in text:
            continue
        try:
            words = shlex.split(text) if any(ch.isspace() for ch in text) else [text]
        except ValueError:
            words = text.split()
        for word in words:
            if not word or word.startswith("-"):
                if "=" not in word:
                    continue
                word = word.split("=", 1)[1]  # --config=./x.py
                if not word:
                    continue
            for base in bases:
                candidate = Path(word) if os.path.isabs(word) else base / word
                candidate = Path(os.path.abspath(candidate))
                inside = candidate == root or root in candidate.parents
                if not inside:
                    try:
                        real = Path(os.path.realpath(candidate))
                        inside = real == real_root or real_root in real.parents
                    except OSError:
                        inside = False
                if not inside or candidate == root or candidate.is_dir():
                    continue
                if candidate.exists() or _path_like(word):
                    found.setdefault(str(candidate), candidate)
    return [found[key] for key in sorted(found)]


def _cwd_values(data: dict) -> list[str]:
    out: list[str] = []
    stack: list[object] = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key == "cwd" and isinstance(value, str) and value:
                    out.append(value)
                else:
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
    return out


def _content_hash(path: Path) -> str | None:
    """sha256 of a covered file, or None when it does not exist (or is not a regular file)."""
    digest = hashlib.sha256()
    try:
        if not Path(path).is_file():
            return None
        size = 0
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                size += len(block)
                if size > MAX_COVERED_BYTES:
                    return "too-large"
                digest.update(block)
    except OSError:
        return "unreadable"
    return digest.hexdigest()


def _covers(path: Path, data: bytes) -> dict[str, str | None]:
    return {str(item): _content_hash(item) for item in referenced_files(path, data=data)}


# --------------------------------------------------------------------------- approvals of extension files


def is_trusted(path: Path) -> bool:
    """True when ``path``'s current content, and every project file it runs, is what the user approved."""
    entry = _load()["files"].get(_key(path))
    if not isinstance(entry, dict):
        return False
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    if len(data) > MAX_FILE_BYTES or entry.get("sha256") != hashlib.sha256(data).hexdigest():
        return False
    covers = entry.get("covers")
    if not isinstance(covers, dict):
        # Approved before approvals covered what a file runs: fine only if it runs no project file.
        return not referenced_files(path, data=data)
    if set(covers) != set(_covers(path, data)):
        return False  # the project moved (the same file names other paths now)
    return all(_content_hash(Path(name)) == digest for name, digest in covers.items())


def trust(path: Path, *, kind: str = "") -> str:
    """Approve ``path`` and the project files it runs, as they are now; returns the file's hash."""
    data = Path(path).read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"{path} is larger than {MAX_FILE_BYTES} bytes")
    covers = _covers(path, data)
    too_large = [name for name, digest in covers.items() if digest in {"too-large", "unreadable"}]
    if too_large:
        raise ValueError(f"cannot approve {path}: it runs {', '.join(too_large)}, which crit cannot read "
                         f"(or is larger than {MAX_COVERED_BYTES} bytes)")
    digest = hashlib.sha256(data).hexdigest()
    store = _load()
    store["files"][_key(path)] = {
        "sha256": digest,
        "kind": kind,
        "covers": covers,
        "approved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _save(store)
    return digest


def covered_files(path: Path) -> dict[str, str | None]:
    """The project files an approval of ``path`` covers now: path -> sha256 (None: not there yet)."""
    try:
        return _covers(path, Path(path).read_bytes())
    except OSError:
        return {}


def untrust(path: Path) -> bool:
    data = _load()
    removed = data["files"].pop(_key(path), None) is not None
    removed = data.setdefault("settings", {}).pop(_key(path), None) is not None or removed
    if removed:
        _save(data)
    return removed


def trusted() -> dict[str, dict]:
    """Every approval: path -> {sha256, kind, approved, covers}."""
    return dict(_load()["files"])


def state_of(path: Path) -> str:
    """"approved", "changed" (approved once, edited since), or "new"."""
    entry = _load()["files"].get(_key(path))
    if not isinstance(entry, dict):
        return "new"
    return "approved" if is_trusted(path) else "changed"


def protected_files(workspace: Path | None) -> list[Path]:
    """Files the model must not write: every program an approved or user-scope extension runs.

    That is what the approvals in ``trust.json`` cover, plus the project files named by the
    user's own (``~/.crit``) and the project's tool, hook and MCP files. A model that rewrote
    one would run its own code the next time the hook or tool fires.
    """
    out: dict[str, Path] = {}
    for entry in _load()["files"].values():
        if isinstance(entry, dict) and isinstance(entry.get("covers"), dict):
            for name in entry["covers"]:
                out.setdefault(name, Path(name))
    for item in discover(workspace, sorted(POWER_KINDS)):
        root = Path(workspace) if item.scope == "user" and workspace is not None else project_root_of(item.path)
        for found in referenced_files(item.path, root):
            out.setdefault(str(found), found)
    return list(out.values())


# --------------------------------------------------------------------------- approvals of values


def digest_approved(path: Path, digest: str) -> bool:
    """True when the user approved ``digest`` for ``path`` (a project's settings file)."""
    entry = _load().get("settings", {}).get(_key(path))
    return isinstance(entry, dict) and entry.get("sha256") == digest


def approve_digest(path: Path, digest: str, *, kind: str = "settings", keys: Iterable[str] = ()) -> None:
    """Record that the user approved ``digest`` for ``path``."""
    data = _load()
    data.setdefault("settings", {})[_key(path)] = {
        "sha256": digest,
        "kind": kind,
        "keys": sorted(keys),
        "approved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _save(data)


def settings_approvals() -> dict[str, dict]:
    """Every approval of project settings values: path -> {sha256, keys, approved}."""
    found = _load().get("settings")
    return dict(found) if isinstance(found, dict) else {}
