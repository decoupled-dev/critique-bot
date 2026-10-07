"""Browser chat UI backend.

Prompt composition, the job queue, and output writing stay above this layer.
The session is ``send(prompt) -> reply`` against a web chat page in Edge.
"""

from __future__ import annotations

import re
import urllib.parse
from contextlib import ExitStack
from dataclasses import replace
from typing import Any

from critique_bot import log
from critique_bot.config import BotConfig

#: How long the first-login window stays open. Longer than a normal turn so
#: SSO and a second factor can finish before the window closes.
_LOGIN_WINDOW_MS = 600_000


class ChatSession:
    """One conversation (one browser tab)."""

    page: Any = None
    #: How the last reply ended. Callers can distinguish a reply the UI
    #: declared finished from one that went quiet.
    last_detail: dict[str, Any] | None = None

    def send(self, prompt: str) -> str:
        raise NotImplementedError

    def new_chat(self) -> bool:
        """Start a fresh conversation for the next send. False when this session cannot."""
        return False

    def stop_generation(self) -> bool:
        """Stop a reply the page is still writing. True when one was running."""
        return False

    def close(self) -> None:
        return None

    def __enter__(self) -> ChatSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class ChatProvider:
    """Process-level Edge resources."""

    can_parallelize: bool = False

    def session(
        self,
        *,
        isolated: bool = False,
        model: str | None = None,
    ) -> ChatSession:
        raise NotImplementedError

    def close(self) -> None:
        return None

    def __enter__(self) -> ChatProvider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_provider(config: BotConfig, *, headed: bool = False) -> ChatProvider:
    return BrowserProvider(config, headed=headed)


def login_in_edge(config: BotConfig) -> None:
    """Open a visible Edge window and wait until the chat box shows a signed-in session."""
    from critique_bot.browser import launch_edge, wait_until_signed_in

    log.info(
        "no saved Edge login; opening a visible window. "
        "It closes after you sign in, then this run continues headless."
    )
    with launch_edge(
        headed=True,
        storage_state=config.storage_state,
        user_data_dir=config.user_data_dir,
        start_url=config.url,
        timeout_ms=config.timeout_ms,
    ) as page:
        wait_until_signed_in(
            page,
            prompt_selector=config.selectors.prompt_input,
            timeout_ms=max(config.timeout_ms, _LOGIN_WINDOW_MS),
        )
    log.info("closed the login window; continuing headless")


def _job_config(config: BotConfig, model: str | None) -> BotConfig:
    if model:
        return replace(config, model=model)
    return config


_CONVERSATION_RE = re.compile(r"^(.*?)/c/[^/]+/?$")


