"""Browser chat UI backend.

Prompt composition, the job queue, and output writing stay above this layer.
The session is ``send(prompt) -> reply`` against a web chat page in Edge.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from typing import Any

from critique_bot.config import BotConfig


class ChatSession:
    """One conversation (one browser tab)."""

    page: Any = None
    #: How the last reply ended. Callers can distinguish a reply the UI
    #: declared finished from one that went quiet.
    last_detail: dict[str, Any] | None = None

    def send(self, prompt: str) -> str:
        raise NotImplementedError

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


def _job_config(config: BotConfig, model: str | None) -> BotConfig:
    if model:
        return replace(config, model=model)
    return config


class BrowserProvider(ChatProvider):
    def __init__(self, config: BotConfig, *, headed: bool) -> None:
        self._config = config
        self._headed = headed
        self._stack: ExitStack | None = None
        self._home = None
        self._cdp_url = config.cdp_url
        self.can_parallelize = False

    def __enter__(self) -> BrowserProvider:
        from critique_bot.browser import launch_edge

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
