# SPDX-License-Identifier: Apache-2.0
"""Run the agent's commands inside an OS sandbox.

Every command crit runs for the model (``run_command``, background jobs, the check
command, builds) starts through :func:`run`, which wraps the argv for this platform:

=====================  ==================================================  ===========================
Mode                   Filesystem                                          Network
=====================  ==================================================  ===========================
``read-only``          read what is not masked; write only a private /tmp   none
``workspace-write``    also write the workspace, ``.crit``, the caches      only through crit's egress
(default)              (``~/.gradle``, ``~/.m2``, ...) and extra paths      proxy (its allow-list)
``full``               the user's own rights (no sandbox)                   the egress policy
=====================  ==================================================  ===========================

In both sandboxed modes secret paths are masked (``~/.ssh``, ``~/.aws``, ``~/.config/gcloud``,
``~/.crit``, ``~/.netrc``, the workspace's ``.env*`` files, ...), and crit's own files in the
workspace (``.crit/settings.json``, ``.crit/skills``) plus ``.git/config`` and ``.git/hooks``
are read-only, so a command cannot loosen crit's rules or plant code that runs later.

Backends: Linux uses bubblewrap (:mod:`.linux`), macOS ``sandbox-exec`` with a generated
profile (:mod:`.macos`). Windows has no backend yet (:mod:`.windows`): commands run as today,
with the permission prompts kept. When the backend is missing (no ``bwrap``, user
namespaces disabled) crit says so once and runs commands as today (``crit doctor sandbox``
explains the fix).

Tools layer. Settings come from ``"sandbox"`` in ``.crit/settings.json`` (or config.json):
``"workspace-write"`` or ``{"mode": ..., "writable": [...], "preset": "aosp", "env_files": false,
"ports": [5037]}``. ``sandbox`` is in ``project_home.SECURITY_KEYS``: a committed settings file
cannot set it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

READ_ONLY = "read-only"
WORKSPACE_WRITE = "workspace-write"
FULL = "full"
MODES = (READ_ONLY, WORKSPACE_WRITE, FULL)
DEFAULT_MODE = WORKSPACE_WRITE
#: Inside the sandbox these are fresh, private tmpfs mounts (not the host's shared folders).
PRIVATE_TMP = "/tmp"  # nosec B108: a private tmpfs inside the sandbox
PRIVATE_SHM = "/dev/shm"  # nosec B108: a private tmpfs inside the sandbox

#: Caches builds write to, writable in workspace-write (each only when it exists or can be made).
DEFAULT_CACHES = ("~/.gradle", "~/.m2", "~/.npm", "~/.cache/pip", "~/.cache/yarn", "~/.cache/go-build", "~/.konan")
#: Presets add writable paths (relative ones are inside the workspace) and env defaults.
PRESETS: dict[str, dict[str, Any]] = {
    # AOSP: out/ (often a symlink to another disk), ccache and Bazel's caches.
    "aosp": {"writable": ("out", "~/.ccache", "~/.cache/ccache", "~/.cache/bazel"), "ports": (5037,)},
    "android": {"writable": ("~/.android/cache", "~/.android/build-cache"), "ports": (5037,)},
}
#: Loopback ports on the host a sandboxed command may reach (the adb server).
DEFAULT_PORTS = (5037,)

#: Secret locations in the home folder, masked in both sandboxed modes.
SECRET_HOME_PATHS = (
    "~/.ssh", "~/.aws", "~/.azure", "~/.config/gcloud", "~/.gnupg", "~/.kube", "~/.docker",
    "~/.netrc", "~/.git-credentials", "~/.pypirc", "~/.pgpass", "~/.vault-token", "~/.password-store",
    "~/.config/gh", "~/.config/glab-cli", "~/.config/hub", "~/.terraform.d", "~/.local/share/keyrings",
    "~/.mozilla", "~/.config/google-chrome", "~/.config/chromium", "~/.config/microsoft-edge",
)
#: Workspace files kept read-only inside the sandbox (crit's own rules, git's code hooks).
PROTECTED_WORKSPACE = (".crit/settings.json", ".crit/skills", ".crit/AGENT.md", ".git/config", ".git/hooks")


class SandboxError(RuntimeError):
    """The sandbox could not be set up for a command (the message says why and what to do)."""


@dataclass(frozen=True)
class Settings:
    """``"sandbox"`` from the settings, parsed."""

    mode: str = DEFAULT_MODE
    writable: tuple[str, ...] = ()
    preset: str = ""
    #: The workspace's .env files stay readable (they are masked by default).
    env_files: bool = False
    ports: tuple[int, ...] = DEFAULT_PORTS
    #: Set when the mode was asked for explicitly (not the default).
    explicit: bool = False


def parse_settings(value: Any, default: Any = None) -> Settings:
    """``"sandbox"`` as written in settings (a mode name or an object); ``default`` is config.json's.

    Raises ValueError with a message for the user when the value is not valid.
    """
    base = parse_settings(default) if default not in (None, "", {}) else Settings()
    if value in (None, "", {}):
        return base
    if isinstance(value, str):
        mode = _mode(value)
        return replace(base, mode=mode, explicit=True)
    if not isinstance(value, Mapping):
        raise ValueError('"sandbox" must be a mode name ("read-only", "workspace-write", "full") or an object')
    unknown = sorted(set(value) - {"mode", "writable", "preset", "env_files", "ports"})
    if unknown:
        raise ValueError(f'"sandbox" has unknown keys: {", ".join(unknown)} (known: mode, writable, preset, env_files, ports)')
    mode = _mode(value["mode"]) if value.get("mode") not in (None, "") else base.mode
    writable = value.get("writable", base.writable)
    if isinstance(writable, str):
        writable = [writable]
    if not isinstance(writable, (list, tuple)) or not all(isinstance(item, str) and item.strip() for item in writable):
        raise ValueError('"sandbox.writable" must be a list of folders')
    preset = str(value.get("preset") or base.preset or "").strip().lower()
    if preset and preset not in PRESETS:
        raise ValueError(f'"sandbox.preset" must be one of {", ".join(sorted(PRESETS))}, not {preset!r}')
    env_files = value.get("env_files", base.env_files)
    if not isinstance(env_files, bool):
        raise ValueError('"sandbox.env_files" must be true or false')
    ports = value.get("ports", base.ports)
    if not isinstance(ports, (list, tuple)) or not all(isinstance(p, int) and not isinstance(p, bool) and 0 < p < 65536 for p in ports):
        raise ValueError('"sandbox.ports" must be a list of port numbers')
    return Settings(
        mode=mode, writable=tuple(item.strip() for item in writable), preset=preset, env_files=env_files,
        ports=tuple(ports), explicit=value.get("mode") not in (None, "") or base.explicit,
    )


def _mode(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    text = {"readonly": READ_ONLY, "ro": READ_ONLY, "workspace": WORKSPACE_WRITE, "write": WORKSPACE_WRITE,
            "off": FULL, "none": FULL, "danger-full-access": FULL, "full-access": FULL}.get(text, text)
    if text not in MODES:
        raise ValueError(f'"sandbox" mode must be one of {", ".join(MODES)}, not {value!r}')
    return text


@dataclass(frozen=True)
class Policy:
    """Everything a backend needs to wrap one command."""

    mode: str
    workspace: Path
    home: Path
    #: Writable besides the workspace (workspace-write) or nothing (read-only): caches, preset, escapes.
    writable: tuple[Path, ...] = ()
    #: Always writable (crit's private shell folder: scripts, cwd and env markers).
    private: tuple[Path, ...] = ()
    #: Read-only paths re-exposed when the private /tmp would hide them (crit's own Python).
    readable: tuple[Path, ...] = ()
    #: Secret paths that read as empty.
    masked: tuple[Path, ...] = ()
    #: Inside writable roots but read-only (crit's settings, .git/config, .git/hooks).
    protected: tuple[Path, ...] = ()
    #: "none" or "proxy" (HTTP(S)_PROXY points at crit's egress proxy).
    network: str = "none"
    #: The egress proxy: a unix socket (Linux: bridged into the sandbox) and its TCP port.
    proxy_socket: str = ""
    proxy_port: int = 0
    #: Host loopback ports reachable from inside (through the proxy): the adb server.
    ports: tuple[int, ...] = ()
    #: Expose USB devices (adb/fastboot to a phone or head unit) in workspace-write.
    usb: bool = False

    @property
    def sandboxed(self) -> bool:
        return self.mode != FULL

    def can_write(self, target: Path) -> bool:
        """True when ``target`` is writable inside this sandbox (by the rules, not by the OS)."""
        if self.mode == FULL:
            return True
        path = _real(target)
        if any(_within(path, item) for item in self.protected + self.masked):
            return False
        roots = list(self.private)
        if self.mode == WORKSPACE_WRITE:
            roots += [self.workspace, *self.writable]
        return any(_within(path, root) for root in roots)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _real(path: Path | str) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def _expand(item: str, workspace: Path, home: Path) -> Path:
    text = item.strip()
    if text == "~" or text.startswith("~/") or text.startswith("~\\"):
        return home / text[2:] if len(text) > 1 else home
    path = Path(os.path.expandvars(text))
    return path if path.is_absolute() else workspace / path


def _env_files(workspace: Path, *, limit_dirs: int = 400) -> list[Path]:
    """``.env``, ``.env.*``, ``*.env``, ``.envrc`` near the top of the workspace (bounded scan)."""
    found: list[Path] = []
    skip = {".git", "out", "node_modules", "build", ".gradle", ".crit", ".repo", "prebuilts", "external"}
    queue: list[tuple[Path, int]] = [(workspace, 0)]
    seen = 0
    while queue and seen < limit_dirs:
        folder, depth = queue.pop(0)
        seen += 1
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if depth < 2 and name not in skip:
                    queue.append((Path(entry.path), depth + 1))
                continue
            lower = name.lower()
            if lower == ".env" or lower.startswith(".env.") or lower == ".envrc" or lower.endswith(".env"):
                found.append(Path(entry.path))
    return found


def git_dirs(workspace: Path) -> tuple[list[Path], list[Path]]:
    """For a git worktree or submodule (``.git`` is a file): its git folders outside the workspace,
    (writable, protected). Commits need the objects, refs, and index; ``config`` and ``hooks`` stay read-only."""
    marker = workspace / ".git"
    if not marker.is_file():
        return [], []
    try:
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return [], []
    if not text.startswith("gitdir:"):
        return [], []
    gitdir = Path(text[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = workspace / gitdir
    gitdir = _real(gitdir)
    if not gitdir.is_dir():
        return [], []
    writable = [gitdir]
    common = gitdir
    try:
        pointer = (gitdir / "commondir").read_text(encoding="utf-8").strip()
        common = _real(gitdir / pointer) if not Path(pointer).is_absolute() else _real(pointer)
    except OSError:
        pass
    if common != gitdir and common.is_dir():
        writable.append(common)
    protected = [item for root in dict.fromkeys([gitdir, common]) for item in (root / "config", root / "hooks")
                 if os.path.lexists(item)]
    return writable, [_real(item) for item in protected]


def build_policy(
    settings: Settings,
    workspace: Path | str,
    *,
    home: Path | str | None = None,
    private: tuple[Path | str, ...] = (),
    extra_writable: tuple[Path | str, ...] = (),
    proxy_socket: str = "",
    proxy_port: int = 0,
    network: bool = False,
    make_caches: bool = True,
) -> Policy:
    """The policy for a session in ``workspace`` under ``settings``.

    ``network`` True: HTTP(S) goes through the egress proxy (``proxy_socket``/``proxy_port``);
    False: no network at all (strict egress or offline). Loopback ``ports`` are bridged only
    when a proxy is given.
    """
    root = _real(workspace)
    home_dir = _real(home if home is not None else os.path.expanduser("~"))
    if settings.mode == FULL:
        return Policy(mode=FULL, workspace=root, home=home_dir)
    writable: list[Path] = []
    if settings.mode == WORKSPACE_WRITE:
        wanted = list(DEFAULT_CACHES) + list(settings.writable)
        preset = PRESETS.get(settings.preset, {})
        wanted += list(preset.get("writable", ()))
        for item in wanted:
            path = _expand(item, root, home_dir)
            if not path.exists() and make_caches and (item.startswith("~") or item in preset.get("writable", ())):
                # A cache that does not exist yet is made now, so the first build can fill it.
                try:
                    path.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
            if path.exists():
                real = _real(path)
                if real not in writable and real != Path("/") and real != home_dir:
                    writable.append(real)
        for item in extra_writable:
            real = _real(item)
            if real.exists() and real not in writable:
                writable.append(real)
    git_writable, git_protected = git_dirs(root)
    if settings.mode == WORKSPACE_WRITE:
        writable += [item for item in git_writable if item not in writable and not _within(item, root)]
    masked: list[Path] = []
    for item in SECRET_HOME_PATHS:
        path = _expand(item, root, home_dir)
        if os.path.lexists(path):
            masked.append(_real(path))
    crit_home = _crit_home(home_dir)
    if crit_home is not None and os.path.lexists(crit_home) and not _within(root, _real(crit_home)):
        masked.append(_real(crit_home))
    elif crit_home is not None and os.path.lexists(crit_home):
        # The workspace is inside ~/.crit (the home folder's own project data): mask the secrets only.
        for name in ("config.json", "browser-profile", "msedge-user-data", "cache"):
            if os.path.lexists(crit_home / name):
                masked.append(_real(crit_home / name))
        masked += [_real(item) for item in crit_home.glob("*.key")]
    run_user = Path(f"/run/user/{os.getuid()}") if hasattr(os, "getuid") else None
    if run_user is not None and run_user.is_dir():
        masked.append(run_user)  # ssh-agent and gpg-agent sockets, the session bus
    if not settings.env_files:
        masked += [_real(item) for item in _env_files(root)]
    protected = [_real(root / item) for item in PROTECTED_WORKSPACE if os.path.lexists(root / item)]
    protected += [item for item in git_protected if item not in protected]
    readable: list[Path] = []
    for item in {sys.prefix, sys.base_prefix, str(Path(__file__).resolve().parents[1])}:
        real = _real(item)
        if real not in readable:
            readable.append(real)
    ports = tuple(settings.ports) + tuple(PRESETS.get(settings.preset, {}).get("ports", ()))
    return Policy(
        mode=settings.mode,
        workspace=root,
        home=home_dir,
        writable=tuple(writable),
        private=tuple(_real(item) for item in private if os.path.isdir(item)),
        readable=tuple(readable),
        masked=tuple(dict.fromkeys(masked)),
        protected=tuple(protected),
        network="proxy" if (network and proxy_socket or network and proxy_port) else "none",
        proxy_socket=proxy_socket,
        proxy_port=proxy_port,
        ports=tuple(dict.fromkeys(ports)) if (proxy_socket or proxy_port) else (),
        usb=settings.mode == WORKSPACE_WRITE and os.path.isdir("/dev/bus/usb"),
    )


def _crit_home(home: Path) -> Path | None:
    override = os.environ.get("CRIT_HOME", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return home / ".crit"


# --------------------------------------------------------------------------- backends


@dataclass(frozen=True)
class Availability:
    """Whether commands can be sandboxed here, and if not, why and how to fix it."""

    ok: bool
    backend: str
    #: What is missing ("" when ok).
    problem: str = ""
    #: What the user can do about it ("" when ok).
    fix: str = ""
    #: Details for crit doctor sandbox.
    details: tuple[str, ...] = field(default_factory=tuple)


def _platform(name: str | None) -> str:
    plat = name or sys.platform
    if plat.startswith("linux"):
        return "linux"
    if plat == "darwin":
        return "macos"
    if plat in {"win32", "cygwin", "msys"}:
        return "windows"
    return plat


_AVAILABILITY: dict[str, Availability] = {}


def availability(platform_name: str | None = None, *, refresh: bool = False) -> Availability:
    """Can this machine sandbox commands? Probed once per process (a real sandboxed ``true``)."""
    plat = _platform(platform_name)
    if not refresh and plat in _AVAILABILITY:
        return _AVAILABILITY[plat]
    if plat == "linux":
        from critique_bot.sandbox import linux

        found = linux.probe()
    elif plat == "macos":
        from critique_bot.sandbox import macos

        found = macos.probe()
    elif plat == "windows":
        from critique_bot.sandbox import windows

        found = windows.probe()
    else:
        found = Availability(False, "none", f"no sandbox backend for {plat}", "commands run as before, with permission prompts")
    _AVAILABILITY[plat] = found
    return found


def wrap(
    argv: list[str], policy: Policy, env: Mapping[str, str], *, cwd: Path | str, platform_name: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """``argv`` and ``env`` that run the command inside ``policy`` on this platform."""
    child_env = dict(env)
    if policy.mode == FULL:
        return list(argv), child_env
    child_env = sandbox_env(child_env, policy)
    plat = _platform(platform_name)
    if plat == "linux":
        from critique_bot.sandbox import linux

        return linux.wrap(argv, policy, child_env, cwd=Path(cwd)), child_env
    if plat == "macos":
        from critique_bot.sandbox import macos

        return macos.wrap(argv, policy, cwd=Path(cwd)), child_env
    return list(argv), child_env


def proxy_env(port: int) -> dict[str, str]:
    """HTTP(S)_PROXY and the JVM's proxy properties pointing at the egress proxy on ``port``."""
    url = f"http://127.0.0.1:{port}"
    return {
        "HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url, "https_proxy": url,
        "ALL_PROXY": url, "all_proxy": url,
        "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1",
    }