def fresh_chat_url(url: str) -> str:
    """The URL of an empty chat for ``url``: a conversation link loses its ``/c/<id>``.

    ``https://chatgpt.com/c/abc`` -> ``https://chatgpt.com/``;
    ``https://chatgpt.com/g/g-x/c/abc`` -> ``https://chatgpt.com/g/g-x`` (the GPT or project stays).
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    match = _CONVERSATION_RE.match(parts.path)
    if not match:
        return url
    path = match.group(1) or "/"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _open_new_chat(page: Any, config: BotConfig) -> bool:
    """Load the configured chat URL as an empty conversation (never a saved ``/c/<id>`` one).

    The next send runs ``prepare_chat`` again, so the model is selected anew.
    """
    if page is None:
        return False
    url = fresh_chat_url(config.url)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=config.timeout_ms)
    except Exception as exc:
        log.warn(f"could not open a new chat at {url}: {exc}")
        return False
    return True


def _stop_page_generation(page: Any, config: BotConfig) -> bool:
    if page is None:
        return False
    from critique_bot.chat_client import stop_generation

    return stop_generation(page, config.selectors)


class BrowserProvider(ChatProvider):
    def __init__(self, config: BotConfig, *, headed: bool) -> None:
        self._config = config
        self._headed = headed
        self._stack: ExitStack | None = None
        self._home = None
        self._cdp_url = config.cdp_url
        self.can_parallelize = False

    def __enter__(self) -> BrowserProvider:
        from critique_bot.browser import launch_edge, needs_visible_login

        signed_in_visibly = False
        if (
            not self._headed
            and not self._config.cdp_url
            and needs_visible_login(
                self._config.user_data_dir,
                self._config.storage_state,
            )
        ):
            login_in_edge(self._config)
            signed_in_visibly = True

        self._stack = ExitStack()
        cdp_out: dict[str, str] = {}
        self._home = self._stack.enter_context(
            launch_edge(
                headed=self._headed,
                storage_state=self._config.storage_state,
                user_data_dir=self._config.user_data_dir,
                cdp_url=self._config.cdp_url,
                start_url=self._config.url,
                timeout_ms=self._config.timeout_ms,
                cdp_out=cdp_out if self._config.max_parallel_tabs > 1 else None,
                promote_missing_profile=not signed_in_visibly,
            )
        )
        self._cdp_url = cdp_out.get("url") or self._config.cdp_url
        self.can_parallelize = bool(self._cdp_url)
        return self

    def close(self) -> None:
        if self._stack is not None:
            self._stack.close()
            self._stack = None
        self._home = None

    def session(
        self,
        *,
        isolated: bool = False,
        model: str | None = None,
    ) -> ChatSession:
        cfg = _job_config(self._config, model)
        from critique_bot.browser import BrowserError, as_browser_error

        if isolated:
            if not self._cdp_url:
                raise BrowserError(
                    "parallel review tabs need Edge remote debugging (cdp_url)"
                )
            return CdpBrowserSession(self._cdp_url, cfg)
        if self._home is None:
            raise BrowserError("browser provider is not started")
        try:
            home = self._home
            if getattr(home, "is_closed", lambda: False)():
                raise BrowserError("Edge home tab has been closed")
            page = home.context.new_page()
        except BrowserError:
            raise
        except Exception as exc:
            closed = as_browser_error(exc)
            if closed is not None:
                raise closed from exc
            raise BrowserError(f"could not open a review tab: {exc}") from exc
        return PageBrowserSession(page, cfg, close_page=True)


class PageBrowserSession(ChatSession):
    def __init__(self, page: Any, config: BotConfig, *, close_page: bool) -> None:
        self.page = page
        self._config = config
        self._close_page = close_page
        self._prepared = False
        self.last_detail: dict[str, Any] | None = None

    def send(self, prompt: str) -> str:
        from critique_bot.chat_client import prepare_chat, send_turn

        if not self._prepared:
            prepare_chat(self.page, self._config)
            self._prepared = True
        detail: dict[str, Any] = {}
        try:
            return send_turn(self.page, self._config, prompt, detail=detail)
        finally:
            self.last_detail = detail

    def new_chat(self) -> bool:
        if not _open_new_chat(self.page, self._config):
            return False
        self._prepared = False
        return True

    def stop_generation(self) -> bool:
        return _stop_page_generation(self.page, self._config)

    def close(self) -> None:
        from critique_bot import log

        if not self._close_page or self.page is None:
            return
        try:
            if not self.page.is_closed():
                self.page.close()
        except Exception as exc:
            log.debug(f"tab close: {exc}")


class CdpBrowserSession(ChatSession):
    def __init__(self, cdp_url: str, config: BotConfig) -> None:
        self._cdp_url = cdp_url
        self._config = config
        self._cm: Any = None
        self.page = None
        self._prepared = False
        self.last_detail: dict[str, Any] | None = None

    def __enter__(self) -> CdpBrowserSession:
        from critique_bot.browser import connect_job_page

        self._cm = connect_job_page(self._cdp_url)
        self.page = self._cm.__enter__()
        return self

    def close(self) -> None:
        if self._cm is None:
            return
        try:
            self._cm.__exit__(None, None, None)
        finally:
            self._cm = None
            self.page = None

    def new_chat(self) -> bool:
        if not _open_new_chat(self.page, self._config):
            return False
        self._prepared = False
        return True

    def stop_generation(self) -> bool:
        return _stop_page_generation(self.page, self._config)

    def send(self, prompt: str) -> str:
        from critique_bot.chat_client import prepare_chat, send_turn

        if self.page is None:
            from critique_bot.browser import BrowserError

            raise BrowserError("browser session is not started")
        if not self._prepared:
            prepare_chat(self.page, self._config)
            self._prepared = True
        detail: dict[str, Any] = {}
        try:
            return send_turn(self.page, self._config, prompt, detail=detail)
        finally:
            self.last_detail = detail
