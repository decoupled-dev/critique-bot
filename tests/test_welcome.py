from __future__ import annotations

import unittest

from critique_bot.welcome import ask_login, pick_theme, render_welcome


class WelcomeTests(unittest.TestCase):
    def test_frame_shows_the_style_list_and_a_live_diff(self) -> None:
        frame = render_welcome(cursor=2, committed=1, syntax=True)
        self.assertIn("Welcome to crit", frame)
        self.assertIn("Let's get started.", frame)
        self.assertIn("To change this later, run /theme", frame)
        self.assertIn("> \033[1mLight mode\033[0m", frame)
        self.assertIn("✓ Dark mode", frame)
        self.assertIn("Hello, crit!", frame)
        self.assertIn("Syntax theme: GitHub", frame)
        plain = render_welcome(cursor=1, committed=1, syntax=False)
        self.assertIn("Syntax theme: off", plain)
        self.assertNotIn("✓", plain)

    def test_arrows_move_the_preview_and_enter_keeps_it(self) -> None:
        keys = iter(["down", "syntax", "enter"])
        chunks: list[str] = []
        chosen = pick_theme(read_key=lambda: next(keys), write=chunks.append)
        self.assertEqual(chosen, ("light", False))
        self.assertIn("Hello, crit!", "".join(chunks))
        self.assertIn("Syntax theme: off", chunks[-1])

    def test_escape_leaves_without_a_choice(self) -> None:
        self.assertIsNone(pick_theme(read_key=lambda: "cancel", write=lambda _text: None))

    def test_login_prompt_defaults_to_yes(self) -> None:
        self.assertTrue(ask_login(prompt=lambda _label: "", write=lambda _text: None))
        self.assertTrue(ask_login(prompt=lambda _label: "y", write=lambda _text: None))
        self.assertFalse(ask_login(prompt=lambda _label: "n", write=lambda _text: None))
        self.assertFalse(ask_login(prompt=lambda _label: "no", write=lambda _text: None))


class KeyDecodeTests(unittest.TestCase):
    def _decode(self, data: str, *, ready: bool = True) -> str:
        from critique_bot.welcome import _decode_key

        chars = iter(data)
        return _decode_key(lambda: next(chars), lambda: ready)

    def test_arrow_sequences_are_arrows_not_cancel(self) -> None:
        self.assertEqual(self._decode("\x1b[A"), "up")
        self.assertEqual(self._decode("\x1b[B"), "down")
        self.assertEqual(self._decode("\x1bOA"), "up")

    def test_lone_escape_cancels(self) -> None:
        self.assertEqual(self._decode("\x1b", ready=False), "cancel")

    def test_plain_keys(self) -> None:
        self.assertEqual(self._decode("\r"), "enter")
        self.assertEqual(self._decode("j"), "down")
        self.assertEqual(self._decode("\x14"), "syntax")

    @unittest.skipIf(__import__("os").name == "nt", "POSIX pty")
    def test_posix_reader_sees_a_whole_arrow_sequence(self) -> None:
        import os
        import pty
        import sys
        from unittest.mock import patch

        from critique_bot import welcome

        import threading
        import time

        master, slave = pty.openpty()
        threading.Thread(target=lambda: (time.sleep(0.2), os.write(master, b"\x1b[B")), daemon=True).start()
        try:
            with os.fdopen(os.dup(slave), "r") as fake_stdin, patch.object(sys, "stdin", fake_stdin):
                self.assertEqual(welcome._read_key_posix(), "down")
        finally:
            os.close(master)
            os.close(slave)

    def test_title_uses_the_crit_star(self) -> None:
        frame = render_welcome(cursor=1, committed=1, syntax=True)
        self.assertIn("✻", frame)
