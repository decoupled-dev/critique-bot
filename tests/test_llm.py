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


if __name__ == "__main__":
    unittest.main()
