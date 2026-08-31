"""LLM backends: browser UI, Ollama, OpenAI, or any OpenAI-compatible HTTP API.

Prompt composition, the job queue, and output writing stay above this layer.
Each backend exposes the same session: ``send(prompt) -> reply``.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from contextlib import ExitStack
from dataclasses import replace
from typing import Any

from critique_bot import log
from critique_bot.config import (
    BACKEND_BROWSER,
    BACKEND_OLLAMA,
    BACKEND_OPENAI,
    BACKEND_OPENAI_COMPAT,
    BotConfig,
)


class LLMError(RuntimeError):
    """The LLM backend did not complete a reply."""


#: The chat UI told us generation had finished. The reply is whole.
COMPLETION_STOPPED = "stop-signal"
#: The reply merely stopped changing. It may have been cut off mid-answer.
COMPLETION_IDLE = "idle-timeout"


class LLMSession:
    """One conversation (one browser tab, or one HTTP message list)."""

    page: Any = None
    #: How the last reply ended. Browser sessions fill this in so callers can
    #: distinguish a reply the UI declared finished from one that went quiet.
    last_detail: dict[str, Any] | None = None

    def send(self, prompt: str) -> str:
        raise NotImplementedError

    def close(self) -> None:
        return None

    def __enter__(self) -> LLMSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class LLMProvider:
    """Process-level backend resources (Edge process, or nothing for HTTP)."""

    can_parallelize: bool = False

    def session(
        self,
        *,
        isolated: bool = False,
        model: str | None = None,
    ) -> LLMSession:
        raise NotImplementedError

    def close(self) -> None:
        return None

    def __enter__(self) -> LLMProvider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_provider(config: BotConfig, *, headed: bool = False) -> LLMProvider:
    if config.backend == BACKEND_BROWSER:
        return BrowserProvider(config, headed=headed)
    if headed:
        log.warn("--headed is ignored when backend is not browser")
    return HttpProvider(config)


def _job_config(config: BotConfig, model: str | None) -> BotConfig:
    if model:
        return replace(config, model=model)
    return config


class HttpProvider(LLMProvider):
    can_parallelize = True

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._client = HttpChatClient(config)

    def session(
        self,
        *,
        isolated: bool = False,
        model: str | None = None,
    ) -> LLMSession:
        del isolated
        cfg = _job_config(self._config, model)
        client = self._client if cfg.model == self._config.model else HttpChatClient(cfg)
        return HttpSession(client)


class HttpSession(LLMSession):
    def __init__(self, client: HttpChatClient) -> None:
        self._client = client
        self._messages: list[dict[str, str]] = []

    def send(self, prompt: str) -> str:
        self._messages.append({"role": "user", "content": prompt})
        with log.loading("Waiting for assistant..."):
            reply = self._client.complete(self._messages)
        self._messages.append({"role": "assistant", "content": reply})
        return reply


class HttpChatClient:
    def __init__(self, config: BotConfig) -> None:
        self.model = config.model
        self.base_url = config.base_url
        self.api_key = config.api_key
        self.timeout_s = max(config.timeout_ms / 1000.0, 1.0)
        self.backend = config.backend

    def complete(self, messages: list[dict[str, str]]) -> str:
        url = _completions_url(self.base_url)
        payload = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "stream": False,
            }
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        log.info(
            "llm request "
            + log.kv(
                backend=self.backend,
                url=url,
                model=self.model,
                messages=len(messages),
                prompt_chars=len(messages[-1].get("content", "")) if messages else 0,
            )
        )
        request = urllib.request.Request(
            url, data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            body = _read_http_body(exc)
            raise LLMError(_format_http_error(exc, body, url, self.backend)) from exc
        except urllib.error.URLError as exc:
            raise LLMError(_format_connect_error(exc, url, self.backend)) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LLMError(
                f"timed out after {self.timeout_s:.0f}s waiting for {url}"
            ) from exc

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMError(
                f"LLM at {url} returned non-JSON ({len(raw)} bytes): {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise LLMError(f"LLM at {url} returned a JSON {type(data).__name__}, not an object")
        if data.get("error"):
            raise LLMError(_format_api_error(data["error"], url, self.backend))
        reply = _choice_text(data)
        if not reply.strip():
            raise LLMError(f"LLM at {url} returned an empty assistant message")
        log.info(f"llm reply ({len(reply)} chars)")
        return reply


class BrowserProvider(LLMProvider):
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
    ) -> LLMSession:
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


class PageBrowserSession(LLMSession):
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
        if not self._close_page or self.page is None:
            return
        try:
            if not self.page.is_closed():
                self.page.close()
        except Exception as exc:
            log.debug(f"tab close: {exc}")


class CdpBrowserSession(LLMSession):
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


def _completions_url(base_url: str) -> str:
    cleaned = (base_url or "").rstrip("/")
    if not cleaned:
        raise LLMError("base_url is empty")
    if cleaned.endswith("/chat/completions"):
        return cleaned
    return cleaned + "/chat/completions"


def _choice_text(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError("LLM response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise LLMError("LLM response choice is not an object")
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"text", None}:
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts).strip()
    text = first.get("text")
    if isinstance(text, str):
        return text.strip()
    raise LLMError("LLM response has no assistant text")


def _read_http_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _format_http_error(
    exc: urllib.error.HTTPError, body: str, url: str, backend: str
) -> str:
    detail = _error_detail(body) or (body.strip()[:400] if body.strip() else "")
    suffix = f": {detail}" if detail else ""
    if exc.code == 404 and backend == BACKEND_OLLAMA:
        return (
            f"Ollama returned HTTP 404 for {url}{suffix}. "
            "Check `model` (`ollama list`) and that `ollama serve` is running."
        )
    if exc.code in {401, 403}:
        return (
            f"LLM at {url} returned HTTP {exc.code} (auth failed){suffix}. "
            "Set CRITIQUE_API_KEY or OPENAI_API_KEY."
        )
    return f"LLM at {url} returned HTTP {exc.code}{suffix}"


def _format_connect_error(exc: urllib.error.URLError, url: str, backend: str) -> str:
    reason = getattr(exc, "reason", exc)
    if backend == BACKEND_OLLAMA:
        return (
            f"Could not reach Ollama at {url} ({reason}). "
            "Start it with: ollama serve   (or: sudo systemctl start ollama)"
        )
    return f"Could not reach LLM at {url}: {reason}"


def _format_api_error(error: object, url: str, backend: str) -> str:
    detail = _error_detail(error) or str(error)
    if backend == BACKEND_OLLAMA and "not found" in detail.lower():
        return (
            f"Ollama model not found ({detail}). "
            "Set `model` to a name from `ollama list`."
        )
    return f"LLM at {url} error: {detail}"


def _error_detail(payload: object) -> str:
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text[:400]
        return _error_detail(parsed)
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:400]
            if isinstance(value, dict):
                nested = _error_detail(value)
                if nested:
                    return nested
    return ""
