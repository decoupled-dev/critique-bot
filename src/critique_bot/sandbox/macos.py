"""macOS backend: ``sandbox-exec -p <profile>`` with a generated Seatbelt (SBPL) profile.

The profile denies by default, then allows running programs, reading files (secret paths
denied again), writing only in the workspace, caches, and temp folders (crit's own files
denied again), and network only to the egress proxy on localhost. ``sandbox-exec`` is
deprecated by Apple but present on every macOS release and used the same way by other
coding agents; :func:`profile` is unit-tested, the real run needs a Mac.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from critique_bot.sandbox import WORKSPACE_WRITE, Availability, Policy

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def probe(runner=subprocess.run) -> Availability:
    exe = SANDBOX_EXEC if os.path.exists(SANDBOX_EXEC) else shutil.which("sandbox-exec")
    if not exe:
        return Availability(False, "sandbox-exec", "sandbox-exec is missing", "It ships with macOS; check /usr/bin/sandbox-exec.")
    try:
        done = runner([exe, "-p", "(version 1)(allow default)", "/usr/bin/true"], capture_output=True, text=True,
                      timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Availability(False, "sandbox-exec", f"sandbox-exec could not run: {exc}", "Run crit outside another sandbox.")
    if done.returncode != 0:
        return Availability(False, "sandbox-exec", f"sandbox-exec failed: {(done.stderr or '').strip()}",
                            "crit may already run inside a sandbox (nested sandboxes are not allowed).")
    return Availability(True, "sandbox-exec", details=(exe,))


def quote(text: str) -> str:
    """An SBPL string literal."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _temp_roots() -> list[str]:
    roots = ["/private/tmp", "/private/var/folders"]
    tmp = os.environ.get("TMPDIR", "")
    if tmp:
        roots.append(os.path.realpath(tmp))
    return list(dict.fromkeys(roots))


def profile(policy: Policy, *, temp_roots: list[str] | None = None) -> str:
    """The Seatbelt profile text for ``policy``."""
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow signal (target same-sandbox))",
        "(allow process-info* (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix-shm)",
        "(allow ipc-posix-sem)",
        "(allow iokit-open)",
        "(allow pseudo-tty)",
        "(allow file-ioctl)",
        "(allow file-read*)",
    ]
    writable = [str(policy.workspace), *map(str, policy.writable)] if policy.mode == WORKSPACE_WRITE else []
    writable += [str(item) for item in policy.private]
    writable += temp_roots if temp_roots is not None else _temp_roots()
    rules = " ".join(f"(subpath {quote(item)})" for item in dict.fromkeys(writable))
    lines.append(f"(allow file-write* {rules} (literal \"/dev/null\") (literal \"/dev/tty\") (regex #\"^/dev/fd/\"))")
    for item in policy.masked:
        kind = "subpath" if os.path.isdir(item) else "literal"
        lines.append(f"(deny file-read* file-write* ({kind} {quote(str(item))}))")
    for item in policy.protected:
        kind = "subpath" if os.path.isdir(item) else "literal"
        lines.append(f"(deny file-write* ({kind} {quote(str(item))}))")
    # A test server inside the command may listen on localhost; nothing else is reachable.
    lines.append('(allow network-bind (local ip "localhost:*"))')
    lines.append('(allow network-inbound (local ip "localhost:*"))')
    if policy.network == "proxy" and policy.proxy_port:
        lines.append(f'(allow network-outbound (remote ip "localhost:{int(policy.proxy_port)}"))')
    for port in policy.ports:
        lines.append(f'(allow network-outbound (remote ip "localhost:{int(port)}"))')
    return "\n".join(lines) + "\n"


def wrap(argv: list[str], policy: Policy, *, cwd: Path) -> list[str]:
    del cwd  # sandbox-exec keeps the working folder Popen sets
    return [SANDBOX_EXEC, "-p", profile(policy), *argv]
