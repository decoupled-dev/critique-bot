"""Every agent session as an append-only event log, and what is rebuilt from it.

A session lives in ``.crit/sessions/<id>/``::

    events.jsonl   one JSON object per line, appended, never rewritten
    meta.json      id, title, created, parent (fork), model, backend, cwd, crit version, status
    blobs/<sha>    long strings (file contents, tool outputs, message lists), by SHA-256
    agent.json     the transcript ``crit eval`` reads (written by the engine at the end)

Each event carries ``seq`` (1, 2, ...), ``ts`` (UTC, ISO 8601) and ``turn`` (model replies
so far). Strings are redacted on write with the same redaction as egress
(:func:`critique_bot.redaction.redact_outgoing`), so a secret in a tool output never lands
in the log or a blob. A string longer than :data:`BLOB_MIN_CHARS` is stored once in
``blobs/`` and the event holds ``{"$blob": sha, "chars": n, "head": ...}`` instead.
Writes are flushed at once and fsync'd at turn boundaries (:meth:`SessionLog.sync`); a
reader skips a last line a hard kill cut short.

The projection :func:`history_from_events` rebuilds the message list a model API session
holds (OpenAI chat format) from the ``assistant_turn`` events' ``wire`` deltas and the
``compaction`` events; resume and fork are built on it. For a backend without a message
list (the web chat) :func:`resume_summary` gives the text a new chat starts with.

Engine layer: it imports only core modules.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from critique_bot import log

SCHEMA = 1
EVENTS_NAME = "events.jsonl"
META_NAME = "meta.json"
BLOBS_NAME = "blobs"
#: Strings at least this long go to ``blobs/`` (the event keeps a short head).
BLOB_MIN_CHARS = 2_048
BLOB_HEAD_CHARS = 160
#: Retention defaults (``sessions_keep_days`` / ``sessions_keep`` in settings).
KEEP_DAYS = 30
KEEP_COUNT = 200

EVENT_TYPES = frozenset(
    {
        "session_started",
        "session_resumed",
        "user_message",
        "system_prompt",
        "tools_declared",
        "model_request",
        "assistant_turn",
        "permission_decision",
        "tool_started",
        "tool_result",
        "checkpoint",
        "mode_changed",
        "role_changed",
        "compaction",
        "task_finished",
        "error",
    }
)

_ID_RE = re.compile(r"^\d{8}T\d{6}Z(?:-[0-9a-f]{4,})?$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_session_id(now: datetime | None = None) -> str:
    """``20261127T101500Z-1a2b``: sorts by start time, unique enough for one machine."""
    stamp = (now or _now()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(2)}"


def _redact(text: str) -> str:
    try:
        from critique_bot.redaction import redact_outgoing

        return redact_outgoing(text, report=False)
    except Exception as exc:  # redaction must never lose the event; fall back to the secret patterns
        log.debug(f"session log: redaction failed: {exc}")
        from critique_bot.redaction import redact_secrets

        return redact_secrets(text)[0]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _is_blob(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("$blob"), str)


# --------------------------------------------------------------------------- writer


class SessionLog:
    """The writer for one session. Thread-safe: parallel tool calls may record at once."""

    enabled = True

    def __init__(self, folder: Path, meta: dict[str, Any]) -> None:
        self.folder = Path(folder)
        self.meta = meta
        self.id = str(meta.get("id") or self.folder.name)
        self._lock = threading.RLock()
        self.seq = 0
        self.turn = 0
        self._dirty = False
        self._file: Any = None

    # ------------------------------------------------------------------ opening

    @classmethod
    def create(
        cls,
        sessions_dir: Path,
        *,
        session_id: str | None = None,
        title: str = "",
        parent: str = "",
        parent_seq: int | None = None,
        **meta: Any,
    ) -> "SessionLog":
        """A new session folder with ``meta.json``. Nothing is in ``events.jsonl`` yet."""
        sid = session_id or new_session_id()
        folder = Path(sessions_dir) / sid
        _private(folder)
        now = _iso(_now())
        values: dict[str, Any] = {
            "schema": SCHEMA,
            "id": sid,
            "title": _title(title),
            "created": now,
            "updated": now,
            "parent": parent or "",
            "status": "running",
            "pid": os.getpid(),
            "crit_version": _crit_version(),
        }
        if parent_seq is not None:
            values["parent_seq"] = parent_seq
        values.update({key: value for key, value in meta.items() if value is not None})
        log_ = cls(folder, values)
        log_._write_meta()
        return log_

    @classmethod
    def open(cls, folder: Path) -> "SessionLog":
        """An existing session, to append to (resume). seq and turn go on from the last event."""
        folder = Path(folder)
        meta = read_meta(folder)
        if not meta:
            meta = {"schema": SCHEMA, "id": folder.name, "created": _iso(_now())}
        log_ = cls(folder, meta)
        events = read_events(folder)
        if events:
            log_.seq = max(int(event.get("seq") or 0) for event in events)
            log_.turn = max(int(event.get("turn") or 0) for event in events)
        log_._repair_tail()
        log_.meta.update({"status": "running", "pid": os.getpid()})
        log_._write_meta()
        return log_

    def _repair_tail(self) -> None:
        """A hard kill may leave half a line: end it, so the next event starts on its own line."""
        path = self.folder / EVENTS_NAME
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    return
                handle.seek(-1, os.SEEK_END)
                last = handle.read(1)
        except OSError:
            return
        if last != b"\n":
            with open(path, "ab") as handle:
                handle.write(b"\n")

    # ------------------------------------------------------------------ writing

    def write(self, event_type: str, /, **fields: Any) -> dict[str, Any]:
        """Append one event and return it (as written: redacted, long strings as blob refs)."""
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown session event type: {event_type}")
        with self._lock:
            self.seq += 1
            if event_type == "assistant_turn":
                self.turn += 1
            event: dict[str, Any] = {"seq": self.seq, "ts": _iso(_now()), "turn": self.turn, "type": event_type}
            for key, value in fields.items():
                if value is None:
                    continue
                event[key] = self._store(value)
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            try:
                handle = self._handle()
                handle.write(line + "\n")
                handle.flush()
                self._dirty = True
            except OSError as exc:
                log.warn(f"session log: could not write to {self.folder}: {exc}")
            return event

    def _handle(self) -> Any:
        if self._file is None:
            _private(self.folder)
            self._file = open(self.folder / EVENTS_NAME, "a", encoding="utf-8", newline="\n")
        return self._file

    def _store(self, value: Any, *, raw: bool = False) -> Any:
        """``value`` redacted, with long strings moved to blobs. ``raw``: already redacted."""
        if isinstance(value, str):
            text = value if raw else _redact(value)
            if len(text) >= BLOB_MIN_CHARS:
                sha = self._put(text)
                return {"$blob": sha, "chars": len(text), "head": text[:BLOB_HEAD_CHARS]}
            return text
        if isinstance(value, dict):
            if _is_blob(value):
                return value
            return {str(key): self._store(item, raw=raw) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._store(item, raw=raw) for item in value]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return self._store(str(value), raw=raw)

    def put_blob(self, text: str) -> str:
        """Store ``text`` (redacted) in ``blobs/`` and return its id."""
        with self._lock:
            return self._put(_redact(text))

    def _put(self, text: str) -> str:
        sha = text_hash(text)
        folder = self.folder / BLOBS_NAME
        path = folder / sha
        if not path.exists():
            try:
                folder.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(".tmp")
                temp.write_text(text, encoding="utf-8", errors="surrogatepass")
                os.replace(temp, path)
            except OSError as exc:
                log.warn(f"session log: could not store a blob: {exc}")
        return sha

    def sync(self) -> None:
        """fsync the log: called at turn boundaries, so a hard kill loses at most the open turn."""
        with self._lock:
            if self._file is None or not self._dirty:
                return
            try:
                self._file.flush()
                os.fsync(self._file.fileno())
            except (OSError, ValueError) as exc:
                log.debug(f"session log: fsync failed: {exc}")
            self._dirty = False

    def update_meta(self, **values: Any) -> None:
        with self._lock:
            for key, value in values.items():
                if key == "title":
                    value = _title(str(value or ""))
                self.meta[key] = value
            self.meta["updated"] = _iso(_now())
            self._write_meta()

    def _write_meta(self) -> None:
        try:
            _private(self.folder)
            _atomic_json(self.folder / META_NAME, self.meta)
        except OSError as exc:
            log.warn(f"session log: could not write {META_NAME}: {exc}")

    def close(self, status: str | None = None) -> None:
        with self._lock:
            self.sync()
            if status:
                self.meta["status"] = status
            self.meta.pop("pid", None)
            self.meta["updated"] = _iso(_now())
            self._write_meta()
            if self._file is not None:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None

    # ------------------------------------------------------------------ one exchange with the model

    #: The prefix the session's first message must start with (set by the engine), for the prefix hash.
    prefix: str = ""

    def request(self, session: Any, outgoing: str, **fields: Any) -> int | None:
        """A ``model_request`` event before ``session.send``. Returns the mark for :meth:`reply`:
        the length of the session's message list (None for a session without one)."""
        history = _history(session)
        mark = len(history) if history is not None else None
        values: dict[str, Any] = {"chars": len(outgoing), "messages": mark}
        if history:
            first = str(history[0].get("content") or "")
            if self.prefix:
                values["prefix_hash"] = text_hash(first[: len(self.prefix)])[:16]
                values["prefix_ok"] = first.startswith(self.prefix)
        tools = getattr(session, "tools", None)
        if isinstance(tools, list) and tools:
            values["tools_hash"] = text_hash(json.dumps(tools, sort_keys=True))[:16]
        if history is None:
            values["payload"] = outgoing  # no message list to rebuild from: keep what was sent
        values.update(fields)
        self.write("model_request", **values)
        return mark

    def reply(self, session: Any, mark: int | None, reply: str) -> None:
        """The ``assistant_turn`` event after a reply, with the messages the exchange added (``wire``)."""
        detail = getattr(session, "last_detail", None)
        detail = detail if isinstance(detail, dict) else {}
        wire = None
        if mark is not None:
            history = _history(session)
            if history is not None:
                wire = history[mark:] if len(history) >= mark else history
        usage = {
            "prompt": detail.get("prompt_eval_count"),
            "completion": detail.get("eval_count"),
            "cached": detail.get("cached_count"),
        }
        self.write(
            "assistant_turn",
            text=reply,
            tool_calls=detail.get("tool_calls"),
            reasoning=detail.get("reasoning"),
            usage={key: value for key, value in usage.items() if isinstance(value, int)} or None,
            parse_notes=detail.get("parse_notes"),
            completion=detail.get("completion"),
            seconds=detail.get("seconds"),
            wire=wire,
        )
        self.sync()

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.flush()
                except (OSError, ValueError):
                    pass
        return read_events(self.folder)

    def load(self, value: Any) -> Any:
        return resolve(self.folder, value)


