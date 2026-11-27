# SPDX-License-Identifier: Apache-2.0
"""The egress proxy: the only way sandboxed commands reach the network.

A small HTTP proxy (``CONNECT`` tunnels and plain ``http://`` requests) on 127.0.0.1, run by
crit in a background thread with asyncio. It listens on a TCP port (macOS, and the address
in ``HTTP(S)_PROXY``) and on a unix socket that the Linux sandbox's bridge forwards to.

Every request is decided by host:

* crit's egress policy (:mod:`critique_bot.egress`): ``strict`` allows the chat host and
  ``allowed_hosts`` only, ``open`` allows any host;
* a tainted session in auto mode (:mod:`critique_bot.taint`) reaches the model server only;
* the host's loopback is reachable only on the forwarded ``ports`` (the adb server), and
  link-local addresses (cloud metadata) never;
* hosts the user allowed for this session (an escape) are added;
* ``CONNECT`` only to ports 443 and 80 (and the forwarded loopback ports).

and logged to the audit log (host, port, method, bytes each way, allowed or denied). A
denied request gets ``403`` with the reason, and its host is remembered so crit can ask
the user once (``denied_since``).

Tools layer. Standard library only.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from critique_bot import audit, egress, log

CONNECT_PORTS = frozenset({443, 80})
HEAD_LIMIT = 64 * 1024
IDLE_TIMEOUT = 600.0
FORWARDED = "forwarded loopback port"


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


@dataclass
class Denied:
    host: str
    port: int
    reason: str
    at: float = field(default_factory=time.monotonic)


def _is_ip(host: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


class EgressProxy:
    """One proxy for this crit process. ``start()`` once; ``decide`` is the policy."""

    def __init__(
        self,
        *,
        ports: tuple[int, ...] = (),
        allow_hosts: bool = True,
        restricted: Callable[[], bool] | None = None,
        model_host: Callable[[], str] | None = None,
        connect_ports: frozenset[int] = CONNECT_PORTS,
    ) -> None:
        #: Host loopback ports a sandboxed command may reach.
        self.ports = set(ports)
        #: False: no host at all (offline / strict egress); only the forwarded loopback ports.
        self.allow_hosts = allow_hosts
        self.restricted = restricted or _taint_restricted
        self.model_host = model_host or (lambda: egress.policy().chat_host)
        self.connect_ports = connect_ports
        #: Hosts the user allowed for this session.
        self.extra_hosts: set[str] = set()
        self.denied: list[Denied] = []
        self.port = 0
        self.socket_path = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._folder = ""
        self._servers: list[asyncio.AbstractServer] = []

    # -- policy

    def allow_host(self, host: str) -> None:
        self.extra_hosts.add(host.lower().rstrip("."))

    def decide(self, host: str, port: int, *, method: str = "CONNECT") -> Decision:
        host = (host or "").lower().rstrip(".").strip("[]")
        if not host:
            return Decision(False, "no host")
        address = _is_ip(host)
        loopback = host == "localhost" or (address is not None and address.is_loopback)
        if loopback:
            if port in self.ports:
                return Decision(True, FORWARDED)
            return Decision(False, f"the host's loopback port {port} is not forwarded into the sandbox")
        if address is not None and (address.is_link_local or address.is_multicast or address.is_unspecified):
            return Decision(False, "link-local and multicast addresses are never reachable")
        if port not in self.connect_ports:
            return Decision(False, f"port {port} (only {', '.join(map(str, sorted(self.connect_ports)))} go through the proxy)")
        if self.restricted():
            model = (self.model_host() or "").lower()
            if model and host == model:
                return Decision(True, "model server (session tainted)")
            return Decision(False, "the session is tainted (untrusted content was read) and in auto mode: "
                                   "only the model server is reachable until /trust")
        if host in self.extra_hosts or any(host.endswith("." + item) for item in self.extra_hosts):
            return Decision(True, "allowed for this session")
        if not self.allow_hosts:
            return Decision(False, 'commands run offline (egress "strict" or "offline": true)')
        if egress.policy().permits(host):
            return Decision(True, "egress policy")
        return Decision(False, f'egress is "strict": {host} is not the url host or in allowed_hosts')

    def denied_since(self, mark: float) -> list[Denied]:
        """Hosts denied after ``mark`` (``time.monotonic()``), each once."""
        seen: dict[str, Denied] = {}
        for item in self.denied:
            if item.at >= mark and item.host not in seen:
                seen[item.host] = item
        return list(seen.values())

    # -- lifecycle

    def start(self, *, unix: bool = True, timeout: float = 5.0) -> "EgressProxy":
        if self._thread is not None:
            return self
        if unix and hasattr(asyncio, "start_unix_server") and os.name != "nt":
            self._folder = tempfile.mkdtemp(prefix="crit-proxy-")
            self.socket_path = os.path.join(self._folder, "proxy.sock")
        self._thread = threading.Thread(target=self._run, name="crit-egress-proxy", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        if self._error is not None:
            raise RuntimeError(f"egress proxy could not start: {self._error}")
        if not self.port:
            raise RuntimeError("egress proxy did not start in time")
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            server = loop.run_until_complete(asyncio.start_server(self._client, "127.0.0.1", 0))
            self._servers.append(server)
            self.port = server.sockets[0].getsockname()[1]
            if self.socket_path:
                unix_server = loop.run_until_complete(asyncio.start_unix_server(self._client, path=self.socket_path))
                self._servers.append(unix_server)
                os.chmod(self.socket_path, 0o600)
        except BaseException as exc:  # noqa: BLE001 - reported by start()
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            for server in self._servers:
                server.close()
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def stop(self) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(2)
        self._thread = None
        if self.socket_path:
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
        if self._folder:
            try:
                os.rmdir(self._folder)
            except OSError:
                pass

    # -- one client

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._handle(reader, writer)
        except Exception as exc:  # noqa: BLE001 - one bad client never stops the proxy
            log.debug(f"egress proxy: {type(exc).__name__}: {exc}")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            return
        if len(head) > HEAD_LIMIT:
            await self._reply(writer, 431, "request head too large")
            return
        text = head.decode("latin-1")
        request_line, _, rest = text.partition("\r\n")
        parts = request_line.split(" ")
        if len(parts) != 3:
            await self._reply(writer, 400, "bad request line")
            return
        method, target, version = parts
        if method.upper() == "CONNECT":
            host, port = _split_host_port(target, 443)
            await self._tunnel(reader, writer, host, port)
            return
        if not target.lower().startswith("http://"):
            await self._reply(writer, 400, "only CONNECT and absolute http:// requests go through this proxy")
            return
        await self._forward(reader, writer, method.upper(), target, version, rest)

    async def _reply(self, writer: asyncio.StreamWriter, status: int, reason: str) -> None:
        body = f"crit egress proxy: {reason}\n".encode("utf-8", "replace")
        words = {400: "Bad Request", 403: "Forbidden", 431: "Request Header Fields Too Large", 502: "Bad Gateway"}
        writer.write(
            f"HTTP/1.1 {status} {words.get(status, 'Error')}\r\nContent-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\nX-Crit-Egress: denied\r\n\r\n".encode("latin-1") + body
        )
        try:
            await writer.drain()
        except ConnectionError:
            pass

    def _check(self, host: str, port: int, method: str) -> Decision:
        decision = self.decide(host, port, method=method)
        if not decision.allowed:
            self.denied.append(Denied(host, port, decision.reason))
            del self.denied[:-200]
            audit.record("egress", host=host, port=port, method=method, allowed=False, reason=decision.reason,
                         sent=0, received=0)
        return decision

    async def _tunnel(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str, port: int) -> None:
        decision = self._check(host, port, "CONNECT")
        if not decision.allowed:
            await self._reply(writer, 403, f"{host}:{port} denied: {decision.reason}")
            return
        try:
            up_reader, up_writer = await _open(host, port, decision.reason == FORWARDED)
        except (OSError, asyncio.TimeoutError) as exc:
            audit.record("egress", host=host, port=port, method="CONNECT", allowed=True, error=str(exc), sent=0, received=0)
            await self._reply(writer, 502, f"could not connect to {host}:{port}: {exc}")
            return
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        sent, received = await _splice(reader, writer, up_reader, up_writer)
        audit.record("egress", host=host, port=port, method="CONNECT", allowed=True, reason=decision.reason,
                     sent=sent, received=received)

    async def _forward(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, method: str, target: str, version: str, rest: str,
    ) -> None:
        from urllib.parse import urlsplit

        parts = urlsplit(target)
        host = (parts.hostname or "").lower()
        port = parts.port or 80
        decision = self._check(host, port, method)
        if not decision.allowed:
            await self._reply(writer, 403, f"{host} denied: {decision.reason}")
            return
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        headers = [line for line in rest.split("\r\n") if line and not line.lower().startswith(("proxy-", "connection:", "keep-alive:"))]
        headers.append("Connection: close")
        head = f"{method} {path} {version}\r\n" + "\r\n".join(headers) + "\r\n\r\n"
        try:
            up_reader, up_writer = await _open(host, port, decision.reason == FORWARDED)
        except (OSError, asyncio.TimeoutError) as exc:
            audit.record("egress", host=host, port=port, method=method, allowed=True, error=str(exc), sent=0, received=0)
            await self._reply(writer, 502, f"could not connect to {host}:{port}: {exc}")
            return
        up_writer.write(head.encode("latin-1"))
        await up_writer.drain()
        sent, received = await _splice(reader, writer, up_reader, up_writer)
        audit.record("egress", host=host, port=port, method=method, allowed=True, reason=decision.reason,
                     sent=sent + len(head), received=received)


def _split_host_port(target: str, default: int) -> tuple[str, int]:
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, _, port = target.rpartition(":") if target.count(":") == 1 else (target, "", "")
    try:
        number = int(port) if port else default
    except ValueError:
        number = default
    return host.lower(), number


async def _open(host: str, port: int, forwarded: bool) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to ``host``; a name that resolves to loopback or link-local is refused (DNS rebinding)."""
    if forwarded:
        return await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 30)
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=1), 30)  # SOCK_STREAM
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
            raise OSError(f"{host} resolves to {address}, which the sandbox may not reach")
    first = infos[0][4][0]
    return await asyncio.wait_for(asyncio.open_connection(first, port), 30)


