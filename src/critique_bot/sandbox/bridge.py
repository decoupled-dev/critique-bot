"""Runs inside the Linux sandbox, in front of the command: the only way out of its network namespace.

Usage: ``python -I -S bridge.py <unix socket> <proxy port> <ports> -- <command...>``

* ``127.0.0.1:<proxy port>`` inside the sandbox is crit's egress proxy: each connection is
  passed byte for byte over the unix socket (bound into the sandbox) to the proxy outside,
  which allows or denies each request by host.
* each of ``<ports>`` (comma separated, e.g. the adb server's 5037) is forwarded to the same
  port on the host's loopback, by asking the proxy to ``CONNECT 127.0.0.1:<port>``; the
  proxy decides whether that port is allowed.

Then the command runs with the same stdin, stdout and stderr; the bridge exits with its
exit code and passes SIGTERM/SIGINT/SIGHUP on. Standard library only: it is run by crit's
own interpreter with ``-I -S`` and must not import crit.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading


def _pipe(source: socket.socket, target: socket.socket) -> None:
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        for sock, how in ((target, socket.SHUT_WR), (source, socket.SHUT_RD)):
            try:
                sock.shutdown(how)
            except OSError:
                pass


def _join(client: socket.socket, upstream: socket.socket) -> None:
    def close_both() -> None:
        first.join()
        second.join()
        for sock in (client, upstream):
            try:
                sock.close()
            except OSError:
                pass

    first = threading.Thread(target=_pipe, args=(client, upstream), daemon=True)
    second = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
    first.start()
    second.start()
    threading.Thread(target=close_both, daemon=True).start()


def _open_upstream(path: str) -> socket.socket:
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    upstream.connect(path)
    return upstream


def _serve_proxy(listener: socket.socket, path: str) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        try:
            upstream = _open_upstream(path)
        except OSError:
            client.close()
            continue
        _join(client, upstream)


def _serve_port(listener: socket.socket, path: str, port: int) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        try:
            upstream = _open_upstream(path)
            upstream.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode("ascii"))
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 8192:
                chunk = upstream.recv(1)
                if not chunk:
                    break
                head += chunk
            if not head.startswith((b"HTTP/1.1 200", b"HTTP/1.0 200")):
                upstream.close()
                client.close()
                continue
        except OSError:
            client.close()
            continue
        _join(client, upstream)


def _listen(port: int) -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        return None
    sock.listen(64)
    return sock


def main(argv: list[str]) -> int:
    if "--" not in argv or len(argv) < 4:
        sys.stderr.write("usage: bridge.py SOCKET PROXY_PORT PORTS -- COMMAND...\n")
        return 2
    split = argv.index("--")
    path, proxy_port, ports = argv[0], int(argv[1] or 0), argv[2]
    command = argv[split + 1:]
    if not command:
        return 0
    if proxy_port:
        listener = _listen(proxy_port)
        if listener is not None:
            threading.Thread(target=_serve_proxy, args=(listener, path), daemon=True).start()
    for item in (ports or "").split(","):
        if item.strip().isdigit() and int(item) != proxy_port:
            listener = _listen(int(item))
            if listener is not None:
                threading.Thread(target=_serve_port, args=(listener, path, int(item)), daemon=True).start()
    try:
        child = subprocess.Popen(command)  # nosec B603: the sandboxed command itself
    except OSError as exc:
        sys.stderr.write(f"{command[0]}: {exc.strerror or exc}\n")
        return 127

    def forward(signum: int, _frame: object) -> None:
        try:
            child.send_signal(signum)
        except OSError:
            pass

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, forward)
    code = child.wait()
    sys.stdout.flush()
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    os._exit(main(sys.argv[1:]))