class NullLog:
    """No session log (tests, helper tabs): every call does nothing."""

    enabled = False
    id = ""
    folder: Path | None = None
    seq = 0
    turn = 0
    meta: dict[str, Any] = {}

    def write(self, event_type: str, /, **fields: Any) -> dict[str, Any]:
        return {}

    prefix = ""

    def put_blob(self, text: str) -> str:
        return ""

    def request(self, session: Any, outgoing: str, **fields: Any) -> int | None:
        return None

    def reply(self, session: Any, mark: int | None, reply: str) -> None:
        return None

    def sync(self) -> None:
        return None

    def update_meta(self, **values: Any) -> None:
        return None

    def close(self, status: str | None = None) -> None:
        return None

    def events(self) -> list[dict[str, Any]]:
        return []

    def load(self, value: Any) -> Any:
        return value


def _history(session: Any) -> list[dict[str, Any]] | None:
    """The session's message list (a model API session's ``history()``), or None (the web chat)."""
    getter = getattr(session, "history", None)
    if not callable(getter):
        return None
    try:
        found = getter()
    except Exception:
        return None
    return list(found) if isinstance(found, list) else None


# --------------------------------------------------------------------------- reading


def read_events(folder: Path, *, upto: int | None = None) -> list[dict[str, Any]]:
    """The events in order. A line that does not parse (a hard kill mid-write) is skipped."""
    path = Path(folder) / EVENTS_NAME
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if upto is not None and int(event.get("seq") or 0) > upto:
            break
        events.append(event)
    return events


