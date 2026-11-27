# SPDX-License-Identifier: Apache-2.0
"""Domain packs: built-in, trusted bundles of tools, agents and defaults for one kind of work.

A pack lives in ``critique_bot/packs/<name>/``:

```
packs/aosp/
  tools/*.json     manifest tools (JSON, so Python 3.10 works), one tool per area with an action
  agents/*.md      file-defined agents (Stage 6 format)
  __init__.py      HANDLERS (Python behind some actions), SANDBOX_PRESET
```

A pack is off until the settings name it (``"packs": ["aosp"]`` in the user's own
``.crit/settings.json`` or config; a committed settings file cannot turn one on, see
``project_home.SECURITY_KEYS``). When the workspace looks like a tree a pack is for and no
pack is set, crit offers it once (:func:`offer`). Its tools and agents load through the
extension loader (``extensions.session.load_session``) as built-in content: no ``crit
trust`` needed, and only packs may name Python handlers in a manifest. An active pack also
picks its sandbox preset when the settings name none (:func:`sandbox_value`).

Tools layer: the handlers use the tool implementations (log_tools, soong, indexing).
"""

from __future__ import annotations

import importlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["AVAILABLE", "Pack", "activate", "active", "agent_dirs", "detect", "load", "offer", "requested",
           "sandbox_value"]

#: Packs that ship with crit.
AVAILABLE = ("aosp",)

_lock = threading.Lock()
_active: list[str] = []


@dataclass
class Pack:
    name: str
    folder: Path
    handlers: dict[str, Any] = field(default_factory=dict)
    sandbox_preset: str = ""
    description: str = ""

    @property
    def tool_files(self) -> list[Path]:
        return sorted((self.folder / "tools").glob("*.json"))

    @property
    def agent_dir(self) -> Path:
        return self.folder / "agents"


def folder(name: str) -> Path:
    return Path(__file__).resolve().parent / name


def load(name: str) -> Pack:
    """The pack called ``name``. Raises KeyError for a pack crit does not ship."""
    if name not in AVAILABLE:
        raise KeyError(name)
    module = importlib.import_module(f"critique_bot.packs.{name}")
    return Pack(name, folder(name), dict(getattr(module, "HANDLERS", {})), str(getattr(module, "SANDBOX_PRESET", "")),
                str(getattr(module, "DESCRIPTION", "")))


def requested(settings: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """The packs ``settings`` turn on, and notes about names crit does not know."""
    raw = (settings or {}).get("packs")
    if raw in (None, "", []):
        return [], []
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    names: list[str] = []
    notes: list[str] = []
    for item in items:
        key = str(item).strip().lower()
        if key in AVAILABLE:
            if key not in names:
                names.append(key)
        else:
            notes.append(f"pack {item!r} is not known (known: {', '.join(AVAILABLE)}); it is ignored")
    if not isinstance(raw, (str, list)):
        notes.append('"packs" must be a list of pack names, e.g. ["aosp"]')
    return names, notes


def activate(names: list[str]) -> None:
    """The packs of the current session (load_session sets them; agent listings read them)."""
    with _lock:
        _active[:] = [name for name in names if name in AVAILABLE]


def active() -> list[str]:
    with _lock:
        return list(_active)


def agent_dirs() -> list[Path]:
    """Agent folders of the active packs, for extensions.agent_files."""
    return [folder(name) / "agents" for name in active() if (folder(name) / "agents").is_dir()]


def sandbox_value(settings: dict[str, Any] | None) -> Any:
    """``settings["sandbox"]`` with the active pack's preset filled in when it names none."""
    options = settings if isinstance(settings, dict) else {}
    value = options.get("sandbox")
    names, _notes = requested(options)
    preset = ""
    for name in names:
        try:
            preset = load(name).sandbox_preset
        except (KeyError, ImportError):
            continue
        if preset:
            break
    if not preset:
        return value
    if value in (None, "", {}):
        return {"preset": preset}
    if isinstance(value, str):
        return {"mode": value, "preset": preset}
    if isinstance(value, dict) and not value.get("preset"):
        return {**value, "preset": preset}
    return value


# --------------------------------------------------------------------------- detection and the offer


def detect(workspace: Path) -> list[str]:
    """Packs this workspace looks like it wants (an AOSP tree: build/envsetup.sh and Android.bp files)."""
    root = Path(workspace)
    found: list[str] = []
    if (root / "build" / "envsetup.sh").exists() and (
        (root / "Android.bp").exists() or (root / "build" / "soong" / "Android.bp").exists()
        or (root / ".repo").is_dir()
    ):
        found.append("aosp")
    return found


_OFFERED = "packs-offered.json"


def offer(workspace: Path, settings: dict[str, Any] | None, state_dir: Path | None) -> str:
    """One sentence offering a pack this tree looks like it wants, the first time only ("" otherwise).

    ``state_dir`` is the project's ``.crit/cache``; the offer is recorded there. Nothing is offered
    when the settings already name packs (or say ``"packs": []``).
    """
    if (settings or {}).get("packs") is not None:
        return ""
    wanted = detect(workspace)
    if not wanted or state_dir is None or not Path(state_dir).is_dir():
        return ""
    marker = Path(state_dir) / _OFFERED
    try:
        done = set(json.loads(marker.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        done = set()
    new = [name for name in wanted if name not in done]
    if not new:
        return ""
    try:
        marker.write_text(json.dumps(sorted(done | set(new))) + "\n", encoding="utf-8")
    except OSError:
        return ""
    pack = load(new[0])
    return (f"This looks like an AOSP tree. The {pack.name} pack adds {pack.description} To turn it on, add "
            f'"packs": ["{pack.name}"] to .crit/settings.json (say "packs": [] there to stop this note).')
