# SPDX-License-Identifier: Apache-2.0
"""Clients of the Engine API: ``crit attach`` (a terminal on a ``crit serve`` session) and
``crit run`` (one headless task, events as JSON lines on stdout).

:class:`Client` speaks the HTTP API of :mod:`server` with the stdlib (``http.client``);
:func:`attach` drives a front end with :func:`engine.drive_ui` over it, exactly as an
in-process front end drives a :class:`engine.SessionHandle`.
"""

from __future__ import annotations

import http.client
import json
import sys
from collections.abc import Callable, Iterator
from typing import IO, Any
from urllib.parse import urlsplit

from critique_bot.engine import RequestClosed, drive_ui

OK_CODES = frozenset({"COMPLETED", "FINISHED", "DONE"})


class ClientError(RuntimeError):
    def __init__(self, status: int, message: str, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class Client:
    """The HTTP API of one ``crit serve``. ``url`` like http://127.0.0.1:43123; ``token`` from server.token."""

    def __init__(self, url: str, token: str, *, timeout: float = 30.0) -> None:
        parts = urlsplit(url)
        if parts.scheme != "http" or not parts.hostname:
            raise ClientError(0, f"not a crit serve address: {url} (http://HOST:PORT)")
        self.url = url.rstrip("/")
        self.host = parts.hostname
        self.port = parts.port or 80
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_server_file(cls, url: str | None = None) -> "Client":
        """The server ``crit serve`` last started (``~/.crit/server.json``), or ``url`` with its token."""
        from critique_bot.server import read_server_info

        info = read_server_info()
        if info is None or not info.get("token"):
            raise ClientError(0, "no crit serve is running for this user (start one with: crit serve)")
        return cls(url or str(info.get("url") or ""), str(info["token"]))

    def _connection(self, timeout: float | None) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def request(self, method: str, path: str, body: Any = None) -> Any:
        """One JSON request; the decoded reply, or ClientError with the server's message."""
        conn = self._connection(self.timeout)
        try:
            data = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        except OSError as exc:
            raise ClientError(0, f"cannot reach crit serve at {self.url}: {exc}") from exc
        finally:
            conn.close()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            payload = None
        if response.status >= 400:
            error = (payload or {}).get("error") if isinstance(payload, dict) else None
            message = (error or {}).get("message") if isinstance(error, dict) else raw.decode("utf-8", "replace")
            raise ClientError(response.status, str(message), str((error or {}).get("code") or ""))
        return payload

    def events(self, session_id: str, since: int = 0, *, follow: bool = True) -> Iterator[dict[str, Any]]:
        """A session's events from the server-sent event stream, in seq order."""
        conn = self._connection(None)
        path = f"/v1/sessions/{session_id}/events?since={int(since)}" + ("" if follow else "&follow=0")
        try:
            conn.request("GET", path, headers={"Authorization": f"Bearer {self.token}",
                                               "Accept": "text/event-stream"})
            response = conn.getresponse()
            if response.status >= 400:
                raw = response.read().decode("utf-8", "replace")
                raise ClientError(response.status, raw)
            data: list[str] = []
            while True:
                line = response.readline()
                if not line:
                    return
                text = line.decode("utf-8").rstrip("\r\n")
                if not text:
                    if data:
                        yield json.loads("\n".join(data))
                        data = []
                    continue
                if text.startswith(":"):
                    continue
                field, _, value = text.partition(":")
                if field == "data":
                    data.append(value[1:] if value.startswith(" ") else value)
        except OSError as exc:
            raise ClientError(0, f"the event stream from {self.url} broke: {exc}") from exc
        finally:
            conn.close()

    # the routes, by name
    def health(self) -> Any:
        return self.request("GET", "/v1/health")

    def sessions(self) -> list[dict[str, Any]]:
        return list((self.request("GET", "/v1/sessions") or {}).get("sessions") or [])

    def session(self, session_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/sessions/{session_id}")

    def create(self, **fields: Any) -> dict[str, Any]:
        return self.request("POST", "/v1/sessions", {k: v for k, v in fields.items() if v is not None})

    def send(self, session_id: str, text: str, *, close_after: bool = False) -> Any:
        return self.request("POST", f"/v1/sessions/{session_id}/messages", {"text": text, "close_after": close_after})

    def answer(self, session_id: str, request_id: str, reply: Any) -> Any:
        try:
            return self.request("POST", f"/v1/sessions/{session_id}/requests/{request_id}", {"reply": reply})
        except ClientError as exc:
            if exc.status == 409:
                raise RequestClosed(str(exc)) from exc
            raise

    def cancel(self, session_id: str) -> Any:
        return self.request("POST", f"/v1/sessions/{session_id}/cancel", {})

    def run_agent(self, name: str, task: str, **fields: Any) -> dict[str, Any]:
        return self.request("POST", f"/v1/agents/{name}/runs", {"task": task, **fields})


def attach(client: Client, session_id: str | None, front: Any, *, task: str | None = None) -> int:
    """A terminal on a server's session: its new events, its open requests, and the next tasks.

    No ``session_id``: the newest live session, else a new one. A session the server does not
    have open (it was restarted after a crash) is resumed by the first message sent to it.
    """
    if session_id is None:
        live = [row for row in client.sessions() if row.get("live")]
        session_id = str(live[0]["id"]) if live else str(client.create()["id"])
    info = client.session(session_id)
    session_id = str(info.get("id") or session_id)
    last = int(info.get("last_seq") or 0)
    open_rows = [item for item in info.get("pending") or [] if isinstance(item, dict)]
    pending = {str(item.get("id")) for item in open_rows}
    front.note("note", f"Attached to session {session_id} on {client.url}.")
    if task:
        client.send(session_id, task)
    elif info.get("state") in {None, "idle", "closed"} and not pending:
        text = front.read_message()
        if text is None:
            return 0
        client.send(session_id, text)
    since = last
    if open_rows:
        # Start early enough to see the open requests again; drive_ui answers them.
        since = min(int(item.get("seq") or 1) for item in open_rows) - 1
    code = drive_ui(
        client.events(session_id, since),
        front,
        answer=lambda rid, reply: client.answer(session_id, rid, reply),
        send=lambda text: client.send(session_id, text),
        replay_until=last,
        open_requests=pending,
    )
    return 0 if not code or code in OK_CODES else 1


def run_headless(
    events: Iterator[dict[str, Any]],
    *,
    json_lines: bool,
    out: IO[str] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    """Print a one-task session's events (JSON lines, or the words and the outcome). The exit code.

    With ``json_lines`` the last line is the final object: ``{"type": "final", "code": ...,
    "changed": [...], "evidence": {...}}``; ``evidence`` is the verified flow's checklist and
    numbers (Stage 7), null for a plain task.
    """
    out = out or sys.stdout
    code = ""
    final: dict[str, Any] = {}
    for event in events:
        if on_event is not None:
            on_event(event)
        kind = event.get("type")
        if json_lines:
            out.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "assistant_text":
            out.write(str(event.get("text") or "") + "\n")
            out.flush()
        if kind == "task_finished":
            code = str(event.get("code") or "")
            final = {"type": "final", "code": code, "changed": event.get("changed") or [],
                     "evidence": event.get("evidence")}
            if not json_lines:
                evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else None
                for line in (evidence or {}).get("checklist") or []:
                    print(f"  {line}", file=sys.stderr)
                print(code, file=sys.stderr)
        elif kind == "session_closed":
            break
    if json_lines:
        out.write(json.dumps(final or {"type": "final", "code": code, "changed": [], "evidence": None},
                             ensure_ascii=False, separators=(",", ":")) + "\n")
        out.flush()
    return 0 if code in OK_CODES else 1
