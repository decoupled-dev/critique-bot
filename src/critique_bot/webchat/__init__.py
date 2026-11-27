# SPDX-License-Identifier: Apache-2.0
"""The web-chat backend, as a removable module: crit driving a chat page in Edge.

Nothing outside this package imports it. Core crit reaches it only through
:mod:`critique_bot.model_api`: importing this package registers the ``browser`` backend
(:func:`model_api.register_backend`) and the hooks the command line uses
(:class:`WebChatHooks`: sign-in, ``crit setup``, blocked-site reporting). Deleting this
folder leaves crit working with local models; a config that names ``backend: browser``
then gets one message saying how to get it back.

Playwright is imported only when Edge is launched, so this package imports without it.
"""

from __future__ import annotations

from typing import Any

from critique_bot import model_api


class WebChatHooks:
    """What the command line may ask of the web chat, without importing it."""

    name = "webchat"

    @property
    def errors(self) -> tuple[type[BaseException], ...]:
        from critique_bot.webchat.browser import BrowserError

        return (BrowserError,)

    def needs_visible_login(self, user_data_dir: Any, storage_state: Any) -> bool:
        from critique_bot.webchat.browser import needs_visible_login

        return needs_visible_login(user_data_dir, storage_state)

    def login(self, config: Any) -> None:
        from critique_bot.webchat.provider import login_in_edge

        login_in_edge(config)

    def drain_blocked_hosts(self) -> list[str]:
        import sys

        browser = sys.modules.get("critique_bot.webchat.browser")
        drain = getattr(browser, "drain_blocked_hosts", None)
        return list(drain()) if callable(drain) else []

    def as_backend_error(self, exc: BaseException) -> Any:
        from critique_bot.webchat.browser import as_browser_error

        return as_browser_error(exc)

    def setup_command(self) -> tuple[Any, int]:
        """``crit setup``: (run_setup, default port)."""
        from critique_bot.webchat.setup_ui import DEFAULT_PORT, run_setup

        return run_setup, DEFAULT_PORT


def _open(config: Any, *, headed: bool = False) -> Any:
    from critique_bot.webchat.provider import open_browser_provider

    return open_browser_provider(config, headed=headed)


model_api.register_backend("browser", _open, hooks=WebChatHooks())
