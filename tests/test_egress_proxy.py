# SPDX-License-Identifier: Apache-2.0
"""The egress proxy: who may be reached, and that every request is logged."""

from __future__ import annotations

import asyncio
import http.server
import socket
import threading
import unittest
from unittest import mock

from critique_bot import audit, egress, egress_proxy


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = f"path={self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        egress.configure("strict", "https://chat.example.com", ("pypi.org",))
        self.addCleanup(egress.configure, "strict")
        self.proxy = egress_proxy.EgressProxy(ports=(5037,), restricted=lambda: False)

    def test_strict_allows_the_allow_list_only(self) -> None:
        self.assertTrue(self.proxy.decide("pypi.org", 443).allowed)
        self.assertTrue(self.proxy.decide("files.pypi.org", 443).allowed)
        self.assertTrue(self.proxy.decide("chat.example.com", 443).allowed)
        denied = self.proxy.decide("evil.example.org", 443)
        self.assertFalse(denied.allowed)
        self.assertIn("strict", denied.reason)

    def test_open_allows_any_public_host(self) -> None:
        egress.configure("open")
        self.assertTrue(self.proxy.decide("evil.example.org", 443).allowed)

    def test_loopback_only_on_forwarded_ports(self) -> None:
        self.assertTrue(self.proxy.decide("127.0.0.1", 5037).allowed)
        self.assertFalse(self.proxy.decide("127.0.0.1", 8080).allowed)
        self.assertFalse(self.proxy.decide("localhost", 11434).allowed, "the host's model server is not reachable")
        self.assertFalse(self.proxy.decide("::1", 22).allowed)

    def test_link_local_never(self) -> None:
        egress.configure("open")
        self.assertFalse(self.proxy.decide("169.254.169.254", 80).allowed)

    def test_only_web_ports(self) -> None:
        egress.configure("open")
        self.assertFalse(self.proxy.decide("example.org", 22).allowed)
        self.assertFalse(self.proxy.decide("example.org", 25, method="GET").allowed)

    def test_offline_allows_no_host(self) -> None:
        self.proxy.allow_hosts = False
        self.assertFalse(self.proxy.decide("pypi.org", 443).allowed)
        self.assertTrue(self.proxy.decide("127.0.0.1", 5037).allowed, "adb still works offline")

    def test_tainted_auto_closes_the_forwarded_port_unless_only_device_logs_tainted(self) -> None:
        # sandbox-egress.md residual: adb can carry data to a device with its own network.
        for kinds, allowed in (({"web_fetch"}, False), ({"device_log", "mcp"}, False), ({"device_log"}, True),
                               (set(), False)):
            proxy = egress_proxy.EgressProxy(ports=(5037,), restricted=lambda: True, taint_kinds=lambda k=kinds: set(k))
            decision = proxy.decide("127.0.0.1", 5037)
            self.assertEqual(decision.allowed, allowed, kinds)
            if not allowed:
                self.assertIn("/trust", decision.reason)
        untainted = egress_proxy.EgressProxy(ports=(5037,), restricted=lambda: False, taint_kinds=lambda: {"web_fetch"})
        self.assertTrue(untainted.decide("127.0.0.1", 5037).allowed, "ask and edits modes keep adb")

    def test_taint_kinds_follow_the_sources(self) -> None:
        from critique_bot import taint

        taint.reset()
        self.addCleanup(taint.reset)
        self.assertEqual(taint.kinds(), set())
        taint.mark("device_log", "adb logcat")
        taint.mark("web_fetch", "https://x.example")
        self.assertEqual(taint.kinds(), {"device_log", "web_fetch"})
        taint.trust()
        self.assertEqual(taint.kinds(), set())

    def test_tainted_auto_reaches_only_the_model(self) -> None:
        proxy = egress_proxy.EgressProxy(restricted=lambda: True, model_host=lambda: "chat.example.com")
        self.assertTrue(proxy.decide("chat.example.com", 443).allowed)
        refused = proxy.decide("pypi.org", 443)
        self.assertFalse(refused.allowed)
        self.assertIn("tainted", refused.reason)

    def test_session_escape_adds_a_host(self) -> None:
        self.proxy.allow_host("mirror.corp.example")
        self.assertTrue(self.proxy.decide("mirror.corp.example", 443).allowed)
        self.assertTrue(self.proxy.decide("eu.mirror.corp.example", 443).allowed)

    def test_dns_rebinding_to_loopback_is_refused(self) -> None:
        async def fake_getaddrinfo(*_args, **_kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

        async def attempt() -> None:
            loop = asyncio.get_running_loop()
            with mock.patch.object(loop, "getaddrinfo", fake_getaddrinfo):
                await egress_proxy._open("rebind.example", 443, False)

        with self.assertRaises(OSError) as caught:
            asyncio.run(attempt())
        self.assertIn("127.0.0.1", str(caught.exception))


class LiveProxyTests(unittest.TestCase):
    """A real proxy and a real upstream on the loopback (a forwarded port stands in for an allowed host)."""

    def setUp(self) -> None:
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.proxy = egress_proxy.EgressProxy(ports=(self.port,), restricted=lambda: False).start()
        self.addCleanup(self.proxy.stop)
        self.records: list[dict] = []
        audit._sink = self.records
        self.addCleanup(setattr, audit, "_sink", None)
        egress.configure("strict", "https://chat.example.com")
        self.addCleanup(egress.configure, "strict")

    def ask(self, request: bytes, *, unix: bool = False) -> bytes:
        if unix:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(self.proxy.socket_path)
        else:
            sock = socket.create_connection(("127.0.0.1", self.proxy.port), 5)
        sock.settimeout(5)
        sock.sendall(request)
        data = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
                if request.startswith(b"CONNECT") and b"\r\n\r\n" in data and not data.startswith(b"HTTP/1.1 200"):
                    break
        except socket.timeout:
            pass
        sock.close()
        return data

    def test_connect_to_an_allowed_port_tunnels_and_is_logged(self) -> None:
        sock = socket.create_connection(("127.0.0.1", self.proxy.port), 5)
        sock.sendall(f"CONNECT 127.0.0.1:{self.port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += sock.recv(1)
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        sock.sendall(b"GET /tunnel HTTP/1.0\r\n\r\n")
        body = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
        sock.close()
        self.assertIn(b"path=/tunnel", body)
        import time

        deadline = time.monotonic() + 5  # the record is written when both sides of the tunnel closed
        while time.monotonic() < deadline and not any(item.get("allowed") for item in self.records):
            time.sleep(0.02)
        allowed = [item for item in self.records if item["kind"] == "egress" and item["allowed"]]
        self.assertEqual(allowed[-1]["method"], "CONNECT")
        self.assertGreater(allowed[-1]["received"], 0)

    def test_plain_http_is_forwarded(self) -> None:
        reply = self.ask(f"GET http://127.0.0.1:{self.port}/plain?q=1 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        self.assertIn(b"path=/plain?q=1", reply)

    def test_denied_host_gets_403_and_is_logged(self) -> None:
        reply = self.ask(b"CONNECT evil.example.org:443 HTTP/1.1\r\nHost: evil.example.org\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"), reply)
        self.assertIn(b"evil.example.org", reply)
        denied = [item for item in self.records if item["kind"] == "egress" and not item["allowed"]]
        self.assertEqual((denied[-1]["host"], denied[-1]["port"]), ("evil.example.org", 443))
        self.assertEqual([item.host for item in self.proxy.denied_since(0)], ["evil.example.org"])

    def test_the_unix_socket_serves_the_same_proxy(self) -> None:
        reply = self.ask(b"GET http://evil.example.org/ HTTP/1.1\r\nHost: evil.example.org\r\n\r\n", unix=True)
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"), reply)

    def test_garbage_is_refused(self) -> None:
        self.assertTrue(self.ask(b"HELLO\r\n\r\n").startswith(b"HTTP/1.1 400"))
        self.assertTrue(self.ask(b"GET /relative HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 400"))


if __name__ == "__main__":
    unittest.main()
