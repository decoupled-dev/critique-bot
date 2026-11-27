# SPDX-License-Identifier: Apache-2.0
"""The audit log: ``~/.crit/logs/audit.jsonl``, one JSON object per line.

What crit did on this machine and why, for a person to check afterwards (``crit audit``):

* ``command``     a command the agent ran: the command line, cwd, sandbox mode, exit code
* ``write``       a file the agent wrote, edited, moved, or deleted
* ``egress``      a request through the egress proxy: host, port, method, bytes, allowed or denied
* ``permission``  a permission decision: what was asked, the mode, allowed or denied, and why
* ``taint``       the session became tainted (and by what), or ``/trust`` lifted it
* ``model``       a model endpoint crit contacted
* ``sandbox``     the sandbox a session runs in (or why it could not)

Every record has ``ts`` (UTC, ISO 8601), ``kind``, and ``session`` (set by :func:`set_session`).
Text values are redacted like the log file. The file is owner-only and rotates at about
5 MB (three files kept). Writing never raises: an audit problem must not stop the agent.

Core layer: depends on nothing above ``config`` and ``redaction``.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

FILE_NAME = "audit.jsonl"
MAX_BYTES = 5 * 1024 * 1024
KEEP_FILES = 3
KINDS = ("command", "write", "egress", "permission", "taint", "model", "sandbox")

_lock = threading.Lock()
_session = ""
_disabled = False
#: Test hook: where records go instead of the file (a list), or None.
_sink: list[dict[str, Any]] | None = None


def path() -> Path:
    """``~/.crit/logs/audit.jsonl`` (in ``CRIT_HOME`` when that is set)."""
    from critique_bot.config import crit_home

    return crit_home() / "logs" / FILE_NAME


def set_session(session_id: str | None = None) -> str:
    """Name the session the next records belong to (a new random id when none is given)."""
    global _session
    _session = (session_id or "").strip() or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    return _session


def session() -> str:
    return _session


def disable(value: bool = True) -> None:
    """Stop (or resume) writing, for this process (``crit uninstall`` removes the folder)."""
    global _disabled
    _disabled = value


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        try:
            from critique_bot import redaction

            return redaction.redact_outgoing(value[:4000], report=False)
        except Exception:
            return value[:4000]
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in list(value)[:200]]
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, Path):
        return _clean(str(value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _clean(str(value))


def record(kind: str, /, **fields: Any) -> dict[str, Any]:
    """Append one record; returns it (also when writing failed)."""
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "kind": kind,
        "session": _session,
    }
    for key, value in fields.items():
        entry[f"{key}_" if key in {"ts", "kind", "session"} else key] = _clean(value)
    if _sink is not None:
        _sink.append(entry)
        return entry
    if _disabled:
        return entry
    try:
        line = json.dumps(entry, ensure_ascii=False, sort_keys=False) + "\n"
        with _lock:
            _append(path(), line)
    except Exception:  # an audit problem never stops the agent
        pass
    return entry


def _rotate(target: Path) -> None:
    for index in range(KEEP_FILES - 1, 0, -1):
        older = target.with_name(f"{target.name}.{index}")
        newer = target if index == 1 else target.with_name(f"{target.name}.{index - 1}")
        try:
            if newer.exists():
                os.replace(newer, older)
        except OSError:
            pass


def _append(target: Path, line: str) -> None:
    folder = target.parent
    if not folder.is_dir():
        folder.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            for each in (folder.parent, folder):
                try:
                    os.chmod(each, 0o700)
                except OSError:
                    pass
    try:
        size = target.stat().st_size
    except OSError:
        size = 0
    if size and size + len(line) > MAX_BYTES:
        _rotate(target)
        size = 0
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8", errors="replace") as handle:
        handle.write(line)
    if os.name != "nt" and not size:
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass


def _parse_since(value: str | None) -> datetime | None:
    """``--since``: an ISO date or time, or a span back from now (``30m``, ``2h``, ``1d``)."""
    if not value:
        return None
    text = value.strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if text[:-1].isdigit() and text[-1:].lower() in units:
        seconds = int(text[:-1]) * units[text[-1].lower()]
        return datetime.fromtimestamp(time.time() - seconds, timezone.utc)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"--since must be a date (2026-11-27), a time (2026-11-27T10:00), or a span (30m, 2h, 1d), not {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment


def read(*, session_id: str | None = None, since: str | None = None, kinds: tuple[str, ...] = ()) -> Iterator[dict[str, Any]]:
    """Records oldest first (rotated files included), filtered by session, time, and kind."""
    after = _parse_since(since)
    target = path()
    files = [target.with_name(f"{target.name}.{index}") for index in range(KEEP_FILES - 1, 0, -1)] + [target]
    for each in files:
        try:
            handle = open(each, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if session_id and not str(entry.get("session") or "").startswith(session_id):
                    continue
                if kinds and entry.get("kind") not in kinds:
                    continue
                if after is not None:
                    try:
                        moment = datetime.fromisoformat(str(entry.get("ts") or "").replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if moment < after:
                        continue
                yield entry


set_session()
