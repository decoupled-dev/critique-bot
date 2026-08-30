from __future__ import annotations

import json
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from critique_bot.config import (
    BACKEND_OLLAMA,
    BACKEND_OPENAI,
    BotConfig,
    Selectors,
)
from critique_bot.llm import (
    HttpChatClient,
    HttpProvider,
    LLMError,
    open_provider,
)


def _config(**overrides: object) -> BotConfig:
    values: dict[str, object] = {
        "url": "",
        "selectors": Selectors(prompt_input="", assistant_messages=""),
        "model": "llama3",
        "backend": BACKEND_OLLAMA,
        "base_url": "http://127.0.0.1:11434/v1",
        "timeout_ms": 5000,
    }
    values.update(overrides)
    return BotConfig(**values)  # type: ignore[arg-type]


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class HttpChatClientTests(unittest.TestCase):
    def test_complete_reads_assistant_text(self) -> None:
        client = HttpChatClient(_config())
        fake = _FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": " LGTM\n"}}]}
        )
        with patch("urllib.request.urlopen", return_value=fake) as opener:
            reply = client.complete([{"role": "user", "content": "review this"}])
        self.assertEqual(reply, "LGTM")
        request = opener.call_args[0][0]
        self.assertEqual(
            request.full_url, "http://127.0.0.1:11434/v1/chat/completions"
        )
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "llama3")
        self.assertEqual(body["stream"], False)

    def test_session_keeps_conversation_history(self) -> None:
        provider = HttpProvider(_config())
        replies = iter(["one", "two"])

        def fake_open(request, timeout=None):
            del timeout
            payload = json.loads(request.data.decode("utf-8"))
            text = next(replies)
            self.assertEqual(len(payload["messages"]), 1 if text == "one" else 3)
            return _FakeResponse(
                {"choices": [{"message": {"content": text}}]}
            )

        with patch("urllib.request.urlopen", side_effect=fake_open):
            with provider.session() as session:
                first = session.send("hi")
                second = session.send("again")
        self.assertEqual(first, "one")
        self.assertEqual(second, "two")

    def test_http_error_mentions_ollama(self) -> None:
        client = HttpChatClient(_config())
        error = HTTPError(
            "http://127.0.0.1:11434/v1/chat/completions",
            404,
            "Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(b'{"error": "model \\"nope\\" not found"}'),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(LLMError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("Ollama", str(ctx.exception))
        self.assertIn("404", str(ctx.exception))

    def test_connect_error_tells_user_to_start_ollama(self) -> None:
        client = HttpChatClient(_config())
        with patch(
            "urllib.request.urlopen",
            side_effect=URLError(ConnectionRefusedError("refused")),
        ):
            with self.assertRaises(LLMError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("ollama serve", str(ctx.exception).lower())

    def test_openai_auth_error(self) -> None:
        client = HttpChatClient(
            _config(
                backend=BACKEND_OPENAI,
                model="gpt-4o",
                base_url="https://api.openai.com/v1",
                api_key="sk-test",
            )
        )
        error = HTTPError(
            "https://api.openai.com/v1/chat/completions",
            401,
            "Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(b'{"error": {"message": "invalid api key"}}'),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(LLMError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("401", str(ctx.exception))
        self.assertIn("CRITIQUE_API_KEY", str(ctx.exception))

    def test_open_provider_http_for_ollama(self) -> None:
        provider = open_provider(_config(), headed=True)
        self.assertIsInstance(provider, HttpProvider)
        self.assertTrue(provider.can_parallelize)

    def test_authorization_header_when_key_set(self) -> None:
        client = HttpChatClient(_config(api_key="sk-secret"))
        fake = _FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        with patch("urllib.request.urlopen", return_value=fake) as opener:
            client.complete([{"role": "user", "content": "hi"}])
        headers = opener.call_args[0][0].headers
        self.assertEqual(headers.get("Authorization"), "Bearer sk-secret")

    def test_empty_assistant_message(self) -> None:
        client = HttpChatClient(_config())
        fake = _FakeResponse({"choices": [{"message": {"content": "  "}}]})
        with patch("urllib.request.urlopen", return_value=fake):
            with self.assertRaises(LLMError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("empty", str(ctx.exception))

    def test_non_json_body(self) -> None:
        client = HttpChatClient(_config())

        class Raw:
            def read(self) -> bytes:
                return b"not-json"

            def __enter__(self) -> Raw:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        with patch("urllib.request.urlopen", return_value=Raw()):
            with self.assertRaises(LLMError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("non-JSON", str(ctx.exception))

    def test_json_array_rejected(self) -> None:
        client = HttpChatClient(_config())

        class Raw:
            def read(self) -> bytes:
                return b"[1, 2]"

            def __enter__(self) -> Raw:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        with patch("urllib.request.urlopen", return_value=Raw()):
            with self.assertRaises(LLMError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("not an object", str(ctx.exception))

    def test_api_error_object(self) -> None:
        client = HttpChatClient(_config())
        fake = _FakeResponse({"error": {"message": "model 'nope' not found"}})
        with patch("urllib.request.urlopen", return_value=fake):
            with self.assertRaises(LLMError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("Ollama model not found", str(ctx.exception))

    def test_content_parts_list(self) -> None:
        client = HttpChatClient(_config())
        fake = _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "Hello "},
                                "world",
                                {"type": "image", "text": "ignored"},
                            ]
                        }
                    }
                ]
            }
        )
        with patch("urllib.request.urlopen", return_value=fake):
            self.assertEqual(client.complete([{"role": "user", "content": "hi"}]), "Hello world")

    def test_legacy_text_field(self) -> None:
        client = HttpChatClient(_config())
        fake = _FakeResponse({"choices": [{"text": "  legacy  "}]})
        with patch("urllib.request.urlopen", return_value=fake):
            self.assertEqual(client.complete([{"role": "user", "content": "hi"}]), "legacy")

    def test_no_choices(self) -> None:
        client = HttpChatClient(_config())
        fake = _FakeResponse({"choices": []})
        with patch("urllib.request.urlopen", return_value=fake):
            with self.assertRaises(LLMError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("no choices", str(ctx.exception))

    def test_timeout(self) -> None:
        client = HttpChatClient(_config(timeout_ms=1000))
        with patch("urllib.request.urlopen", side_effect=TimeoutError("late")):
            with self.assertRaises(LLMError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("timed out", str(ctx.exception))

    def test_empty_base_url(self) -> None:
        client = HttpChatClient(_config(base_url=""))
        with self.assertRaises(LLMError) as ctx:
            client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("base_url is empty", str(ctx.exception))

    def test_completions_url_already_complete(self) -> None:
        client = HttpChatClient(
            _config(base_url="http://h/v1/chat/completions")
        )
        fake = _FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        with patch("urllib.request.urlopen", return_value=fake) as opener:
            client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(
            opener.call_args[0][0].full_url, "http://h/v1/chat/completions"
        )

    def test_http_403(self) -> None:
        client = HttpChatClient(
            _config(backend=BACKEND_OPENAI, model="gpt-4o", api_key="sk")
        )
        error = HTTPError(
            "https://api.openai.com/v1/chat/completions",
            403,
            "Forbidden",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(b'{"error":{"message":"nope"}}'),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(LLMError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("403", str(ctx.exception))
        self.assertIn("auth failed", str(ctx.exception))

    def test_generic_http_error(self) -> None:
        client = HttpChatClient(_config())
        error = HTTPError(
            "http://127.0.0.1:11434/v1/chat/completions",
            500,
            "Boom",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(b"internal"),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(LLMError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_openai_connect_error(self) -> None:
        client = HttpChatClient(
            _config(
                backend=BACKEND_OPENAI,
                model="gpt-4o",
                base_url="https://api.openai.com/v1",
                api_key="sk",
            )
        )
        with patch(
            "urllib.request.urlopen",
            side_effect=URLError("dns"),
        ):
            with self.assertRaises(LLMError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("Could not reach LLM", str(ctx.exception))

    def test_job_config_replace(self) -> None:
        from critique_bot.llm import _job_config

        cfg = _config(model="llama3")
        self.assertEqual(_job_config(cfg, None).model, "llama3")
        self.assertEqual(_job_config(cfg, "mistral").model, "mistral")

    def test_session_base_not_implemented(self) -> None:
        from critique_bot.llm import LLMSession

        with self.assertRaises(NotImplementedError):
            LLMSession().send("x")

    def test_open_provider_browser(self) -> None:
        from critique_bot.config import BACKEND_BROWSER
        from critique_bot.llm import BrowserProvider

        provider = open_provider(
            _config(
                backend=BACKEND_BROWSER,
                url="https://chat.example",
                selectors=Selectors(prompt_input="t", assistant_messages=".a"),
            )
        )
        self.assertIsInstance(provider, BrowserProvider)

    def test_browser_session_requires_start(self) -> None:
        from critique_bot.browser import BrowserError
        from critique_bot.config import BACKEND_BROWSER
        from critique_bot.llm import BrowserProvider

        provider = BrowserProvider(
            _config(
                backend=BACKEND_BROWSER,
                url="https://chat.example",
                selectors=Selectors(prompt_input="t", assistant_messages=".a"),
            ),
            headed=False,
        )
        with self.assertRaises(BrowserError):
            provider.session()
        with self.assertRaises(BrowserError):
            provider.session(isolated=True)

    def test_http_session_uses_alternate_model_client(self) -> None:
        provider = HttpProvider(_config(model="llama3"))
        fake = _FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        with patch("urllib.request.urlopen", return_value=fake) as opener:
            with provider.session(model="mistral") as session:
                session.send("hi")
        body = json.loads(opener.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(body["model"], "mistral")

    def test_error_detail_nested_json_string(self) -> None:
        from critique_bot.llm import _error_detail

        self.assertEqual(_error_detail(""), "")
        self.assertEqual(_error_detail("plain"), "plain")
        self.assertEqual(
            _error_detail('{"error": {"message": "nested"}}'),
            "nested",
        )
        self.assertEqual(_error_detail({"detail": "d"}), "d")


if __name__ == "__main__":
    unittest.main()
