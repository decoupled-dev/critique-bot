"""A small MCP client: JSON-RPC 2.0 over stdio or streamable HTTP, no extra packages.

What crit needs from an MCP server is ``initialize``, ``tools/list`` and ``tools/call``.
The servers come from ``mcp.json`` in the same shape as Claude Code's (so a user can copy
theirs), from ``~/.crit`` or (once approved with ``crit trust``) from the project:

```json
{"mcpServers": {
  "git":  {"command": "uvx", "args": ["mcp-server-git", "--repository", "."]},
  "jira": {"type": "http", "url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer ..."}}
}}
```

crit-only keys per server: ``disabled`` (true skips it), ``timeout_s`` (per call, default
60), ``read_only_tools`` (tool names that run without asking; the server's own
``readOnlyHint`` is a claim, not trusted).

Safety:

- **Pinning.** The first time a tool is seen, a sha256 of its name, description and input
  schema goes into ``~/.crit/mcp-lock.json``. When the server later describes the tool
  differently (a "rug pull" that rewrites the description into instructions), the tool is
  left out of the session until ``crit mcp approve <server>``, which shows the diff.
- **Namespacing.** Tools appear as ``mcp__<server>__<tool>``.
- **Untrusted output.** Results come back inside ``<untrusted source="mcp:server/tool">``
  and carry a taint flag (see untrusted.py).
"""

from __future__ import annotations

import difflib
import hashlib
import http.client
import itertools
import json
import os
import queue
import re
import subprocess  # nosec B404 - starting the MCP servers the user configured; argv only
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from critique_bot import agent_shell
from critique_bot.extensions import runner, schema, untrusted

__all__ = [
    "McpError",
    "McpServer",
    "McpClient",
    "connect",
    "load_config",
    "lock_path",
    "pin_tools",
    "approve_tools",
    "tool_name",
]

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT_S = 60.0
CONNECT_TIMEOUT_S = 20.0
MAX_MESSAGE_BYTES = 8_000_000
_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


class McpError(RuntimeError):
    """A server could not be reached, or answered with an error."""


@dataclass
class McpServer:
    name: str
    #: "stdio" or "http".
    transport: str
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout_s: float = DEFAULT_TIMEOUT_S
    read_only_tools: tuple[str, ...] = ()
    scope: str = "user"
    source: str = ""

    def describe(self) -> str:
        if self.transport == "http":
            return f"{self.name}: HTTP {self.url.split('?', 1)[0]}"
        return f"{self.name}: runs {' '.join(self.command)[:200]}"


def safe_name(name: str) -> str:
    return _NAME_RE.sub("_", str(name))[:40] or "server"


def tool_name(server: str, tool: str) -> str:
    """``mcp__<server>__<tool>``, the name the model sees."""
    return f"mcp__{safe_name(server)}__{_NAME_RE.sub('_', str(tool))}"


# --------------------------------------------------------------------------- config

def _servers_from(data: Any, *, scope: str, source: str) -> tuple[list[McpServer], list[str]]:
    problems: list[str] = []
    servers: list[McpServer] = []
    entries = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return [], [f"{source}: expected {{\"mcpServers\": {{...}}}}"]
    for raw_name, item in entries.items():
        if not isinstance(item, dict):
            problems.append(f"{source}: server {raw_name} must be an object")
            continue
        if item.get("disabled") is True:
            continue
        server = server_from_entry(str(raw_name), item, scope=scope, source=source)
        if isinstance(server, str):
            problems.append(server)
        else:
            servers.append(server)
    return servers, problems