def _jvm_proxy(port: int) -> str:
    return (f"-Dhttp.proxyHost=127.0.0.1 -Dhttp.proxyPort={port} -Dhttps.proxyHost=127.0.0.1 "
            f"-Dhttps.proxyPort={port} -Dhttp.nonProxyHosts=localhost|127.0.0.1")


def sandbox_env(env: dict[str, str], policy: Policy) -> dict[str, str]:
    """The environment inside the sandbox: private temp, no agent sockets, the proxy when there is one."""
    env = dict(env)
    env["TMPDIR"] = PRIVATE_TMP
    for name in ("TEMP", "TMP"):
        if name in env:
            env[name] = PRIVATE_TMP
    for name in ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "GPG_AGENT_INFO", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
        env.pop(name, None)
    env["CRIT_SANDBOX"] = policy.mode
    if policy.network == "proxy" and policy.proxy_port:
        offline = env.get("HTTPS_PROXY", "").endswith(":9")  # offline mode keeps its closed proxy
        if not offline:
            env.update(proxy_env(policy.proxy_port))
            for name in ("GRADLE_OPTS", "JAVA_TOOL_OPTIONS"):
                env[name] = (env.get(name, "") + " " + _jvm_proxy(policy.proxy_port)).strip()
    return env


def run(
    argv: list[str],
    *,
    cwd: Path | str,
    env: Mapping[str, str] | None = None,
    policy: Policy | None = None,
    mode: str | None = None,
    platform_name: str | None = None,
    popen: Any = None,
    **kwargs: Any,
) -> subprocess.Popen:
    """Start ``argv`` in the sandbox ``policy`` (or a default one for ``mode``); a ``Popen``.

    ``policy`` None and ``mode`` None or "full": the command runs unwrapped, as before.
    When the backend is unavailable the command also runs unwrapped: callers check
    :func:`availability` first and tell the user (see ``agent_shell.use_sandbox``).
    """
    if policy is None and mode not in (None, FULL):
        policy = build_policy(Settings(mode=_mode(mode)), cwd)
    base_env = dict(os.environ if env is None else env)
    if policy is not None and policy.sandboxed and availability(platform_name).ok:
        argv, base_env = wrap(argv, policy, base_env, cwd=cwd, platform_name=platform_name)
    starter = popen or subprocess.Popen
    return starter(argv, cwd=str(cwd), env=base_env, **kwargs)


