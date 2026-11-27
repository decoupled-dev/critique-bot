# SPDX-License-Identifier: Apache-2.0
"""macOS backend: ``sandbox-exec -p <profile>`` with a generated Seatbelt (SBPL) profile.

The profile denies by default, then allows running programs, looking up a short list of
system services (:data:`MACH_SERVICES`, not every daemon), reading files (secret paths denied
again), writing only in the workspace, caches, and temp folders (crit's own files
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


#: The system services a sandboxed command may look up (mach-lookup). A blanket
#: ``(allow mach-lookup)`` lets a command ask daemons such as ``nsurlsessiond``, ``mDNSResponder``
#: (DNS) or ``securityd`` (the keychain) to work for it outside the sandbox, past the network and
#: secret rules. These are the ones command-line tools need: user and group lookups, logging,
#: notifications, preferences, temp folders, file events, TLS certificate checks.
MACH_SERVICES = (
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.opendirectoryd.membership",
    "com.apple.system.DirectoryService.libinfo_v1",
    "com.apple.system.logger",
    "com.apple.logd",
    "com.apple.system.notification_center",
    "com.apple.cfprefsd.daemon",
    "com.apple.cfprefsd.agent",
    "com.apple.bsd.dirhelper",
    "com.apple.FSEvents",
    "com.apple.PowerManagement.control",
    "com.apple.trustd",
    "com.apple.trustd.agent",
)
#: Never: they act for the caller outside the sandbox (``open`` a URL in the browser, DNS, the
#: keychain, background downloads). Kept as a list so the tests can say so.
MACH_DENIED = ("com.apple.coreservices.launchservicesd", "com.apple.lsd.mapdb", "com.apple.dnssd.service",
               "com.apple.SecurityServer", "com.apple.securityd", "com.apple.nsurlsessiond",
               "com.apple.nsurlsessiond.NSURLSessionProxyService")


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
        "(allow mach-lookup " + " ".join(f"(global-name {quote(name)})" for name in MACH_SERVICES) + ")",
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