def server_from_entry(name: str, item: dict[str, Any], *, scope: str = "user", source: str = "") -> McpServer | str:
    """One ``mcpServers`` entry as a server, or a problem sentence."""
    kind = str(item.get("type") or ("http" if item.get("url") else "stdio")).lower()
    timeout = item.get("timeout_s", DEFAULT_TIMEOUT_S)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        timeout = DEFAULT_TIMEOUT_S
    read_only = tuple(str(x) for x in item.get("read_only_tools") or [] if isinstance(x, str))
    env = item.get("env") or {}
    if isinstance(env, list):  # ACP shape: [{"name": ..., "value": ...}]
        env = {str(e.get("name")): str(e.get("value", "")) for e in env if isinstance(e, dict) and e.get("name")}
    headers = item.get("headers") or {}
    if isinstance(headers, list):
        headers = {str(h.get("name")): str(h.get("value", "")) for h in headers if isinstance(h, dict) and h.get("name")}
    if not isinstance(env, dict) or not isinstance(headers, dict):
        return f"{source}: server {name}: env and headers must be objects"
    if kind in {"http", "streamable-http", "streamable_http", "sse"}:
        url = str(item.get("url") or "")
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return f"{source}: server {name}: url must be an http(s) address"
        return McpServer(name, "http", url=url, headers={str(k): str(v) for k, v in headers.items()},
                         timeout_s=float(timeout), read_only_tools=read_only, scope=scope, source=source)
    command = item.get("command")
    args = item.get("args") or []
    if not isinstance(command, str) or not command.strip():
        return f"{source}: server {name} needs a command (or a url)"
    if not isinstance(args, list) or not all(isinstance(a, (str, int, float)) for a in args):
        return f"{source}: server {name}: args must be a list of strings"
    return McpServer(name, "stdio", command=[command, *[str(a) for a in args]],
                     env={str(k): str(v) for k, v in env.items()}, cwd=str(item.get("cwd") or ""),
                     timeout_s=float(timeout), read_only_tools=read_only, scope=scope, source=source)