# --------------------------------------------------------------------------- blocked writes


def blocked_paths(output: str) -> list[str]:
    """Paths a command could not write because of the sandbox, from its output (``Read-only file system``)."""
    import re

    found: list[str] = []
    patterns = (
        r"[`'‘\"]([^`'’\"\n]+)[`'’\"][^\n]{0,40}?Read-only file system",
        r"Read-only file system[:,]?\s*[`'‘\"]([^`'’\"\n]+)[`'’\"]",
        r"(/[^\s:'\"()‘’`]+)\s*\(Read-only file system\)",
        r"(/[^\s:'\"()‘’`]+):\s*Read-only file system",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, output or ""):
            path = match.group(1).strip()
            if path.startswith(("/", "~")) and path not in found:
                found.append(path)
    return found[:5]


def escape_target(path: str, policy: Policy) -> Path | None:
    """The folder to offer as a writable escape for a blocked ``path``, or None when none is safe to offer.

    The folder itself when it exists, else its parent; never ``/``, the home folder, a masked
    secret, or a protected file.
    """
    target = _real(path)
    if not target.is_dir():
        target = target.parent
    if not target.exists():
        return None
    if target == Path("/") or target == policy.home or len(target.parts) <= 2:
        return None
    if any(_within(target, item) or _within(item, target) for item in policy.masked + policy.protected):
        return None
    return target
