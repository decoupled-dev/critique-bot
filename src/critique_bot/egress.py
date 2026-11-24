"""Where crit may connect: the egress policy from config.json ``"egress"``.

``"strict"`` (the default): the only machine crit talks to is the chat at ``url`` (its
exact host) plus the hosts listed in ``allowed_hosts``. Concretely:

* the browser crit starts cannot resolve any other host name (Chromium resolver rules),
  uses no proxy, blocks service workers, and its page filter allows only those hosts;
* the agent's commands run offline (package managers offline, proxies pointed at a
  closed local port, git local only), ``web_fetch`` is off, and commands that can send
  data are blocked outright (``"network_commands": "ask"`` is ignored);
* GitLab is reached only at the host of ``gitlab.base_url`` (or one in ``allowed_hosts``);
* a browser crit did not start (``cdp_url``) is refused, since its traffic is not crit's to limit.

``"open"``: the earlier behavior. The chat page runs as it does for a person (its CDNs
and sign-in hosts work), builds may download dependencies, and the optional settings
(``filter_chat_page``, ``offline``, ``web_fetch``) apply as configured.

The policy is process-wide: ``config.load_config`` sets it, everything else reads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

STRICT = "strict"
OPEN = "open"
MODES = (STRICT, OPEN)


@dataclass(frozen=True)
class Policy:
    mode: str = STRICT
    #: The chat's exact host ("" for the local backend or before a config is loaded).
    chat_host: str = ""
    #: config.json allowed_hosts; each also covers its subdomains.
    allowed_hosts: tuple[str, ...] = ()
    #: gitlab.base_url's host: reachable by the GitLab client only, never by the browser.
    gitlab_host: str = ""
    #: False until a config.json was loaded (``gitlab-post`` in CI may run without one).
    configured: bool = False

    @property
    def strict(self) -> bool:
        return self.mode == STRICT

    def hosts(self) -> tuple[str, ...]:
        """Every host crit may reach under this policy (the chat host first)."""
        return tuple(dict.fromkeys(h for h in (self.chat_host, *self.allowed_hosts) if h))

    def permits(self, host: str) -> bool:
        """True when ``host`` may be contacted. Loopback is always allowed (it never leaves the PC)."""
        host = (host or "").lower().rstrip(".")
        if not host:
            return False
        if host in _LOOPBACK:
            return True
        if not self.strict:
            return True
        if host in (self.chat_host, self.gitlab_host):
            return True
        return any(host == allowed or host.endswith("." + allowed) for allowed in self.allowed_hosts)


_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
_policy = Policy()


def configure(mode: str, chat_url: str = "", allowed_hosts: tuple[str, ...] = (), gitlab_url: str = "") -> Policy:
    """Set the process-wide policy (called by config.load_config); return it."""
    global _policy
    if mode not in MODES:
        raise ValueError(f"egress must be one of {', '.join(MODES)}, not {mode!r}")
    host = (urlsplit(chat_url).hostname or "").lower().rstrip(".") if chat_url else ""
    gitlab_host = (urlsplit(gitlab_url).hostname or "").lower().rstrip(".") if gitlab_url else ""
    _policy = Policy(
        mode=mode,
        chat_host=host,
        allowed_hosts=tuple(h.lower().rstrip(".") for h in allowed_hosts),
        gitlab_host=gitlab_host,
        configured=True,
    )
    return _policy


def policy() -> Policy:
    return _policy


def strict() -> bool:
    return _policy.strict


def require(host: str, what: str) -> None:
    """Raise PermissionError when the loaded config's policy does not allow contacting ``host`` for ``what``."""
    if _policy.configured and not _policy.permits(host):
        raise PermissionError(
            f'egress is "strict": {what} would contact {host}, which is not the chat host or in allowed_hosts. '
            f'To allow it, run crit config --allow-host {host} (adds "{host}" to allowed_hosts), '
            "or crit config --egress open."
        )
