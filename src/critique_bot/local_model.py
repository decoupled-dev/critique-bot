"""A model served on this PC: Ollama, llama.cpp, LM Studio, vLLM, or any OpenAI-compatible server.

This is the second backend behind :mod:`critique_bot.provider`. It offers the same
session as the browser backend, ``send(prompt) -> reply``, so the agent loop, the
worker, and the one-shot commands do not know which one they talk to. One session
is one conversation: the messages so far go with every request, and ``new_chat``
forgets them. Nothing leaves the PC: the server is reached over plain HTTP on
loopback, without a proxy (``config.py`` refuses any other host unless it is
listed in ``allowed_hosts``).

Two wire formats, chosen by ``local.api`` in ``config.json``:

- ``ollama``: ``POST /api/chat`` streaming NDJSON (Ollama).
- ``openai``: ``POST /v1/chat/completions`` streaming SSE (llama.cpp ``llama-server``,
  LM Studio, vLLM, and others).

Both stream, so a reply that stops arriving is noticed after ``reply_stall_ms``
instead of at the end of a long timeout.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import time
from typing import Any, Iterator
from urllib.parse import urlsplit

from critique_bot import log
from critique_bot.chat_client import COMPLETION_IDLE, COMPLETION_STOPPED, ChatError
from critique_bot.config import LOCAL_API_OLLAMA, LOCAL_API_OPENAI, BotConfig
from critique_bot.provider import ChatProvider, ChatSession

#: Ollama answers a chat request for a model it does not have with this.
_MODEL_MISSING_RE = re.compile(r"model\s+['\"]?[^'\"]+['\"]?\s+not found|no such model|not found, try pulling", re.I)
_CONTEXT_RE = re.compile(r"context (?:length|window|size)|too many tokens|maximum context|exceeds? the (?:model'?s )?context", re.I)
#: Reasoning models (qwen3, deepseek-r1, ...) may put their thinking in the reply itself.
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S | re.I)
_LEADING_THINK_RE = re.compile(r"\A\s*<think>.*\Z", re.S | re.I)
#: Context window asked from Ollama when the config does not say. Ollama's own default
#: (a few thousand tokens) is smaller than the agent's instructions.
DEFAULT_NUM_CTX = 32_768
#: How long the first chunk may take: the model may still be loading from disk.
_MIN_FIRST_CHUNK_SECONDS = 120.0
_CONNECT_SECONDS = 15.0
_PROBE_SECONDS = 10.0


class LocalModelError(ChatError):
    """The local model server did not complete a reply (a ChatError, so callers retry the same way)."""


def server_label(url: str) -> str:
    """``127.0.0.1:11434`` for a server URL, for the header and messages."""
    parts = urlsplit(url)
    host = parts.hostname or url
    return f"{host}:{parts.port}" if parts.port else host


def strip_thinking(text: str) -> str:
    """The reply without ``<think>...</think>`` blocks; an unclosed block at the start is dropped too."""
    if "<think" not in text.lower():
        return text
    cleaned = _THINK_RE.sub("", text)
    if _LEADING_THINK_RE.match(cleaned):
        return ""
    return cleaned


class LocalModelProvider(ChatProvider):
    """Process-level resources for a local model: none, beyond a check that the server answers."""

    can_parallelize = True

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self.client = LocalModelClient(config)
        self.models: list[str] = []

    @property
    def model(self) -> str:
        return self.client.model

    def __enter__(self) -> LocalModelProvider:
        self.models = self.client.list_models()
        if not self.client.model:
            if not self.models:
                raise LocalModelError(
                    f"no model is served at {self._config.url}. Pull one (for Ollama: "
                    "ollama pull qwen2.5-coder:7b) or set model in config.json."
                )
            self.client.model = self.models[0]
            log.info(f"local model: config has no model; using {self.client.model} from the server")
        elif self.models and not _served(self.client.model, self.models):
            log.warn(
                f"local model: {self.client.model} is not in the server's list "
                f"({', '.join(self.models[:6])}); the first request will say so if it is missing"
            )
        log.info(f"local model {self.client.model} at {self._config.url} ({self.client.api} API)")
        return self

    def session(self, *, isolated: bool = False, model: str | None = None) -> ChatSession:
        del isolated  # every session is its own conversation already
        client = self.client
        if model and model != client.model:
            client = LocalModelClient(self._config, model=model)
        return LocalModelSession(client)


def _served(model: str, models: list[str]) -> bool:
    wanted = model.lower()
    return any(name.lower() == wanted or name.lower() == wanted + ":latest" for name in models)


class LocalModelSession(ChatSession):
    """One conversation with the local model: the messages so far go with every request."""

    page: Any = None

    def __init__(self, client: LocalModelClient) -> None:
        self.client = client
        self.messages: list[dict[str, str]] = []
        self.last_detail: dict[str, Any] | None = None

    def send(self, prompt: str) -> str:
        from critique_bot.chat_client import redact_outgoing

        prompt = redact_outgoing(prompt)
        self.messages.append({"role": "user", "content": prompt})
        detail: dict[str, Any] = {"backend": "local", "model": self.client.model}
        try:
            reply = self.client.chat(self.messages, detail=detail)
        except BaseException:
            # A retry sends the same text again; it must not be in the history twice.
            self.messages.pop()
            self.last_detail = detail
            raise
        self.messages.append({"role": "assistant", "content": reply})
        self.last_detail = detail
        return reply

    def new_chat(self) -> bool:
        self.messages = []
        return True

    def stop_generation(self) -> bool:
        # Requests are synchronous: when send returns or raises, the server has stopped.
        return False

    def close(self) -> None:
        self.messages = []


class LocalModelClient:
    """HTTP calls to the server, one request per reply, streamed."""

    def __init__(self, config: BotConfig, *, model: str | None = None) -> None:
        self.url = config.url.rstrip("/")
        self.model = (model if model is not None else config.model) or ""
        self.api = config.local.api or LOCAL_API_OPENAI
        self.num_ctx = config.local.num_ctx or (DEFAULT_NUM_CTX if self.api == LOCAL_API_OLLAMA else 0)
        self.temperature = config.local.temperature
        self.keep_alive_seconds = config.local.keep_alive_seconds
        self.first_chunk_seconds = max(config.reply_start_ms / 1000.0, _MIN_FIRST_CHUNK_SECONDS)
        self.stall_seconds = max(config.reply_stall_ms / 1000.0, 1.0)
        self.max_seconds = max(config.reply_max_ms / 1000.0, self.stall_seconds)
        parts = urlsplit(self.url)
        self._https = parts.scheme == "https"
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or (443 if self._https else 80)
        self._prefix = parts.path.rstrip("/")

    # ------------------------------------------------------------------ requests

    def list_models(self) -> list[str]:
        """Names the server serves. An unreachable server is an error with the fix in it."""
        path = "/api/tags" if self.api == LOCAL_API_OLLAMA else "/v1/models"
        try:
            status, body = self._request("GET", path, None, timeout=_PROBE_SECONDS, stream=False)
        except OSError as exc:
            raise LocalModelError(self._unreachable(exc)) from exc
        if status != 200:
            raise LocalModelError(
                f"the model server at {self.url} answered {status} to {path}: "
                f"{_one_line(body.decode('utf-8', 'replace'), 160)}. Is local.api right ({self.api})?"
            )
        try:
            data = json.loads(body.decode("utf-8", "replace") or "{}")
        except ValueError:
            return []
        if self.api == LOCAL_API_OLLAMA:
            items = data.get("models") if isinstance(data, dict) else None
            return [str(item.get("name") or item.get("model") or "") for item in items or [] if isinstance(item, dict)]
        items = data.get("data") if isinstance(data, dict) else None
        return [str(item.get("id") or "") for item in items or [] if isinstance(item, dict)]

    def chat(self, messages: list[dict[str, str]], *, detail: dict[str, Any] | None = None) -> str:
        """One reply to ``messages``, streamed and joined. Raises LocalModelError when it fails."""
        detail = detail if detail is not None else {}
        path, body = self._chat_request(messages)
        started = time.monotonic()
        parts: list[str] = []
        try:
            stream = self._open("POST", path, body, timeout=self.first_chunk_seconds)
        except OSError as exc:
            raise LocalModelError(self._unreachable(exc)) from exc
        try:
            if stream.status != 200:
                raise LocalModelError(self._http_error(stream.status, stream.read_all()))
            for event in self._events(stream):
                text, done = self._chunk(event, detail)
                if text:
                    parts.append(text)
                if done:
                    break
                if time.monotonic() - started > self.max_seconds:
                    detail["completion"] = COMPLETION_IDLE
                    raise LocalModelError(
                        f"the reply from {self.model} did not finish within {self.max_seconds:.0f}s (reply_max_ms)"
                    )
            else:
                # The server closed the stream without a final event: whatever came is the reply.
                detail.setdefault("completion", COMPLETION_IDLE)
        except socket.timeout as exc:
            detail["completion"] = COMPLETION_IDLE
            if parts:
                raise LocalModelError(
                    f"the reply from {self.model} stopped arriving for {self.stall_seconds:.0f}s (reply_stall_ms)"
                ) from exc
            raise LocalModelError(
                f"{self.model} sent nothing for {self.first_chunk_seconds:.0f}s (reply_start_ms); "
                "a large model may need longer to load"
            ) from exc
        except OSError as exc:
            raise LocalModelError(f"the model server at {self.url} dropped the connection: {_one_line(str(exc), 120)}") from exc
        finally:
            stream.close()
        detail.setdefault("completion", COMPLETION_STOPPED)
        detail["seconds"] = round(time.monotonic() - started, 1)
        reply = strip_thinking("".join(parts))
        self._warn_if_truncated(detail)
        return reply

    # ------------------------------------------------------------------ wire formats

    def _chat_request(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
        if self.api == LOCAL_API_OLLAMA:
            options: dict[str, Any] = {}
            if self.num_ctx:
                options["num_ctx"] = self.num_ctx
            if self.temperature is not None:
                options["temperature"] = self.temperature
            body: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True}
            if options:
                body["options"] = options
            if self.keep_alive_seconds:
                body["keep_alive"] = f"{self.keep_alive_seconds}s"
            return "/api/chat", body
        body = {"model": self.model, "messages": messages, "stream": True}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        return "/v1/chat/completions", body

    def _events(self, stream: _Stream) -> Iterator[dict[str, Any]]:
        """Parsed JSON events: one per NDJSON line (Ollama) or ``data:`` line (SSE)."""
        first = True
        while True:
            line = stream.readline()
            if first:
                # From here on a pause between chunks is a stall, not a model still loading.
                stream.set_timeout(self.stall_seconds)
                first = False
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if not text or text.startswith(":"):
                continue
            if text.startswith("data:"):
                text = text[5:].strip()
            if text == "[DONE]":
                yield {"_done": True}
                return
            try:
                event = json.loads(text)
            except ValueError:
                log.debug(f"local model: skipping a line that is not JSON: {text[:80]}")
                continue
            if isinstance(event, dict):
                yield event

    def _chunk(self, event: dict[str, Any], detail: dict[str, Any]) -> tuple[str, bool]:
        """The text in one event and whether it ends the reply."""
        if event.get("_done"):
            return "", True
        error = event.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise LocalModelError(self._server_said(str(message or error)))
        if self.api == LOCAL_API_OLLAMA:
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            text = str(message.get("content") or "")
            done = bool(event.get("done"))
            if done:
                detail["completion"] = COMPLETION_STOPPED
                detail["done_reason"] = str(event.get("done_reason") or "stop")
                for key in ("prompt_eval_count", "eval_count"):
                    if isinstance(event.get(key), int):
                        detail[key] = event[key]
                if detail["done_reason"] == "length":
                    detail["completion"] = COMPLETION_IDLE
            return text, done
        choices = event.get("choices") or []
        text = ""
        done = False
        if choices and isinstance(choices[0], dict):
            choice = choices[0]
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            if not delta and isinstance(choice.get("message"), dict):  # a non-streaming answer
                delta = choice["message"]
                done = True
            text = str(delta.get("content") or "")
            reason = choice.get("finish_reason")
            if reason:
                detail["done_reason"] = str(reason)
                detail["completion"] = COMPLETION_IDLE if reason == "length" else COMPLETION_STOPPED
        usage = event.get("usage")
        if isinstance(usage, dict):
            if isinstance(usage.get("prompt_tokens"), int):
                detail["prompt_eval_count"] = usage["prompt_tokens"]
            if isinstance(usage.get("completion_tokens"), int):
                detail["eval_count"] = usage["completion_tokens"]
        return text, done

    def _warn_if_truncated(self, detail: dict[str, Any]) -> None:
        if detail.get("done_reason") == "length":
            log.warn(f"local model: {self.model} stopped at its output limit; the reply may be cut off")
        used = detail.get("prompt_eval_count")
        if self.num_ctx and isinstance(used, int) and used >= self.num_ctx - 64:
            log.warn(
                f"local model: the conversation fills the {self.num_ctx}-token context window "
                "(local.num_ctx); older messages are being dropped. Lower compact_after_chars in "
                ".bot/settings.json or raise num_ctx."
            )

    # ------------------------------------------------------------------ errors

    def _unreachable(self, exc: BaseException) -> str:
        hint = "is Ollama running (ollama serve)?" if self.api == LOCAL_API_OLLAMA else "is the model server running?"
        return f"no model server at {self.url}: {_one_line(str(exc) or type(exc).__name__, 100)}. {hint}"

    def _http_error(self, status: int, body: bytes) -> str:
        text = body.decode("utf-8", "replace")
        try:
            data = json.loads(text or "{}")
            error = data.get("error") if isinstance(data, dict) else None
            if isinstance(error, dict):
                text = str(error.get("message") or text)
            elif error:
                text = str(error)
        except ValueError:
            pass
        if status == 404 and self.api == LOCAL_API_OLLAMA and (_MODEL_MISSING_RE.search(text) or "model" in text.lower()):
            return f"Ollama has no model {self.model!r}: {_one_line(text, 100)}. Run: ollama pull {self.model}"
        if status == 404:
            return f"the model server at {self.url} has no {self._prefix}{'/v1/chat/completions' if self.api == LOCAL_API_OPENAI else '/api/chat'} (HTTP 404). Is local.api right ({self.api})?"
        return self._server_said(text, status=status)

    def _server_said(self, text: str, *, status: int | None = None) -> str:
        where = f"HTTP {status}" if status else "error"
        if _MODEL_MISSING_RE.search(text):
            return f"the model server has no model {self.model!r} ({where}): {_one_line(text, 120)}"
        if _CONTEXT_RE.search(text):
            return (
                f"the conversation no longer fits the context window of {self.model} ({where}): "
                f"{_one_line(text, 100)}. Lower compact_after_chars in .bot/settings.json or raise local.num_ctx."
            )
        return f"the model server said ({where}): {_one_line(text, 160)}"

    # ------------------------------------------------------------------ HTTP

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        # http.client never uses a proxy, so a loopback request stays on this PC even with
        # http_proxy set in the environment.
        if self._https:
            return http.client.HTTPSConnection(self._host, self._port, timeout=timeout)
        return http.client.HTTPConnection(self._host, self._port, timeout=timeout)

    def _open(self, method: str, path: str, body: dict[str, Any] | None, *, timeout: float) -> _Stream:
        conn = self._connection(_CONNECT_SECONDS)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/x-ndjson, text/event-stream, application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, self._prefix + path, body=payload, headers=headers)
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
            response = conn.getresponse()
        except BaseException:
            conn.close()
            raise
        return _Stream(conn, response)

    def _request(self, method: str, path: str, body: dict[str, Any] | None, *, timeout: float, stream: bool) -> tuple[int, bytes]:
        opened = self._open(method, path, body, timeout=timeout)
        try:
            return opened.status, opened.read_all()
        finally:
            opened.close()


class _Stream:
    """One open response: read it line by line, change its timeout, close it with its connection."""

    def __init__(self, conn: http.client.HTTPConnection, response: http.client.HTTPResponse) -> None:
        self._conn = conn
        self._response = response
        self.status = response.status

    def readline(self) -> bytes:
        return self._response.readline()

    def read_all(self) -> bytes:
        try:
            return self._response.read()
        except OSError:
            return b""

    def set_timeout(self, seconds: float) -> None:
        sock = getattr(getattr(self._response, "fp", None), "raw", None)
        sock = getattr(sock, "_sock", None)
        if sock is not None:
            try:
                sock.settimeout(seconds)
            except OSError:
                pass

    def close(self) -> None:
        for closer in (self._response.close, self._conn.close):
            try:
                closer()
            except Exception:
                pass


def _one_line(text: str, limit: int) -> str:
    compact = " ".join(str(text).split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"
