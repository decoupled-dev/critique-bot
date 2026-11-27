# SPDX-License-Identifier: Apache-2.0
"""``crit serve``: the Engine API over HTTP and server-sent events (stdlib only).

One server hosts the sessions of one project. It binds 127.0.0.1 on a random port by
default, writes where it listens to ``~/.crit/server.json`` and a bearer token made for
this start to ``~/.crit/server.token`` (both mode 0600), and answers only requests that
carry that token. A non-loopback address needs ``--listen ADDRESS`` and ``--insecure``:
there is no TLS yet, so the token and the code would cross the network in the clear.

Routes (``docs/api/openapi.yaml`` is the contract; ``tests/test_server.py`` checks the
server against it):

    GET  /v1/health                         GET  /v1/agents
    GET  /v1/tools                          GET  /v1/models
    GET  /v1/sessions                       POST /v1/sessions
    GET  /v1/sessions/{id}                  POST /v1/sessions/{id}/fork
    POST /v1/sessions/{id}/messages         POST /v1/sessions/{id}/cancel
    PUT  /v1/sessions/{id}/mode             POST /v1/sessions/{id}/undo
    GET  /v1/sessions/{id}/events?since=N   (text/event-stream; Last-Event-ID works too)
    POST /v1/sessions/{id}/requests/{rid}   (answer an approval, a question, a plan review)
    POST /v1/agents/{name}/runs             (a file-defined agent, headless)
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from critique_bot import __version__, log, model_api
from critique_bot.config import crit_home
from critique_bot.engine import BadReply, Engine, EngineError, RequestClosed

DEFAULT_HOST = "127.0.0.1"
SERVER_FILE = "server.json"
TOKEN_FILE = "server.token"
#: Largest request body accepted (a task with pasted context fits easily).
MAX_BODY = 1 << 20
#: A comment line on an idle event stream this often, so proxies and clients keep it open.
KEEPALIVE_SECONDS = 15.0
_SESSION_ID = r"(?P<sid>[0-9A-Za-z][0-9A-Za-z_-]{0,63})"


class ServerError(RuntimeError):
    """The server cannot start as asked (an unsafe bind, a port in use)."""


class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def server_file() -> Path:
    return crit_home() / SERVER_FILE


def token_file() -> Path:
    return crit_home() / TOKEN_FILE


def is_loopback(host: str) -> bool:
    """True for localhost and loopback addresses (IPv4 and IPv6)."""
    if host.strip().lower() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return False
        return bool(infos) and all(ipaddress.ip_address(info[4][0]).is_loopback for info in infos)


def check_bind(host: str, *, insecure: bool) -> None:
    """Refuse a non-loopback address unless the person said they know it is not encrypted."""
    if is_loopback(host):
        return
    if not insecure:
        raise ServerError(
            f"{host} is reachable from other machines, and crit serve has no TLS yet: the token and your "
            "code would travel unencrypted. Add --insecure to do it anyway (a trusted network only), or keep "
            "the default 127.0.0.1 and use an SSH tunnel."
        )


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` readable by this user only (0600), replacing it atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def read_server_info() -> dict[str, Any] | None:
    """What ``crit serve`` wrote: url, pid, project, and the token (None when no server ran)."""
    try:
        info = json.loads(server_file().read_text(encoding="utf-8"))
        info["token"] = token_file().read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


#: Connections served at once; more are closed at once (SF-10: a local client cannot pile up threads).
MAX_CONNECTIONS = 64
#: Seconds a connection may sit before it has sent an authorized request (SF-10). Once the token
#: checks out the connection has no timeout (an event stream stays open as long as it is read).
IDLE_TIMEOUT_S = 30.0


class _BoundedServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that serves at most ``MAX_CONNECTIONS`` connections at once."""

    slots: threading.BoundedSemaphore

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            log.warn(f"serve: more than {MAX_CONNECTIONS} connections; one from {client_address} closed")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class CritServer:
    """The HTTP server around an :class:`Engine`."""

    def __init__(self, engine: Engine, *, host: str = DEFAULT_HOST, port: int = 0, insecure: bool = False,
                 token: str | None = None, publish: bool = True) -> None:
        check_bind(host, insecure=insecure)
        self.engine = engine
        self.host = host
        self.token = token or secrets.token_urlsafe(32)
        self.publish = publish
        self.stopping = threading.Event()
        handler = type("Handler", (_Handler,), {"app": self})
        try:
            family = socket.AF_INET6 if ":" in host.strip("[]") else socket.AF_INET
            server_class = type("Server", (_BoundedServer,), {"address_family": family, "daemon_threads": True,
                                                               "slots": threading.BoundedSemaphore(MAX_CONNECTIONS)})
            self.httpd = server_class((host.strip("[]"), port), handler)
        except OSError as exc:
            raise ServerError(f"cannot listen on {host}:{port}: {exc}") from exc
        self.port = int(self.httpd.server_address[1])
        shown = f"[{host.strip('[]')}]" if ":" in host.strip("[]") else host
        self.url = f"http://{shown}:{self.port}"
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        """Serve on a background thread; returns the URL."""
        self._publish()
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="crit-serve", daemon=True)
        self._thread.start()
        return self.url

    def serve_forever(self) -> None:
        self._publish()
        try:
            self.httpd.serve_forever()
        finally:
            self.close()

    def close(self) -> None:
        if self.stopping.is_set():
            return
        self.stopping.set()
        if self._thread is not None:
            self.httpd.shutdown()
        self.httpd.server_close()
        self.engine.close()
        if self.publish:
            self._unpublish()

    def _publish(self) -> None:
        if not self.publish:
            return
        info = {
            "url": self.url,
            "host": self.host,
            "port": self.port,
            "pid": os.getpid(),
            "project": str(self.engine.home.root),
            "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "version": __version__,
            "token_file": str(token_file()),
        }
        _write_private(token_file(), self.token + "\n")
        _write_private(server_file(), json.dumps(info, indent=2) + "\n")

    def _unpublish(self) -> None:
        try:
            info = json.loads(server_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if info.get("pid") == os.getpid() and info.get("port") == self.port:
            server_file().unlink(missing_ok=True)
            token_file().unlink(missing_ok=True)

    # ------------------------------------------------------------------ the routes

    def route(self, method: str, path: str, query: dict[str, list[str]], body: Any, handler: "_Handler") -> Any:
        for pattern, verb, name in _ROUTES:
            match = pattern.fullmatch(path)
            if match and verb == method:
                return getattr(self, name)(body=body, query=query, handler=handler, **match.groupdict())
        if any(pattern.fullmatch(path) for pattern, _, _ in _ROUTES):
            raise HttpError(405, "method_not_allowed", f"{method} is not allowed on {path}")
        raise HttpError(404, "not_found", f"no route {path}")

    def health(self, **_: Any) -> Any:
        return {"ok": True, "version": __version__, "project": str(self.engine.home.root),
                "sessions": len(self.engine.list_live())}

    def list_sessions(self, **_: Any) -> Any:
        return {"sessions": self.engine.list_sessions()}

    def create_session(self, body: Any, **_: Any) -> Any:
        fields = _object(body)
        try:
            handle = self.engine.create_session(
                fields.get("cwd"), agent=_opt_str(fields, "agent"), model=_opt_str(fields, "model"),
                mode=_opt_str(fields, "mode"), title=str(fields.get("title") or fields.get("message") or ""),
                unattended=bool(fields.get("unattended", False)),
            )
            if fields.get("message"):
                handle.send(str(fields["message"]), close_after=bool(fields.get("close_after", False)))
        except EngineError as exc:
            raise HttpError(400, "bad_request", str(exc)) from exc
        return 201, handle.info()

    def get_session(self, sid: str, **_: Any) -> Any:
        try:
            sid = self.engine.find(sid)  # a unique prefix of a logged session works too
        except EngineError as exc:
            raise HttpError(404, "not_found", str(exc)) from exc
        live = self.engine.session(sid)
        if live is not None:
            return live.info()
        for row in self.engine.list_sessions():
            if row["id"] == sid:
                return row
        raise HttpError(404, "not_found", f"no session {sid}")

    def fork_session(self, sid: str, body: Any, **_: Any) -> Any:
        fields = _object(body)
        at = fields.get("at_seq")
        if at is not None and (isinstance(at, bool) or not isinstance(at, int) or at < 1):
            raise HttpError(400, "bad_request", "at_seq must be a positive whole number")
        try:
            handle = self.engine.fork_session(sid, at, mode=_opt_str(fields, "mode"))
        except EngineError as exc:
            raise HttpError(404, "not_found", str(exc)) from exc
        return 201, handle.info()

    def post_message(self, sid: str, body: Any, **_: Any) -> Any:
        fields = _object(body)
        text = str(fields.get("text") or "").strip()
        if not text:
            raise HttpError(400, "bad_request", "text is required")
        handle = self._live_or_open(sid)
        try:
            handle.send(text, close_after=bool(fields.get("close_after", False)))
        except (EngineError, BadReply) as exc:
            raise HttpError(409, "conflict", str(exc)) from exc
        return 202, {"accepted": True, "session": handle.info()}

    def cancel(self, sid: str, **_: Any) -> Any:
        return {"cancelled": self._live(sid).cancel()}

    def set_mode(self, sid: str, body: Any, **_: Any) -> Any:
        fields = _object(body)
        try:
            return {"mode": self._live_or_open(sid).set_mode(str(fields.get("mode") or ""))}
        except BadReply as exc:
            raise HttpError(400, "bad_request", str(exc)) from exc

    def undo(self, sid: str, **_: Any) -> Any:
        try:
            return {"restored": self._live(sid).undo()}
        except BadReply as exc:
            raise HttpError(400, "bad_request", str(exc)) from exc
        except EngineError as exc:
            raise HttpError(409, "conflict", str(exc)) from exc

    def answer(self, sid: str, rid: str, body: Any, **_: Any) -> Any:
        fields = _object(body)
        reply = fields.get("reply", fields)
        try:
            self._live(sid).answer(rid, reply)
        except RequestClosed as exc:
            raise HttpError(409, "request_closed", str(exc)) from exc
        except BadReply as exc:
            raise HttpError(400, "bad_request", str(exc)) from exc
        return {"answered": True}

    def events(self, sid: str, query: dict[str, list[str]], handler: "_Handler", **_: Any) -> Any:
        since = _int_query(query, "since", 0)
        last_id = handler.headers.get("Last-Event-ID")
        if last_id and last_id.isdigit():
            since = max(since, int(last_id))
        follow = (query.get("follow") or ["1"])[0] not in {"0", "false", "no"}
        try:
            sid = self.engine.find(sid)
        except EngineError as exc:
            raise HttpError(404, "not_found", str(exc)) from exc
        handler.stream_events(self.engine.events(sid, since, follow=follow, stop=self.stopping))
        return None

    def list_agents(self, **_: Any) -> Any:
        from critique_bot.extensions import agent_files

        agents, problems = agent_files.load_agents(self.engine.home.root)
        return {
            "agents": [
                {"name": item.name, "description": item.description, "mode": item.mode or "ask",
                 "scope": item.scope, "trusted": item.trusted, "model": item.model or None}
                for item in sorted(agents.values(), key=lambda item: item.name)
            ],
            "problems": problems,
        }

    def run_agent(self, name: str, body: Any, **_: Any) -> Any:
        from critique_bot.extensions import agent_files

        fields = _object(body)
        task = str(fields.get("task") or fields.get("text") or "").strip()
        if not task:
            raise HttpError(400, "bad_request", "task is required")
        found = agent_files.find_agent(name, self.engine.home.root)
        if found is None:
            raise HttpError(404, "not_found", f"no agent {name!r} (GET /v1/agents lists them)")
        try:
            handle = self.engine.create_session(
                agent=found.name, mode=_opt_str(fields, "mode") or agent_mode(found), title=task,
                model=found.model or None, unattended=bool(fields.get("unattended", True)),
                max_rounds=found.max_steps, kind="agent-run",
            )
            handle.send(task, close_after=True)
        except EngineError as exc:
            raise HttpError(400, "bad_request", str(exc)) from exc
        return 202, handle.info()

    def list_tools(self, **_: Any) -> Any:
        from critique_bot import tool_api

        return {"tools": [{"name": spec.name, "description": spec.description, "read_only": spec.read_only,
                           "parameters": spec.parameters} for spec in tool_api.registry().specs()]}

    def list_models(self, **_: Any) -> Any:
        config = self.engine.config
        return {"models": [{"id": config.model or "", "backend": config.backend,
                            "label": model_api.model_label(config), "default": True}]}

    # ------------------------------------------------------------------ helpers

    def _live(self, sid: str) -> Any:
        handle = self.engine.session(sid)
        if handle is None:
            raise HttpError(404, "not_found", f"session {sid} is not open on this server")
        return handle

    def _live_or_open(self, sid: str) -> Any:
        try:
            return self.engine.open_session(sid)
        except EngineError as exc:
            raise HttpError(404, "not_found", str(exc)) from exc


def agent_mode(agent: Any) -> str:
    """The permission mode a file-defined agent runs in headless: its own, but an agent file the
    user has not approved cannot loosen it (Stage 6), and read-only is ask with no one answering."""
    from critique_bot.extensions import agent_files

    mode = str(getattr(agent, "mode", "") or "ask")
    if not getattr(agent, "trusted", True) and mode not in agent_files.SAFE_MODES:
        return "ask"
    return "ask" if mode == "read-only" else mode


#: (path template as in docs/api/openapi.yaml, method, handler). The template's {name} parts
#: become the named groups below.
_ROUTE_TABLE: list[tuple[str, str, str]] = [
    ("/v1/health", "GET", "health"),
    ("/v1/sessions", "GET", "list_sessions"),
    ("/v1/sessions", "POST", "create_session"),
    ("/v1/sessions/{id}", "GET", "get_session"),
    ("/v1/sessions/{id}/fork", "POST", "fork_session"),
    ("/v1/sessions/{id}/messages", "POST", "post_message"),
    ("/v1/sessions/{id}/cancel", "POST", "cancel"),
    ("/v1/sessions/{id}/mode", "PUT", "set_mode"),
    ("/v1/sessions/{id}/undo", "POST", "undo"),
    ("/v1/sessions/{id}/events", "GET", "events"),
    ("/v1/sessions/{id}/requests/{rid}", "POST", "answer"),
    ("/v1/agents", "GET", "list_agents"),
    ("/v1/agents/{name}/runs", "POST", "run_agent"),
    ("/v1/tools", "GET", "list_tools"),
    ("/v1/models", "GET", "list_models"),
]
_PARTS = {"id": _SESSION_ID, "rid": r"(?P<rid>[0-9a-f]{1,64})", "name": r"(?P<name>[a-z0-9][a-z0-9_-]{0,63})"}
_ROUTES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(re.sub(r"\{(\w+)\}", lambda m: _PARTS[m.group(1)], template)), verb, name)
    for template, verb, name in _ROUTE_TABLE
]


