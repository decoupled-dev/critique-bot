"""The engine package: history ownership, the Engine API and its request events."""

from __future__ import annotations

import unittest

from critique_bot.engine.chat import _Chat


class _ListSession:
    """A model API session in miniature: it appends the prompt and a reply to its list."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.seen: list[list[dict]] = []
        self.last_detail = None

    def send(self, prompt: str) -> str:
        self.seen.append([dict(item) for item in self.messages])
        self.messages.append({"role": "user", "content": prompt})
        reply = f"reply {len(self.seen)}"
        self.messages.append({"role": "assistant", "content": reply})
        return reply

    def history(self) -> list[dict]:
        return [dict(item) for item in self.messages]

    def replace_history(self, messages: list[dict]) -> None:
        self.messages = [dict(item) for item in messages]

    def new_chat(self) -> bool:
        self.messages = []
        return True


class EngineOwnsHistoryTest(unittest.TestCase):
    def test_the_engine_list_is_what_the_session_sends(self) -> None:
        session = _ListSession()
        chat = _Chat(session, retries=0)
        chat.send("one")
        chat.send("two")
        self.assertEqual([m["content"] for m in chat.messages], ["one", "reply 1", "two", "reply 2"])
        # Something outside the engine changes the session's list: the next request still
        # starts from the engine's.
        session.messages = [{"role": "user", "content": "stray"}]
        chat.send("three")
        self.assertEqual([m["content"] for m in session.seen[-1]], ["one", "reply 1", "two", "reply 2"])
        self.assertEqual(len(chat.messages), 6)

    def test_set_history_and_a_new_chat(self) -> None:
        session = _ListSession()
        chat = _Chat(session, retries=0)
        chat.send("one")
        chat.set_history([{"role": "user", "content": "summary"}])
        self.assertEqual(session.messages, [{"role": "user", "content": "summary"}])
        chat.send("two")
        self.assertEqual([m["content"] for m in chat.messages], ["summary", "two", "reply 2"])
        self.assertTrue(chat.restart("test"))
        self.assertEqual(chat.messages, [])

    def test_a_web_chat_keeps_its_own_conversation(self) -> None:
        class _Page:
            last_detail = None

            def send(self, prompt: str) -> str:
                return "ok"

        chat = _Chat(_Page(), retries=0)
        self.assertIsNone(chat.messages)
        self.assertEqual(chat.send("hi"), "ok")


if __name__ == "__main__":
    unittest.main()
