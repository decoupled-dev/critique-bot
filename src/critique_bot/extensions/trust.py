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

The same store holds the approvals of a project's ``.crit/settings.json``: the values that
loosen crit (``project_home.APPROVAL_KEYS``) count only once the user approved them
(:func:`approve_digest`), whether or not git tracks anything.

The approvals live in ``~/.crit/trust.json``, never in the project.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

__all__ = [
    "KINDS",
    "ExtFile",
    "approve_digest",
    "digest_approved",
    "discover",
    "file_hash",
    "grants_power",
    "is_trusted",
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


def is_trusted(path: Path) -> bool:
    """True when ``path``'s current content is the content the user approved."""
    entry = _load()["files"].get(_key(path))
    if not isinstance(entry, dict):
        return False
    try:
        return entry.get("sha256") == file_hash(path)
    except (OSError, ValueError):
        return False


def trust(path: Path, *, kind: str = "") -> str:
    """Approve ``path`` as it is now; returns its hash."""
    digest = file_hash(path)
    data = _load()
    data["files"][_key(path)] = {
        "sha256": digest,
        "kind": kind,
        "approved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _save(data)
    return digest


def untrust(path: Path) -> bool:
    data = _load()
    removed = data["files"].pop(_key(path), None) is not None
    removed = data.setdefault("settings", {}).pop(_key(path), None) is not None or removed
    if removed:
        _save(data)
    return removed


def trusted() -> dict[str, dict]:
    """Every approval: path -> {sha256, kind, approved}."""
    return dict(_load()["files"])


def state_of(path: Path) -> str:
    """"approved", "changed" (approved once, edited since), or "new"."""
    entry = _load()["files"].get(_key(path))
    if not isinstance(entry, dict):
        return "new"
    return "approved" if is_trusted(path) else "changed"


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