def _object(body: Any) -> dict[str, Any]:
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise HttpError(400, "bad_request", "the body must be a JSON object")
    return body


def _opt_str(fields: dict[str, Any], key: str) -> str | None:
    value = fields.get(key)
    return str(value) if isinstance(value, (str, int)) and str(value).strip() else None


def _int_query(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = (query.get(key) or [""])[0]
    if not raw:
        return default
    if not raw.isdigit():
        raise HttpError(400, "bad_request", f"{key} must be a whole number")
    return int(raw)


class _Handler(BaseHTTPRequestHandler):
    app: CritServer
    #: Until a request is authorized: a client that never finishes its headers is dropped.
    timeout = IDLE_TIMEOUT_S
    protocol_version = "HTTP/1.1"
    server_version = f"crit/{__version__}"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
        log.debug("serve: " + (format % args))

    def do_GET(self) -> None:  # noqa: N802 - http.server's names
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            self._authorize()
            parts = urlsplit(self.path)
            body = self._body() if method in {"POST", "PUT"} else None
            result = self.app.route(method, parts.path.rstrip("/") or "/", parse_qs(parts.query), body, self)
            if result is None:
                return  # streamed
            status, payload = result if isinstance(result, tuple) else (200, result)
            self._json(status, payload)
        except HttpError as exc:
            try:
                self._json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            except OSError:
                pass  # the client is gone
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - a request never takes the server down
            log.exception(f"serve: {method} {self.path} failed: {exc}")
            try:
                self._json(500, {"error": {"code": "internal", "message": "the server failed; see crit's log"}})
            except OSError:
                pass

    def _authorize(self) -> None:
        header = self.headers.get("Authorization") or ""
        scheme, _, given = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip().encode(), self.app.token.encode()):
            raise HttpError(401, "unauthorized", "a bearer token is required (crit serve wrote it to "
                                                 f"{token_file()})")
        try:
            self.connection.settimeout(None)  # the token holder's streams may stay open
        except OSError:
            pass

    def _body(self) -> Any:
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            if self.headers.get("Transfer-Encoding"):
                raise HttpError(411, "length_required", "send the body with Content-Length")
            return None
        size = int(length)
        if size > MAX_BODY:
            raise HttpError(413, "too_large", f"the body is over {MAX_BODY} bytes")
        raw = self.rfile.read(size) if size else b""
        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HttpError(400, "bad_request", f"the body is not JSON: {exc}") from exc

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="crit"')
        self.end_headers()
        self.wfile.write(data)

    def stream_events(self, items: Any) -> None:
        """Server-sent events: ``id`` is the event's seq, ``event`` its type, ``data`` the JSON."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b": crit events\n\n")
        self.wfile.flush()
        last_write = time.monotonic()
        stop = threading.Event()
        # A thread that writes keep-alive comments while the stream waits for the next event.
        lock = threading.Lock()

        def keepalive() -> None:
            nonlocal last_write
            while not stop.wait(1.0):
                if time.monotonic() - last_write >= KEEPALIVE_SECONDS:
                    try:
                        with lock:
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
                            last_write = time.monotonic()
                    except OSError:
                        stop.set()

        ticker = threading.Thread(target=keepalive, daemon=True)
        ticker.start()
        try:
            for event in items:
                if stop.is_set():
                    break
                data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                frame = f"id: {event.get('seq')}\nevent: {event.get('type')}\ndata: {data}\n\n".encode("utf-8")
                with lock:
                    self.wfile.write(frame)
                    self.wfile.flush()
                    last_write = time.monotonic()
        except OSError:
            pass  # the client went away
        finally:
            stop.set()
            close = getattr(items, "close", None)
            if callable(close):
                close()
