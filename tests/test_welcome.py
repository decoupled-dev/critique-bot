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