async def _splice(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    up_reader: asyncio.StreamReader, up_writer: asyncio.StreamWriter,
) -> tuple[int, int]:
    """Copy both ways until either side closes; bytes sent up and received back."""
    counts = [0, 0]

    async def copy(source: asyncio.StreamReader, target: asyncio.StreamWriter, index: int) -> None:
        try:
            while True:
                data = await asyncio.wait_for(source.read(65536), IDLE_TIMEOUT)
                if not data:
                    break
                counts[index] += len(data)
                target.write(data)
                await target.drain()
        except (ConnectionError, asyncio.TimeoutError, OSError):
            pass
        finally:
            try:
                if target.can_write_eof():
                    target.write_eof()
            except (OSError, RuntimeError):
                pass

    await asyncio.gather(copy(reader, up_writer, 0), copy(up_reader, writer, 1))
    try:
        up_writer.close()
    except Exception:
        pass
    return counts[0], counts[1]


def _taint_restricted() -> bool:
    from critique_bot import taint

    return taint.restricted()


_shared: EgressProxy | None = None
_shared_lock = threading.Lock()


def shared(*, ports: tuple[int, ...] = (), allow_hosts: bool = True) -> EgressProxy:
    """The process's proxy, started on first use; later calls update its ports and host switch."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = EgressProxy(ports=ports, allow_hosts=allow_hosts).start()
        else:
            _shared.ports |= set(ports)
            _shared.allow_hosts = allow_hosts
        return _shared


def current() -> EgressProxy | None:
    return _shared
