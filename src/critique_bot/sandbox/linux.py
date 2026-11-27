# SPDX-License-Identifier: Apache-2.0
"""Linux backend: bubblewrap (``bwrap``).

The command runs in new user, mount, PID, IPC, UTS, cgroup and network namespaces:

* ``/`` is bound read-only; ``/tmp`` is a private tmpfs; ``/dev`` and ``/proc`` are fresh.
* workspace-write binds the workspace, the caches, and extra paths writable over that.
* secret folders become empty read-only tmpfs mounts, secret files read as ``/dev/null``.
* crit's own files and git's code hooks in the workspace are bound read-only again.
* the network namespace has only loopback. With a proxy, :mod:`.bridge` (run inside, by
  crit's own Python) listens on ``127.0.0.1:<port>`` and forwards each connection over a
  unix socket bound into the sandbox to crit's egress proxy, which applies the allow-list.
  The adb server port (and other ``ports``) is forwarded the same way.
* ``--die-with-parent`` (crit exits: the sandbox goes), ``--new-session`` (no TIOCSTI
  typing into crit's terminal).

Landlock (kernel 5.13+) as a second layer is not used yet. TODO(sandbox): add it for
processes crit starts outside bwrap; doing it from Python needs ctypes ``syscall`` calls
in a ``preexec_fn``, which is not safe with crit's threads, so it waits for a small helper.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from critique_bot.sandbox import PRIVATE_SHM, PRIVATE_TMP, WORKSPACE_WRITE, Availability, Policy

BRIDGE = Path(__file__).with_name("bridge.py")

INSTALL_HINT = (
    "Install bubblewrap: sudo apt install bubblewrap (Debian, Ubuntu), sudo dnf install bubblewrap "
    "(Fedora, RHEL), sudo pacman -S bubblewrap (Arch), sudo zypper install bubblewrap (openSUSE)."
)
USERNS_HINT = (
    "Unprivileged user namespaces are disabled. Ubuntu 23.10+ (AppArmor): allow bwrap with an AppArmor "
    "profile (sudo tee /etc/apparmor.d/bwrap <<< 'abi <abi/4.0>, include <tunables/global> "
    "profile bwrap /usr/bin/bwrap flags=(unconfined) { userns, }' && sudo systemctl reload apparmor), or "
    "sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0. Debian before 11: "
    "sudo sysctl kernel.unprivileged_userns_clone=1. RHEL/CentOS 7: sudo sysctl user.max_user_namespaces=15000. "
    "Make a sysctl permanent in /etc/sysctl.d/."
)


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def probe(bwrap: str | None = None, runner=subprocess.run) -> Availability:
    """Run ``true`` in the same namespaces crit uses; explain what fails."""
    exe = bwrap or bwrap_path()
    details = []
    for name in ("kernel/unprivileged_userns_clone", "kernel/apparmor_restrict_unprivileged_userns", "user/max_user_namespaces"):
        value = _read(f"/proc/sys/{name}")
        if value:
            details.append(f"{name.replace('/', '.')} = {value}")
    if not exe:
        return Availability(False, "bwrap", "bubblewrap (bwrap) is not installed", INSTALL_HINT, tuple(details))
    argv = [exe, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-all",
            "--die-with-parent", "--new-session", "true"]
    try:
        done = runner(argv, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Availability(False, "bwrap", f"bwrap could not run: {exc}", INSTALL_HINT, tuple(details))
    if done.returncode == 0:
        version = ""
        try:
            version = runner([exe, "--version"], capture_output=True, text=True, timeout=5, check=False).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        return Availability(True, "bwrap", details=tuple([f"{exe} {version}".strip(), *details]))
    error = (done.stderr or done.stdout or "").strip().splitlines()
    message = error[-1] if error else f"exit {done.returncode}"
    lowered = message.lower()
    if any(word in lowered for word in ("namespace", "uid map", "permission denied", "operation not permitted", "setting up")):
        return Availability(False, "bwrap", f"bwrap cannot create a sandbox: {message}", USERNS_HINT, tuple(details))
    return Availability(False, "bwrap", f"bwrap failed: {message}", USERNS_HINT, tuple(details))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def bwrap_args(policy: Policy, *, cwd: Path, tmp: Path = Path(PRIVATE_TMP)) -> list[str]:
    """The bwrap options for ``policy`` (without the bwrap program and the command)."""
    args = ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", str(tmp),
            "--tmpfs", PRIVATE_SHM]
    if policy.usb and os.path.isdir("/dev/bus/usb"):
        args += ["--dev-bind", "/dev/bus/usb", "/dev/bus/usb"]
    hidden = [tmp, *[item for item in policy.masked if os.path.isdir(item)]]

    def needs_rebind(path: Path) -> bool:
        return any(_inside(path, root) for root in hidden)

    if policy.mode == WORKSPACE_WRITE:
        args += ["--bind", str(policy.workspace), str(policy.workspace)]
        for item in policy.writable:
            args += ["--bind", str(item), str(item)]
    else:
        args += ["--ro-bind", str(policy.workspace), str(policy.workspace)]
    for item in policy.private:
        args += ["--bind", str(item), str(item)]
    if policy.proxy_socket:
        folder = str(Path(policy.proxy_socket).parent)
        if not any(_inside(Path(folder), item) for item in policy.private):
            args += ["--ro-bind", folder, folder]
    dirs: list[Path] = []
    for item in policy.masked:
        if os.path.isdir(item):
            args += ["--tmpfs", str(item)]
            dirs.append(item)
        elif os.path.exists(item):
            args += ["--ro-bind", "/dev/null", str(item)]
    for item in policy.readable:
        if needs_rebind(item) and os.path.exists(item):
            args += ["--ro-bind", str(item), str(item)]
    if policy.mode == WORKSPACE_WRITE:
        # A writable cache inside a masked folder (rare) is bound again over the mask.
        for item in policy.writable:
            if any(_inside(item, masked) for masked in dirs):
                args += ["--bind", str(item), str(item)]
    for item in policy.protected:
        if os.path.exists(item):
            args += ["--ro-bind", str(item), str(item)]
    for item in dirs:
        args += ["--remount-ro", str(item)]
    args += ["--unshare-all", "--die-with-parent", "--new-session", "--chdir", str(cwd)]
    return args


def bridge_argv(policy: Policy) -> list[str]:
    """The in-sandbox bridge to the proxy, run by crit's own Python (stdlib only, isolated)."""
    ports = ",".join(str(port) for port in policy.ports)
    port = policy.proxy_port if policy.network == "proxy" else 0
    return [sys.executable, "-I", "-S", str(BRIDGE), policy.proxy_socket, str(port or 0), ports, "--"]


def wrap(argv: list[str], policy: Policy, env: dict[str, str], *, cwd: Path, bwrap: str | None = None) -> list[str]:
    """``argv`` run inside bwrap for ``policy``."""
    exe = bwrap or bwrap_path() or "bwrap"
    inner = list(argv)
    if policy.proxy_socket and (policy.network == "proxy" or policy.ports):
        inner = bridge_argv(policy) + inner
    return [exe, *bwrap_args(policy, cwd=cwd), "--", *inner]