def read_meta(folder: Path) -> dict[str, Any]:
    try:
        data = json.loads((Path(folder) / META_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_blob(folder: Path, ref: Any) -> str:
    """The text of a blob (``{"$blob": sha}`` or the sha); "" when it is missing."""
    sha = ref.get("$blob") if isinstance(ref, dict) else ref
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        return ""
    try:
        return (Path(folder) / BLOBS_NAME / sha).read_text(encoding="utf-8", errors="surrogatepass")
    except OSError:
        return ""


def resolve(folder: Path, value: Any) -> Any:
    """``value`` with every blob reference replaced by its text."""
    if _is_blob(value):
        text = load_blob(folder, value)
        return text if text or not value.get("head") else str(value.get("head"))
    if isinstance(value, dict):
        return {key: resolve(folder, item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(folder, item) for item in value]
    return value


# --------------------------------------------------------------------------- projection


@dataclass
class Projection:
    """What a session's events add up to."""

    #: The message list a model API session holds now (OpenAI chat format).
    messages: list[dict[str, Any]] = field(default_factory=list)
    #: True when some exchange carried a message list (a model API backend).
    has_wire: bool = False
    #: seq of the last complete model reply.
    last_turn_seq: int = 0
    #: Tool calls of the last reply that have no result (the run stopped while they ran).
    unanswered_calls: list[str] = field(default_factory=list)
    #: "task -> outcome" per finished task, oldest first.
    tasks: list[str] = field(default_factory=list)
    #: The task that was running when the log ends without task_finished, or "".
    open_task: str = ""


def history_from_events(events: Iterable[dict[str, Any]], folder: Path | None = None) -> Projection:
    """Rebuild the conversation from ``events``.

    ``assistant_turn`` events carry ``wire``: the messages that exchange added to the
    session's list (the user or tool messages sent, then the reply). A ``compaction``
    event changes the list as it changed in the session: ``new_chat`` empties it,
    ``mask`` replaces the content of some messages, ``summarize`` (or any event with a
    ``history``) replaces the whole list. A request with no reply (killed mid-turn) adds
    nothing, so the projection ends at the last complete turn.
    """
    out = Projection()
    messages: list[dict[str, Any]] = []
    task = ""

    def load(value: Any) -> Any:
        return resolve(folder, value) if folder is not None else value

    for event in events:
        kind = event.get("type")
        if kind == "user_message" and event.get("task"):
            task = str(load(event.get("text")) or "")
        elif kind == "task_finished":
            label = " ".join(str(load(event.get("task")) or task).split())[:100]
            out.tasks.append(f"{label} -> {event.get('code') or '?'}")
            task = ""
        elif kind == "assistant_turn":
            out.last_turn_seq = int(event.get("seq") or 0)
            wire = event.get("wire")
            calls = [str(call.get("id") or call.get("call_id") or "") for call in event.get("tool_calls") or []
                     if isinstance(call, dict)]
            out.unanswered_calls = calls
            if isinstance(wire, list):
                out.has_wire = True
                messages.extend(load(item) for item in wire if isinstance(item, dict))
        elif kind == "tool_result":
            call_id = str(event.get("call_id") or "")
            if call_id in out.unanswered_calls:
                out.unanswered_calls.remove(call_id)
            elif out.unanswered_calls and not call_id:
                out.unanswered_calls.pop(0)
        elif kind == "compaction":
            strategy = event.get("strategy")
            if strategy == "new_chat":
                messages = []
            elif event.get("history") is not None:
                history = load(event.get("history"))
                if isinstance(history, str):
                    try:
                        history = json.loads(history)
                    except ValueError:
                        history = []
                messages = [dict(item) for item in history or [] if isinstance(item, dict)]
            elif strategy == "mask":
                for item in event.get("masked") or []:
                    index = item.get("index") if isinstance(item, dict) else None
                    if isinstance(index, int) and 0 <= index < len(messages):
                        messages[index] = {**messages[index], "content": load(item.get("text")) or ""}
    out.messages = [dict(item) for item in messages]
    out.open_task = task
    return out


def resume_summary(events: list[dict[str, Any]], folder: Path | None = None, *, limit: int = 6_000) -> str:
    """What a new conversation needs to carry on a logged session: tasks, the open task's
    progress (files touched, last command, the model's last words), and how it ended."""
    proj = history_from_events(events, folder)

    def load(value: Any) -> str:
        found = resolve(folder, value) if folder is not None else value
        return str(found or "")

    lines = ["RESUMED SESSION (crit restored it from its log; the earlier conversation is not shown)"]
    if proj.tasks:
        lines.append("Tasks done earlier, oldest first:\n- " + "\n- ".join(proj.tasks[-8:]))
    if proj.open_task:
        lines.append("The task that was running when the session stopped:\n" + proj.open_task.strip()[:2_000])
        changed: list[str] = []
        last_command = ""
        last_words = ""
        for event in events:
            kind = event.get("type")
            if kind == "user_message" and event.get("task"):
                changed, last_command, last_words = [], "", ""
            elif kind == "tool_result":
                path = str(event.get("path") or "")
                if event.get("ok") and event.get("mutating") and path and path not in changed:
                    changed.append(path)
                if event.get("tool") == "run_command":
                    last_command = f"{event.get('summary') or ''}"
            elif kind == "assistant_turn":
                text = load(event.get("text")).strip()
                if text and not text.startswith('{"output"'):
                    last_words = text
        if changed:
            lines.append("Files changed so far: " + ", ".join(changed[:20]))
        if last_command:
            lines.append("Last command: " + last_command[:300])
        if last_words:
            lines.append("Your last words:\n" + last_words[-1_500:])
        if proj.unanswered_calls:
            lines.append("The last tool calls did not finish (crit stopped); check the state before repeating them.")
    text = "\n".join(lines)
    return text[-limit:] if len(text) > limit else text


# --------------------------------------------------------------------------- sessions on disk


@dataclass
class SessionInfo:
    id: str
    folder: Path
    title: str
    created: str
    updated: str
    status: str
    parent: str = ""
    tokens: int = 0
    turns: int = 0


def _alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        return True  # no cheap check; the status stays "running"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def list_sessions(sessions_dir: Path) -> list[SessionInfo]:
    """Sessions with an event log, newest first. A "running" one whose process is gone is "killed"."""
    root = Path(sessions_dir)
    try:
        folders = [path for path in root.iterdir() if path.is_dir() and (path / EVENTS_NAME).is_file()]
    except OSError:
        return []
    rows: list[SessionInfo] = []
    for folder in folders:
        meta = read_meta(folder)
        status = str(meta.get("status") or "unknown")
        if status == "running" and not _alive(meta.get("pid")):
            status = "killed"
        tokens = meta.get("tokens") if isinstance(meta.get("tokens"), dict) else {}
        rows.append(
            SessionInfo(
                id=str(meta.get("id") or folder.name),
                folder=folder,
                title=str(meta.get("title") or ""),
                created=str(meta.get("created") or ""),
                updated=str(meta.get("updated") or meta.get("created") or ""),
                status=status,
                parent=str(meta.get("parent") or ""),
                tokens=int(tokens.get("prompt") or 0) + int(tokens.get("completion") or 0),
                turns=int(meta.get("turns") or 0),
            )
        )
    rows.sort(key=lambda row: (row.updated, row.id), reverse=True)
    return rows


class SessionNotFound(LookupError):
    pass


def find_session(sessions_dir: Path, ref: str | None) -> Path:
    """The folder for ``ref``: an id, a unique prefix of one, or ``last``/None for the newest."""
    rows = list_sessions(sessions_dir)
    if not rows:
        raise SessionNotFound("no sessions with a log in this project yet")
    wanted = (ref or "").strip()
    if not wanted or wanted in {"last", "--last"}:
        return rows[0].folder
    exact = [row for row in rows if row.id == wanted]
    if exact:
        return exact[0].folder
    matches = [row for row in rows if row.id.startswith(wanted) or row.id.endswith(wanted)]
    if len(matches) == 1:
        return matches[0].folder
    if matches:
        raise SessionNotFound(f"{wanted!r} matches {len(matches)} sessions; give more of the id")
    raise SessionNotFound(f"no session {wanted!r} (crit sessions lists them)")


def fork_session(sessions_dir: Path, ref: str | None, *, at: int | None = None) -> SessionLog:
    """A new session whose log is ``ref``'s events up to seq ``at`` (all when None).

    The events are copied unchanged (same seq), the blobs they use are linked or copied,
    and ``meta.json`` names the parent. History rebuilt from the fork equals the parent's
    history at ``at``.
    """
    source = find_session(sessions_dir, ref)
    events = read_events(source, upto=at)
    if at is not None and (not events or int(events[-1].get("seq") or 0) < 1):
        raise SessionNotFound(f"session {source.name} has no event at or before seq {at}")
    meta = read_meta(source)
    fork = SessionLog.create(
        sessions_dir,
        title=str(meta.get("title") or ""),
        parent=source.name,
        parent_seq=int(events[-1].get("seq") or 0) if events else 0,
        model=meta.get("model"),
        backend=meta.get("backend"),
        cwd=meta.get("cwd"),
    )
    shas: set[str] = set()
    _collect_blobs(events, shas)
    if shas:
        (fork.folder / BLOBS_NAME).mkdir(parents=True, exist_ok=True)
    for sha in shas:
        src = source / BLOBS_NAME / sha
        dst = fork.folder / BLOBS_NAME / sha
        if dst.exists() or not src.exists():
            continue
        try:
            os.link(src, dst)
        except OSError:
            shutil.copyfile(src, dst)
    with open(fork.folder / EVENTS_NAME, "w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    fork.seq = max((int(event.get("seq") or 0) for event in events), default=0)
    fork.turn = max((int(event.get("turn") or 0) for event in events), default=0)
    fork.update_meta(status="forked", turns=fork.turn)
    fork.meta.pop("pid", None)
    fork._write_meta()
    return fork


def _collect_blobs(value: Any, found: set[str]) -> None:
    if _is_blob(value):
        found.add(str(value["$blob"]))
    elif isinstance(value, dict):
        for item in value.values():
            _collect_blobs(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_blobs(item, found)


def prune_sessions(
    sessions_dir: Path,
    *,
    keep_days: int = KEEP_DAYS,
    keep_count: int = KEEP_COUNT,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Remove sessions older than ``keep_days`` and all but the newest ``keep_count``.

    A session whose process still runs is kept. Returns the removed ids.
    """
    rows = list_sessions(sessions_dir)
    moment = now or _now()
    limit = moment - timedelta(days=max(0, keep_days))
    removed: list[str] = []
    for position, row in enumerate(rows):
        if row.status == "running":
            continue
        when = _parse_time(row.updated) or _parse_time(row.created)
        too_old = when is not None and when < limit
        if position < keep_count and not too_old:
            continue
        removed.append(row.id)
        if not dry_run:
            shutil.rmtree(row.folder, ignore_errors=True)
    return removed


def _parse_time(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- show and export


def render_transcript(events: list[dict[str, Any]], folder: Path | None = None, *, full: bool = False) -> str:
    """A readable transcript: tasks, the model's words, tool calls and results, compactions."""

    def text(value: Any) -> str:
        found = resolve(folder, value) if (full and folder is not None) else value
        if _is_blob(found):
            return f"{found.get('head') or ''}... [{found.get('chars')} chars, blob {str(found['$blob'])[:12]}]"
        return str(found or "")

    lines: list[str] = []
    for event in events:
        kind = event.get("type")
        seq = event.get("seq")
        if kind == "session_started":
            lines.append(f"# Session {event.get('id') or ''}  ({event.get('ts')})")
            details = ", ".join(f"{key}: {event[key]}" for key in ("model", "backend", "cwd") if event.get(key))
            if details:
                lines.append(details)
        elif kind == "session_resumed":
            lines.append(f"\n--- resumed at seq {seq} ({event.get('ts')}) ---")
        elif kind == "user_message":
            label = "Task" if event.get("task") else "You"
            lines.append(f"\n## {label} [{seq}]\n{text(event.get('text')).strip()}")
        elif kind == "assistant_turn":
            words = text(event.get("text")).strip()
            usage = event.get("usage") or {}
            tokens = f" ({usage.get('prompt')} in, {usage.get('completion')} out)" if usage.get("prompt") else ""
            lines.append(f"\n### Assistant, turn {event.get('turn')} [{seq}]{tokens}")
            if words:
                lines.append(words)
        elif kind == "tool_started":
            lines.append(f"- {event.get('tool')}: {text(event.get('summary'))}")
        elif kind == "tool_result":
            mark = "ok" if event.get("ok") else "failed"
            lines.append(f"  -> {mark}: {text(event.get('summary'))}")
        elif kind == "permission_decision":
            verdict = "allowed" if event.get("allowed") else "denied"
            lines.append(f"  [permission {verdict}: {text(event.get('summary'))}]")
        elif kind in {"mode_changed", "role_changed"}:
            lines.append(f"\n[{kind.replace('_', ' ')}: {event.get('old')} -> {event.get('new')}]")
        elif kind == "compaction":
            before, after = event.get("tokens_before"), event.get("tokens_after")
            size = f", ~{before} -> ~{after} tokens" if before else ""
            lines.append(f"\n[compaction: {event.get('strategy')}{size}; {event.get('reason') or ''}]")
        elif kind == "task_finished":
            lines.append(f"\n**{event.get('code')}**")
        elif kind == "error":
            lines.append(f"\n[error: {text(event.get('message'))}]")
    return "\n".join(lines).strip() + "\n"


def export_session(folder: Path, *, redacted: bool = False) -> dict[str, Any]:
    """The whole session as one JSON object: meta and events with blobs inlined.

    The log is already redacted on write. ``redacted`` also runs the redaction again (with
    today's custom terms) and replaces the workspace path and the home folder with
    ``<workspace>`` and ``~``, for sharing.
    """
    folder = Path(folder)
    meta = read_meta(folder)
    events = [resolve(folder, event) for event in read_events(folder)]
    payload: dict[str, Any] = {"meta": meta, "events": events}
    if not redacted:
        return payload
    workspace = str(meta.get("cwd") or "")
    home = str(Path.home())

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            text = _redact(value)
            if workspace and len(workspace) > 3:
                text = text.replace(workspace, "<workspace>")
            if home and len(home) > 3:
                text = text.replace(home, "~")
            return text
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    cleaned = clean(payload)
    cleaned["meta"].pop("pid", None)
    cleaned["redacted"] = True
    return cleaned


# --------------------------------------------------------------------------- helpers


def _title(text: str) -> str:
    return " ".join(str(text or "").split())[:100]


def _crit_version() -> str:
    try:
        from critique_bot import __version__

        return str(__version__)
    except Exception:
        return ""


def _private(folder: Path) -> None:
    folder = Path(folder)
    if folder.is_dir():
        return
    try:
        from critique_bot.output import private_dir

        private_dir(folder)
    except Exception:
        folder.mkdir(parents=True, exist_ok=True)


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    temp = path.with_name(path.name + f".{os.getpid()}.{int(time.time() * 1000)}.tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def valid_session_id(text: str) -> bool:
    return bool(_ID_RE.match(text or ""))
