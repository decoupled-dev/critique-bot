# SPDX-License-Identifier: Apache-2.0
"""Adversarial review (sec-surface): crit serve. The server binds 127.0.0.1 on a random port and is
stopped by each test (the harness of tests/test_server.py)."""

from __future__ import annotations

import socket
import unittest

import test_00_isolation  # noqa: F401  (this package sorts before it: isolate HOME/CRIT_HOME first)
from test_server import _ServerCase  # noqa: E402  (a TestCase with no tests of its own)


class ServerAuthHoldsTest(_ServerCase):
    """What held up: every route and method variant needs the token (kept as a regression guard)."""

    def test_no_route_or_method_variant_answers_without_the_token(self) -> None:
        host, port = self.url.split("//", 1)[1].rsplit(":", 1)
        for method, path in (("GET", "/v1/health"), ("GET", "//v1/health"), ("GET", "/v1/health/"),
                             ("GET", "/v1/%68ealth"), ("GET", "/v1/sessions/x/events"), ("POST", "/v1/sessions"),
                             ("PUT", "/v1/sessions/x/mode"), ("DELETE", "/v1/sessions"),
                             ("OPTIONS", "/v1/health"), ("HEAD", "/v1/health")):
            with socket.create_connection((host, int(port)), timeout=10) as conn:
                conn.sendall(f"{method} {path} HTTP/1.1\r\nHost: evil.example\r\nOrigin: http://evil.example\r\n"
                             "Content-Length: 0\r\nConnection: close\r\n\r\n".encode())
                reply = b""
                while chunk := conn.recv(4096):
                    reply += chunk
                status = reply.split(b"\r\n", 1)[0]
            self.assertTrue(status.split()[1] in {b"401", b"501"}, f"{method} {path}: {status!r}")
        status, _ = self.call("GET", "/v1/health", token=self.app.token + "x")
        self.assertEqual(status, 401)


class ServerIdleConnectionTest(_ServerCase):
    # SECURITY FINDING SF-10: no socket timeout; an unauthenticated client holds a server thread forever.
    @unittest.expectedFailure
    def test_an_unauthenticated_idle_connection_is_dropped(self) -> None:
        # Demonstration (manual): connect, send "GET /v1/health HTTP/1.1\r\n" and nothing more; the
        # connection stays open indefinitely, one thread each (ThreadingHTTPServer, no limit).
        handler = self.app.httpd.RequestHandlerClass
        self.assertIsNotNone(getattr(handler, "timeout", None), "the request handler has no socket timeout")


if __name__ == "__main__":
    unittest.main()
