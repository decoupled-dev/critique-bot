"""A model served on this PC or the network: llama.cpp, LM Studio, vLLM, or any OpenAI-compatible server.

This is the second backend behind :mod:`critique_bot.provider`. It offers the same
session as the browser backend, ``send(prompt) -> reply``, so the agent loop, the
worker, and the one-shot commands do not know which one they talk to. One session
is one conversation: the messages so far go with every request, and ``new_chat``
forgets them. Nothing leaves the PC: the server is reached over plain HTTP on
loopback, without a proxy (``config.py`` refuses any other host unless it is
listed in ``allowed_hosts``).

One wire format: the OpenAI-compatible ``POST /v1/chat/completions``, streamed as
server-sent events (llama.cpp ``llama-server``, LM Studio, vLLM, and others serve it),
and ``GET /v1/models`` for the model list. A reply that stops arriving is noticed after
``reply_stall_ms`` instead of at the end of a long timeout.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import time
from typing import Any, Iterator
from urllib.parse import urlsplit

from critique_bot import log, run_stats
from critique_bot.model_api import COMPLETION_IDLE, COMPLETION_STOPPED, ChatError, ChatProvider, ChatSession
from critique_bot.config import BotConfig

#: How servers answer a chat request for a model they do not have.
_MODEL_MISSING_RE = re.compile(r"model\s+['\"]?[^'\"]+['\"]?\s+not found|no such model|not found, try pulling", re.I)
_CONTEXT_RE = re.compile(r"context (?:length|window|size)|too many tokens|maximum context|exceeds? the (?:model'?s )?context", re.I)
#: Reasoning models (qwen3, deepseek-r1, ...) may put their thinking in the reply itself.
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S | re.I)
_LEADING_THINK_RE = re.compile(r"\A\s*<think>.*\Z", re.S | re.I)
#: The context window crit assumes when ``local.num_ctx`` is not set: the size the server
#: should be started with (``llama-server -c 32768``). crit sizes its chats to it.
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
                    f"no model is served at {self._config.url}. Start the server with one "
                    "(llama-server -m model.gguf) or set model in config.json."
                )
            self.client.model = self.models[0]
            log.info(f"local model: config has no model; using {self.client.model} from the server")
        elif self.models and not _served(self.client.model, self.models):
            log.warn(
                f"local model: {self.client.model} is not in the server's list "
                f"({', '.join(self.models[:6])}); the first request will say so if it is missing"
            )
        log.info(f"local model {self.client.model} at {self._config.url}")
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


def _merge_tool_call(pending: dict[int, dict[str, Any]], piece: Any) -> None:
    """Add one streamed ``tool_calls`` delta to the calls being built (name once, arguments in pieces)."""
    if not isinstance(piece, dict):
        return
    index = piece.get("index")
    if not isinstance(index, int):
        index = len(pending)
    call = pending.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    if piece.get("id"):
        call["id"] = str(piece["id"])
    function = piece.get("function") if isinstance(piece.get("function"), dict) else {}
    if function.get("name"):
        call["function"]["name"] += str(function["name"]) if not call["function"]["name"] else ""
    arguments = function.get("arguments")
    if isinstance(arguments, dict):  # some servers send the object, not a string
        call["function"]["arguments"] = json.dumps(arguments, ensure_ascii=False)
    elif isinstance(arguments, str):
        call["function"]["arguments"] += arguments


def _result_text(output: Any) -> str:
    """A function_call_output payload as the text of a tool message."""
    if not isinstance(output, dict):
        return str(output)
    body = output.get("output")
    body = body if isinstance(body, str) else ("" if body is None else json.dumps(body, ensure_ascii=False))
    if output.get("ok"):
        return body or "(ok, no output)"
    error = str(output.get("error") or "tool failed")
    parts = [f"ERROR: {error}"]
    if body:
        parts.append(body)
    if output.get("allowed"):
        parts.append("allowed: " + ", ".join(map(str, output["allowed"])))
    return "\n".join(parts)


def split_results(prompt: str) -> tuple[list[dict[str, Any]], str]:
    """(function_call_output items, the rest) from a message the engine sends after running tools."""
    text = prompt.lstrip()
    if not text.startswith('{"input"'):
        return [], prompt
    try:
        value, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return [], prompt
    items = value.get("input") if isinstance(value, dict) else None
    if not isinstance(items, list):
        return [], prompt
    results = [item for item in items if isinstance(item, dict) and item.get("type") == "function_call_output"]
    return results, text[end:].strip()


def responses_text(content: str, calls: list[dict[str, Any]]) -> str:
    """A native reply as the Responses-format text the engine parses (message + function_call items)."""
    items: list[dict[str, Any]] = []
    if content.strip():
        items.append({"type": "message", "content": [{"type": "output_text", "text": content.strip()}]})
    for call in calls:
        function = call.get("function") or {}
        raw = function.get("arguments") or "{}"
        try:
            arguments: Any = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            arguments = raw  # the engine reports a call it cannot read
        items.append({"type": "function_call", "call_id": call.get("id") or "", "name": function.get("name") or "",
                      "arguments": arguments})
    return json.dumps({"output": items}, ensure_ascii=False)


class LocalModelSession(ChatSession):
    """One conversation with the local model: the messages so far go with every request."""

    page: Any = None

    def __init__(self, client: LocalModelClient) -> None:
        self.client = client
        self.messages: list[dict[str, Any]] = []
        self.last_detail: dict[str, Any] | None = None
        #: Native tool calling: the tools go to the server in ``tools`` (OpenAI chat format).
        self.tools: list[dict[str, Any]] | None = None
        self._call_count = 0

    def use_tools(self, tools: list[dict[str, Any]]) -> bool:
        """Call tools natively from now on. The engine keeps sending and parsing the Responses
        format; this session translates it to and from the server's ``tools`` / ``tool_calls``."""
        self.tools = list(tools)
        return True

    def send(self, prompt: str) -> str:
        from critique_bot.redaction import redact_outgoing

        prompt = redact_outgoing(prompt)
        if self.tools is not None:
            return self._send_native(prompt)
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
        run_stats.add_usage(detail.get("prompt_eval_count"), detail.get("eval_count"), detail.get("cached_count"))
        return reply

    def _send_native(self, prompt: str) -> str:
        from critique_bot import tool_parse

        before = len(self.messages)
        results, rest = split_results(prompt)
        pending = [c.get("id") for c in (self.messages[-1].get("tool_calls") or [])] if self.messages else []
        answered: set[str] = set()
        loose: list[str] = []
        for item in results:
            call_id = str(item.get("call_id") or "")
            text = _result_text(item.get("output"))
            if call_id in pending and call_id not in answered:
                self.messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
                answered.add(call_id)
            else:
                loose.append(f"result of {call_id or 'a call'}: {text}")
        for call_id in pending:  # every call needs an answer before the next user turn
            if call_id not in answered:
                self.messages.append({"role": "tool", "tool_call_id": call_id, "content": "(not run)"})
        user_text = "\n\n".join([*loose, rest] if rest else loose)
        if user_text or not results:
            self.messages.append({"role": "user", "content": user_text or prompt})
        detail: dict[str, Any] = {"backend": "local", "model": self.client.model, "tool_mode": "native"}
        try:
            content = self.client.chat(self.messages, detail=detail, tools=self.tools)
        except BaseException:
            del self.messages[before:]  # a retry sends the same text again
            self.last_detail = detail
            raise
        calls = list(detail.get("tool_calls") or [])
        if not calls:
            for source_text in (content, str(detail.get("reasoning") or "")):
                found, cleaned = tool_parse.recover_calls(source_text)
                if found:
                    calls = [{"id": "", "type": "function",
                              "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                             for c in found]
                    detail["parse_notes"] = [f"recovered {len(found)} call(s) from the {found[0].source} format"] + [
                        note for c in found for note in c.notes]
                    if source_text is content:
                        content = cleaned
                    break
        for call in calls:
            if not call.get("id"):
                self._call_count += 1
                call["id"] = f"call_{self._call_count}"
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = calls
        self.messages.append(message)
        self.last_detail = detail
        run_stats.add_usage(detail.get("prompt_eval_count"), detail.get("eval_count"), detail.get("cached_count"))
        if detail.get("parse_notes"):
            run_stats.count("recovered_calls", len(calls))
        if not calls:
            return content
        return responses_text(content, calls)

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
        #: The server's context window when the config says (used to warn when a chat fills it).
        self.num_ctx = config.local.num_ctx
        self.temperature = config.local.temperature
        self.max_tokens = config.local.max_tokens
        #: ``local.api_key`` (or CRIT_API_KEY): sent as a Bearer token, only to this server.
        self._api_key = config.local.api_key
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
        path = "/v1/models"
        try:
            status, body = self._request("GET", path, None, timeout=_PROBE_SECONDS, stream=False)
        except OSError as exc:
            raise LocalModelError(self._unreachable(exc)) from exc
        if status in (401, 403):
            raise LocalModelError(
                f"the model server at {self.url} refused crit ({status}): "
                + ("the API key was not accepted" if self._api_key else "it needs an API key")
                + ". Set it with: crit config --api-key"
            )
        if status != 200:
            raise LocalModelError(
                f"the model server at {self.url} answered {status} to {path}: "
                f"{_one_line(body.decode('utf-8', 'replace'), 160)}. Is it an OpenAI-compatible server?"
            )
        try:
            data = json.loads(body.decode("utf-8", "replace") or "{}")
        except ValueError:
            return []
        items = data.get("data") if isinstance(data, dict) else None
        return [str(item.get("id") or "") for item in items or [] if isinstance(item, dict)]

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        detail: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        """One reply to ``messages``, streamed and joined. Raises LocalModelError when it fails.

        With ``tools`` the request uses the server's native tool calling; the calls the
        server parsed land in ``detail["tool_calls"]`` (OpenAI chat format) and any
        reasoning text in ``detail["reasoning"]``.
        """
        detail = detail if detail is not None else {}
        path, body = self._chat_request(messages, tools)
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
        pending = detail.pop("_tool_calls", None)
        if pending:
            detail["tool_calls"] = [pending[index] for index in sorted(pending)]
        reasoning = detail.pop("_reasoning", None)
        if reasoning:
            detail["reasoning"] = "".join(reasoning)
        reply = strip_thinking("".join(parts))
        self._warn_if_truncated(detail)
        return reply

    # ------------------------------------------------------------------ wire formats

    def _chat_request(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> tuple[str, dict[str, Any]]:
        # include_usage: the last chunk carries token counts (vLLM, llama.cpp, LM Studio, Ollama), for crit eval.
        body: dict[str, Any] = {
            "model": self.model, "messages": messages, "stream": True, "stream_options": {"include_usage": True},
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return "/v1/chat/completions", body

    def _events(self, stream: _Stream) -> Iterator[dict[str, Any]]:
        """Parsed JSON events, one per ``data:`` line of the server-sent event stream."""
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
            thought = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(thought, str) and thought:
                detail.setdefault("_reasoning", []).append(thought)
            for piece in delta.get("tool_calls") or []:
                _merge_tool_call(detail.setdefault("_tool_calls", {}), piece)
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
            details = usage.get("prompt_tokens_details")
            if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
                detail["cached_count"] = details["cached_tokens"]  # vLLM, OpenAI-compatible servers
        timings = event.get("timings")
        if isinstance(timings, dict):  # llama.cpp: prompt_n tokens processed, cache_n reused from the cache
            if isinstance(timings.get("cache_n"), int):
                detail["cached_count"] = timings["cache_n"]
            if isinstance(timings.get("prompt_n"), int) and "prompt_eval_count" not in detail:
                detail["prompt_eval_count"] = timings["prompt_n"] + int(timings.get("cache_n") or 0)
            if isinstance(timings.get("predicted_n"), int) and "eval_count" not in detail:
                detail["eval_count"] = timings["predicted_n"]
        return text, done

    def _warn_if_truncated(self, detail: dict[str, Any]) -> None:
        if detail.get("done_reason") == "length":
            log.warn(f"local model: {self.model} stopped at its output limit; the reply may be cut off")
        used = detail.get("prompt_eval_count")
        if self.num_ctx and isinstance(used, int) and used >= self.num_ctx - 64:
            log.warn(
                f"local model: the conversation fills the {self.num_ctx}-token context window "
                "(local.num_ctx); older messages are being dropped. Lower compact_after_chars in "
                ".crit/settings.json or raise num_ctx."
            )

    # ------------------------------------------------------------------ errors

    def _unreachable(self, exc: BaseException) -> str:
        hint = "is the model server running (llama-server -m model.gguf --port ...)?"
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
        if status == 404 and _MODEL_MISSING_RE.search(text):
            return f"the model server has no model {self.model!r}: {_one_line(text, 100)}"
        if status == 404:
            return (
                f"the model server at {self.url} has no {self._prefix}/v1/chat/completions (HTTP 404). "
                "Is it an OpenAI-compatible server (llama-server, LM Studio, vLLM)?"
            )
        return self._server_said(text, status=status)

    def _server_said(self, text: str, *, status: int | None = None) -> str:
        where = f"HTTP {status}" if status else "error"
        if _MODEL_MISSING_RE.search(text):
            return f"the model server has no model {self.model!r} ({where}): {_one_line(text, 120)}"
        if _CONTEXT_RE.search(text):
            return (
                f"the conversation no longer fits the context window of {self.model} ({where}): "
                f"{_one_line(text, 100)}. Lower compact_after_chars in .crit/settings.json or raise local.num_ctx."
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
        if self._api_key:
            # The connection goes only to the configured server (self._host), never through a proxy.
            headers["Authorization"] = f"Bearer {self._api_key}"
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


# --------------------------------------------------------------------------- reachability


#: The answers a model server probe can have (see :func:`probe_server`).
PROBE_OK, PROBE_NO_MODELS, PROBE_REFUSED, PROBE_TIMEOUT, PROBE_UNKNOWN_HOST, PROBE_FAILED = (
    "ok", "no-models", "refused", "timeout", "unknown-host", "failed",
)
#: The server answers 401 or 403: it needs an API key (or a different one).
PROBE_NEEDS_KEY = "needs-key"
_PROBE_CONNECT_SECONDS = 3.0
_PROBE_HTTP_SECONDS = 5.0
_PROBE_PATHS = ("/v1/models", "/models", "/health")


class Probe:
    """What :func:`probe_server` found: ``outcome`` (one of PROBE_*), the models, one plain line."""

    def __init__(self, outcome: str, line: str, models: list[str] | None = None) -> None:
        self.outcome = outcome
        self.line = line
        self.models = list(models or [])

    @property
    def reachable(self) -> bool:
        return self.outcome in (PROBE_OK, PROBE_NO_MODELS)


def host_port(url: str) -> tuple[str, int, bool, str]:
    """``(host, port, https, path prefix)`` of a server address."""
    parts = urlsplit(url)
    https = parts.scheme == "https"
    return (parts.hostname or "", parts.port or (443 if https else 80), https, parts.path.rstrip("/"))


def _where(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _model_names(body: bytes) -> list[str]:
    try:
        data = json.loads(body.decode("utf-8", "replace") or "{}")
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    names: list[str] = []
    for key, fields in (("data", ("id",)), ("models", ("name", "model", "id"))):
        items = data.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                name = next((str(item[f]) for f in fields if item.get(f)), "")
                if name and name not in names:
                    names.append(name)
        if names:
            break
    return names


def _auth_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def probe_server(url: str, api_key: str | None = None) -> Probe:
    """Whether a model server answers at ``url``: a TCP connect (3 s), then ``GET /v1/models``
    (then ``/models``, ``/health``) with http.client, which never uses a proxy (5 s).
    ``api_key`` goes only to that server, as ``Authorization: Bearer``. A 401 or 403 is
    :data:`PROBE_NEEDS_KEY`."""
    host, port, https, prefix = host_port(url)
    where = _where(host, port)
    try:
        sock = socket.create_connection((host, port), timeout=_PROBE_CONNECT_SECONDS)
    except socket.gaierror:
        return Probe(PROBE_UNKNOWN_HOST, f"unknown host: {host}")
    except ConnectionRefusedError:
        return Probe(PROBE_REFUSED, f"refused: nothing is listening on {where}")
    except (socket.timeout, TimeoutError):
        return Probe(PROBE_TIMEOUT, f"timed out: {where} does not answer (firewall or wrong address?)")
    except OSError as exc:
        return Probe(PROBE_FAILED, f"could not connect to {where}: {_one_line(str(exc) or type(exc).__name__, 100)}")
    try:
        sock.close()
    except OSError:
        pass
    for path in _PROBE_PATHS:
        cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
        conn = cls(host, port, timeout=_PROBE_HTTP_SECONDS)
        try:
            conn.request("GET", prefix + path, headers=_auth_headers(api_key))
            response = conn.getresponse()
            status, body = response.status, response.read()
        except (OSError, http.client.HTTPException):
            continue
        finally:
            conn.close()
        if status in (401, 403):
            if api_key:
                return Probe(PROBE_NEEDS_KEY, f"the server refused the API key ({status})")
            return Probe(PROBE_NEEDS_KEY, "the server needs an API key")
        if status != 200:
            continue
        names = _model_names(body)
        if names:
            shown = ", ".join(names[:5]) + (", ..." if len(names) > 5 else "")
            return Probe(PROBE_OK, f"reachable, {len(names)} model{'s' if len(names) != 1 else ''}: {shown}", names)
    return Probe(PROBE_NO_MODELS, "reachable, but it does not list models (is it an OpenAI-compatible server?)")


def probe_site(url: str) -> Probe:
    """Whether a web page's host resolves and answers (a TCP connect, 3 s); nothing is sent."""
    host, port, _https, _prefix = host_port(url)
    where = _where(host, port)
    try:
        socket.getaddrinfo(host, port)
    except socket.gaierror:
        return Probe(PROBE_UNKNOWN_HOST, f"unknown host: {host}")
    try:
        socket.create_connection((host, port), timeout=_PROBE_CONNECT_SECONDS).close()
    except socket.gaierror:
        return Probe(PROBE_UNKNOWN_HOST, f"unknown host: {host}")
    except ConnectionRefusedError:
        return Probe(PROBE_REFUSED, f"refused: nothing is listening on {where}")
    except (socket.timeout, TimeoutError):
        return Probe(PROBE_TIMEOUT, f"timed out: {where} does not answer (firewall, proxy, or wrong address?)")
    except OSError as exc:
        return Probe(PROBE_FAILED, f"could not connect to {where}: {_one_line(str(exc) or type(exc).__name__, 100)}")
    return Probe(PROBE_OK, f"reachable: {host} resolves and answers on port {port}")


# --------------------------------------------------------------------------- context window

#: The context window each server reported (url -> tokens; 0 when it reports none), once per run.
_reported_ctx: dict[str, int] = {}


def _get_json(url: str, path: str, api_key: str | None) -> Any:
    host, port, https, prefix = host_port(url)
    cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
    conn = cls(host, port, timeout=_PROBE_HTTP_SECONDS)
    try:
        conn.request("GET", prefix + path, headers=_auth_headers(api_key))
        response = conn.getresponse()
        status, body = response.status, response.read()
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    if status != 200:
        return None
    try:
        return json.loads(body.decode("utf-8", "replace") or "null")
    except ValueError:
        return None


def _positive(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def server_context_window(url: str, model: str = "", api_key: str | None = None) -> int:
    """The context window the server reports, or 0: llama.cpp ``GET /props``
    (``default_generation_settings.n_ctx``), then vLLM ``GET /v1/models`` (``data[].max_model_len``,
    the configured model's or else the first). Asked once per server per run; never asks the person."""
    key = url.rstrip("/") + "|" + model
    if key in _reported_ctx:
        return _reported_ctx[key]
    found = 0
    props = _get_json(url, "/props", api_key)
    if isinstance(props, dict):
        settings = props.get("default_generation_settings")
        if isinstance(settings, dict):
            found = _positive(settings.get("n_ctx"))
        found = found or _positive(props.get("n_ctx"))
    if not found:
        models = _get_json(url, "/v1/models", api_key)
        items = models.get("data") if isinstance(models, dict) else None
        if isinstance(items, list):
            entries = [item for item in items if isinstance(item, dict)]
            chosen = [item for item in entries if model and str(item.get("id") or "") == model] or entries
            for item in chosen:
                found = _positive(item.get("max_model_len"))
                if found:
                    break
    if found:
        log.info(f"local model: the server reports a {found}-token context window")
    _reported_ctx[key] = found
    return found