def load_config(path: Path, *, scope: str = "user") -> tuple[list[McpServer], list[str]]:
    """The servers in one ``mcp.json`` and the problems found (one sentence each)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"{path}: {exc}"]
    return _servers_from(data, scope=scope, source=str(path))


# --------------------------------------------------------------------------- transports

class _StdioTransport:
    def __init__(self, server: McpServer, workspace: Path) -> None:
        cwd = Path(server.cwd) if server.cwd else workspace
        if not cwd.is_absolute():
            cwd = workspace / cwd
        try:
            self.proc = subprocess.Popen(  # nosec B603 - argv list, shell=False; a server the user configured
                server.command,
                cwd=str(cwd),
                env=runner.argv_env(server.env),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name != "nt",
            )
        except (OSError, ValueError) as exc:
            raise McpError(f"could not start {server.command[0]!r}: {exc}") from exc
        self.name = server.name
        self._lock = threading.Lock()
        self._waiting: dict[Any, queue.Queue] = {}
        self._stderr: list[str] = []
        self.closed = False
        threading.Thread(target=self._read, daemon=True, name=f"mcp-{server.name}").start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _read(self) -> None:
        stream = self.proc.stdout
        assert stream is not None
        try:
            for line in iter(lambda: stream.readline(MAX_MESSAGE_BYTES), b""):
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._dispatch(message)
        except (OSError, ValueError):
            pass
        self.closed = True
        with self._lock:
            for waiter in self._waiting.values():
                waiter.put({"error": {"code": -32000, "message": "the server closed its output"}})

    def _drain_stderr(self) -> None:
        stream = self.proc.stderr
        if stream is None:
            return
        try:
            for line in iter(lambda: stream.readline(10_000), b""):
                self._stderr.append(line.decode("utf-8", "replace").rstrip())
                del self._stderr[:-20]
        except (OSError, ValueError):
            pass

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        if "method" in message and "id" in message:
            # A request from the server: answer ping; refuse what crit does not offer.
            if message["method"] == "ping":
                self._write({"jsonrpc": "2.0", "id": message["id"], "result": {}})
            else:
                self._write({"jsonrpc": "2.0", "id": message["id"],
                             "error": {"code": -32601, "message": "not supported by this client"}})
            return
        if "id" in message:
            with self._lock:
                waiter = self._waiting.pop(message["id"], None)
            if waiter is not None:
                waiter.put(message)

    def _write(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpError(f"{self.name}: could not write to the server: {exc}") from exc

    def request(self, message: dict[str, Any], timeout: float) -> dict[str, Any]:
        waiter: queue.Queue = queue.Queue()
        with self._lock:
            self._waiting[message["id"]] = waiter
        if self.closed:
            raise McpError(f"{self.name}: the server is not running{self._stderr_tail()}")
        self._write(message)
        try:
            return waiter.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._waiting.pop(message["id"], None)
            raise McpError(f"{self.name}: no answer to {message.get('method')} in {timeout:g}s") from None

    def notify(self, message: dict[str, Any]) -> None:
        self._write(message)

    def _stderr_tail(self) -> str:
        tail = [line for line in self._stderr if line.strip()][-3:]
        return (": " + " | ".join(tail))[:400] if tail else ""

    def close(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            agent_shell.kill_tree(self.proc)
            try:
                self.proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
        for pipe in (self.proc.stdout, self.proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass


class _HttpTransport:
    """Streamable HTTP: each message is a POST; the answer is JSON or an SSE stream."""

    def __init__(self, server: McpServer) -> None:
        parts = urlsplit(server.url)
        self.name = server.name
        self.https = parts.scheme == "https"
        self.host = parts.hostname or ""
        self.port = parts.port
        self.path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        self.headers = dict(server.headers)
        self.session_id = ""
        self.protocol = ""

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        # http.client never reads proxy variables: the request goes where mcp.json says.
        if self.https:
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout)
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _post(self, message: dict[str, Any], timeout: float) -> tuple[int, dict[str, str], bytes]:
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        headers = {
            **self.headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        conn = self._connection(timeout)
        try:
            conn.request("POST", self.path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read(MAX_MESSAGE_BYTES + 1)
            got = {key.lower(): value for key, value in response.getheaders()}
            return response.status, got, data
        except (OSError, http.client.HTTPException) as exc:
            raise McpError(f"{self.name}: {exc}") from exc
        finally:
            conn.close()

    def request(self, message: dict[str, Any], timeout: float) -> dict[str, Any]:
        status, headers, data = self._post(message, timeout)
        if headers.get("mcp-session-id"):
            self.session_id = headers["mcp-session-id"]
        if status >= 400:
            raise McpError(f"{self.name}: HTTP {status}: {data[:200].decode('utf-8', 'replace')}")
        if len(data) > MAX_MESSAGE_BYTES:
            raise McpError(f"{self.name}: the answer is larger than {MAX_MESSAGE_BYTES} bytes")
        text = data.decode("utf-8", "replace")
        if "text/event-stream" in headers.get("content-type", ""):
            for event in _sse_messages(text):
                if isinstance(event, dict) and event.get("id") == message["id"]:
                    return event
            raise McpError(f"{self.name}: the event stream had no answer to {message.get('method')}")
        try:
            answer = json.loads(text)
        except json.JSONDecodeError as exc:
            raise McpError(f"{self.name}: not JSON: {exc}") from exc
        if isinstance(answer, list):  # a batch: pick ours
            answer = next((item for item in answer if isinstance(item, dict) and item.get("id") == message["id"]), {})
        return answer if isinstance(answer, dict) else {}

    def notify(self, message: dict[str, Any]) -> None:
        status, headers, _ = self._post(message, CONNECT_TIMEOUT_S)
        if headers.get("mcp-session-id"):
            self.session_id = headers["mcp-session-id"]

    def close(self) -> None:
        if not self.session_id:
            return
        conn = self._connection(5.0)
        try:
            conn.request("DELETE", self.path, headers={**self.headers, "Mcp-Session-Id": self.session_id})
            conn.getresponse().read(1000)
        except (OSError, http.client.HTTPException):
            pass
        finally:
            conn.close()


def _sse_messages(text: str) -> list[Any]:
    out: list[Any] = []
    for block in re.split(r"\r?\n\r?\n", text):
        data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
        if data:
            try:
                out.append(json.loads(data))
            except json.JSONDecodeError:
                continue
    return out


# --------------------------------------------------------------------------- client

class McpClient:
    def __init__(self, server: McpServer, transport: Any) -> None:
        self.server = server
        self.transport = transport
        self._ids = itertools.count(1)
        self.info: dict[str, Any] = {}

    def _call(self, method: str, params: dict[str, Any] | None, timeout: float) -> Any:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            message["params"] = params
        answer = self.transport.request(message, timeout)
        if "error" in answer:
            error = answer.get("error") or {}
            raise McpError(f"{self.server.name}: {method}: {error.get('message') or error}")
        return answer.get("result")

    def initialize(self) -> None:
        from critique_bot import __version__

        result = self._call(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
             "clientInfo": {"name": "crit", "version": __version__}},
            CONNECT_TIMEOUT_S,
        )
        self.info = result if isinstance(result, dict) else {}
        if isinstance(self.transport, _HttpTransport):
            self.transport.protocol = str(self.info.get("protocolVersion") or PROTOCOL_VERSION)
        self.transport.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(50):
            result = self._call("tools/list", {"cursor": cursor} if cursor else {}, CONNECT_TIMEOUT_S) or {}
            for item in result.get("tools") or []:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    tools.append(item)
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._call("tools/call", {"name": name, "arguments": arguments}, self.server.timeout_s)
        return result if isinstance(result, dict) else {}

    def close(self) -> None:
        try:
            self.transport.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass


def connect(server: McpServer, workspace: Path) -> McpClient:
    """Start (or reach) ``server`` and run the MCP handshake."""
    transport = _HttpTransport(server) if server.transport == "http" else _StdioTransport(server, Path(workspace))
    client = McpClient(server, transport)
    try:
        client.initialize()
    except BaseException:
        client.close()
        raise
    return client


def result_text(result: dict[str, Any]) -> str:
    """The text of a ``tools/call`` result's content blocks."""
    parts: list[str] = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind == "resource":
            resource = block.get("resource") or {}
            if isinstance(resource, dict) and "text" in resource:
                parts.append(str(resource.get("text") or ""))
            else:
                parts.append(f"[resource {resource.get('uri', '')} omitted]")
        elif kind == "resource_link":
            parts.append(f"[resource link {block.get('uri', '')}]")
        else:
            parts.append(f"[{kind} content omitted]")
    if not parts and isinstance(result.get("structuredContent"), (dict, list)):
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False, indent=1))
    return "\n".join(parts)


