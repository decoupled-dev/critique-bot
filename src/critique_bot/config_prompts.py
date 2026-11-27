# SPDX-License-Identifier: Apache-2.0
"""Settings crit asks for while it works, the first time they are needed.

Only in a terminal session that ``enable`` switched on (the command line does that for an
interactive ``crit``, ``crit chat``, ``crit ask``, or ``crit review``); never in the worker,
CI, ``--acp``, or with input piped in. ``--yes`` does not answer these: each one widens
what crit may reach, so the default answer is No.

* A site the chat page tried to reach and the page filter blocked (only with
  ``"page_network": "strict"``): after the current step, crit asks once per site whether to add it to ``allowed_hosts`` (from the next start on).
* ``gitlab.base_url`` when a command needs GitLab and none is set.

Answers are saved in the config file in use. The browser collects blocked sites (see
``browser.drain_blocked_hosts``); the command line hands that function in through ``enable``
so this module stays below the model layer.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Callable

from critique_bot import egress, log
from critique_bot.config import (
    ConfigError,
    _host_list,
    clean_gitlab_url,
    read_config_dict,
    write_config_file,
)

Confirm = Callable[[str], bool]
AskText = Callable[[str], str]

_lock = threading.Lock()
_path: Path | None = None
_confirm: Confirm | None = None
_ask_text: AskText | None = None
_drain: Callable[[], list[str]] | None = None
_asked_hosts: set[str] = set()

BLOCKED_QUESTION = (
    "The page tried to reach {host}. Allow it from now on? "
    "Only say yes if it belongs to the service at your url."
)


def _plain_confirm(question: str) -> bool:
    answer = input(f"{question} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def enable(
    path: str | Path,
    *,
    confirm: Confirm | None = None,
    ask_text: AskText | None = None,
    drain: Callable[[], list[str]] | None = None,
) -> None:
    """Ask as needed from now on, saving answers in ``path``."""
    global _path, _confirm, _ask_text, _drain
    with _lock:
        _path = Path(path)
        _confirm = confirm or _plain_confirm
        _ask_text = ask_text or input
        if drain is not None:
            _drain = drain


def disable() -> None:
    global _path, _confirm, _ask_text, _drain
    with _lock:
        _path = _confirm = _ask_text = _drain = None
        _asked_hosts.clear()


def enabled() -> bool:
    return _path is not None


def config_path() -> Path | None:
    return _path


def add_allowed_host(path: str | Path, host: str) -> tuple[str, ...]:
    """Add ``host`` to ``allowed_hosts`` in the config file at ``path``; the new list."""
    data = read_config_dict(path)
    existing = data.get("allowed_hosts") if isinstance(data.get("allowed_hosts"), list) else []
    hosts = _host_list(list(existing) + [host])
    data["allowed_hosts"] = list(hosts)
    write_config_file(path, data)
    return hosts


def offer_blocked_hosts() -> list[str]:
    """Ask about each site the chat page was blocked from since the last call. The sites allowed."""
    drain = _drain
    if drain is None:
        return []
    try:
        hosts = list(drain())
    except Exception as exc:  # never let a question break the session
        log.debug(f"blocked-host list unavailable: {exc}")
        return []
    path, confirm = _path, _confirm
    if path is None or confirm is None:
        return []
    if not egress.page_restricted():
        # page_network "open": the page reaches what it needs; a blocked site is only logged.
        for host in hosts:
            log.info(f"the page was kept from {host}; allow it with: crit config --allow-host {host}")
        return []
    added: list[str] = []
    for host in hosts:
        if not host or host in _asked_hosts:
            continue
        _asked_hosts.add(host)
        try:
            yes = bool(confirm(BLOCKED_QUESTION.format(host=host)))
        except (EOFError, KeyboardInterrupt):
            yes = False
        if not yes:
            _say(f"{host} stays blocked.")
            continue
        try:
            add_allowed_host(path, host)
        except (ConfigError, OSError) as exc:
            log.debug(f"could not save allowed host {host}: {exc}")
            _say(f"Could not save that: {exc}. Run: crit config --allow-host {host}")
            continue
        added.append(host)
        _say(f"Allowed {host}; this takes effect the next time crit starts.")
    return added


def ensure_gitlab_url(current: str = "") -> str:
    """The GitLab address: ``current`` when set, else asked for (and saved) in a terminal session; "" otherwise."""
    if current:
        return current
    path, ask = _path, _ask_text
    if path is None or ask is None:
        return ""
    _say("crit needs your GitLab address for this (asked once; saved in your settings).")
    for _ in range(3):
        try:
            answer = ask("GitLab address, for example https://gitlab.example.com (Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        if not answer:
            return ""
        try:
            url = clean_gitlab_url(answer)
        except ConfigError as exc:
            _say(f"  {exc}")
            continue
        try:
            data = read_config_dict(path)
            gitlab = data.get("gitlab") if isinstance(data.get("gitlab"), dict) else {}
            gitlab["base_url"] = url
            data["gitlab"] = gitlab
            write_config_file(path, data)
        except (ConfigError, OSError) as exc:
            _say(f"Could not save that: {exc}. Run: crit config --gitlab-url {url}")
            return url
        policy = egress.policy()
        if policy.configured:
            chat = f"https://{policy.chat_host}/" if policy.chat_host else ""
            egress.configure(policy.mode, chat, policy.allowed_hosts, gitlab_url=url, page_network=policy.page_network)
        _say(f"Saved. crit uses {url} for GitLab from now on.")
        return url
    return ""