# --------------------------------------------------------------------------- pinning

def lock_path() -> Path:
    from critique_bot.config import crit_home

    return crit_home() / "mcp-lock.json"


def tool_digest(tool: dict[str, Any]) -> str:
    material = json.dumps(
        {"name": tool.get("name"), "description": tool.get("description") or "",
         "inputSchema": tool.get("inputSchema") or {}},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _load_lock() -> dict[str, Any]:
    try:
        data = json.loads(lock_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"servers": {}}
    if not isinstance(data, dict) or not isinstance(data.get("servers"), dict):
        return {"servers": {}}
    return data


def _save_lock(data: dict[str, Any]) -> None:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mcp-lock-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class PinResult:
    allowed: list[dict[str, Any]]
    #: (tool name, the pinned definition, the server's definition now) for each blocked tool.
    changed: list[tuple[str, dict[str, Any], dict[str, Any]]]
    new: list[str]


def _pinned_entry(tool: dict[str, Any]) -> dict[str, Any]:
    return {"sha256": tool_digest(tool), "description": tool.get("description") or "",
            "inputSchema": tool.get("inputSchema") or {}}


def pin_tools(server: str, tools: list[dict[str, Any]]) -> PinResult:
    """Pin tools seen for the first time; block those whose definition changed since."""
    data = _load_lock()
    pinned = data["servers"].setdefault(server, {})
    allowed, changed, new = [], [], []
    for tool in tools:
        name = tool["name"]
        entry = pinned.get(name)
        if not isinstance(entry, dict):
            pinned[name] = _pinned_entry(tool)
            new.append(name)
            allowed.append(tool)
        elif entry.get("sha256") == tool_digest(tool):
            allowed.append(tool)
        else:
            changed.append((name, {"name": name, "description": entry.get("description", ""),
                                   "inputSchema": entry.get("inputSchema", {})}, tool))
    if new:
        _save_lock(data)
    return PinResult(allowed, changed, new)


def approve_tools(server: str, tools: list[dict[str, Any]], names: list[str] | None = None) -> list[str]:
    """Pin the server's current definitions (all, or ``names``); returns the names pinned."""
    data = _load_lock()
    pinned = data["servers"].setdefault(server, {})
    done = []
    for tool in tools:
        if names and tool["name"] not in names:
            continue
        pinned[tool["name"]] = _pinned_entry(tool)
        done.append(tool["name"])
    _save_lock(data)
    return done


def definition_diff(old: dict[str, Any], new: dict[str, Any]) -> str:
    def lines(tool: dict[str, Any]) -> list[str]:
        shown = {"description": tool.get("description") or "", "inputSchema": tool.get("inputSchema") or {}}
        return json.dumps(shown, indent=2, sort_keys=True, ensure_ascii=False).splitlines()

    return "\n".join(difflib.unified_diff(lines(old), lines(new), "pinned", "server now", lineterm=""))


# --------------------------------------------------------------------------- tools for the registry

@dataclass
class McpTool:
    server: McpServer
    client: McpClient
    definition: dict[str, Any]

    @property
    def name(self) -> str:
        return tool_name(self.server.name, self.definition["name"])

    @property
    def remote_name(self) -> str:
        return str(self.definition["name"])

    @property
    def description(self) -> str:
        return str(self.definition.get("description") or "").strip()

    @property
    def schema(self) -> dict[str, Any]:
        found = self.definition.get("inputSchema")
        return found if isinstance(found, dict) else {"type": "object", "properties": {}}

    @property
    def read_only(self) -> bool:
        return self.remote_name in self.server.read_only_tools

    def one_line(self) -> str:
        first = self.description.split("\n", 1)[0]
        return first[:160]

    def permission(self, arguments: Any, ctx: Any) -> Any:
        from critique_bot.tool_api import Permission

        shown = json.dumps(arguments if isinstance(arguments, dict) else {}, ensure_ascii=False, indent=1)
        label = f"MCP {self.server.name}/{self.remote_name}"
        if self.read_only:
            return Permission("read", label, "", shown, tool=self.name)
        return Permission("command", label + " (it may change things or reach the network)",
                          f"mcp:{self.server.name}/{self.remote_name}", shown, tool=self.name)

    def execute(self, arguments: Any, ctx: Any) -> dict[str, Any]:
        source = f"mcp:{self.server.name}/{self.remote_name}"
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                arguments = None
        if not isinstance(arguments, dict):
            return {"tool": self.name, "ok": False, "error": "arguments must be a JSON object"}
        problems = schema.validate(self.schema, arguments) if self.schema.get("type") in (None, "object") else []
        if problems:
            return {"tool": self.name, "ok": False, "error": "bad arguments: " + "; ".join(problems[:6]),
                    "parameters": self.schema}
        started = time.monotonic()
        try:
            result = self.client.call_tool(self.remote_name, arguments)
        except McpError as exc:
            return {"tool": self.name, "ok": False, "error": str(exc)}
        from critique_bot.redaction import redact_secrets

        text, _ = redact_secrets(result_text(result))
        cap = int(getattr(ctx, "max_chars", 0) or 0) or 16_000
        if len(text) > cap:
            text = text[:cap] + f"\n... {len(text) - cap} characters cut ..."
        ok = not result.get("isError")
        out: dict[str, Any] = {
            "tool": self.name,
            "ok": ok,
            "output": text,
            "ui": {"summary": f"{self.server.name}/{self.remote_name} ({time.monotonic() - started:.1f}s)",
                   "diff": None, "lines": None},
        }
        if not ok:
            out["error"] = "the MCP tool reported an error"
        return untrusted.mark(out, source)
