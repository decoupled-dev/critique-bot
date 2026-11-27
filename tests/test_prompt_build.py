# SPDX-License-Identifier: Apache-2.0
"""Stage 3: memory files (hierarchical AGENTS.md and .crit/AGENT.md, the cap), the session context
in the first turn, change notes, and llama.cpp slot pinning."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from critique_bot import local_model, prompt_build
from critique_bot.local_model import LocalModelClient, LocalModelSession
from test_native_tools import _config


class MemoryFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.user = base / "home" / ".crit"
        self.user.mkdir(parents=True)
        self.repo = base / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.work = self.repo / "apps" / "car"
        (self.work / ".crit").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_files_load_from_the_user_then_the_repo_root_down_deeper_last(self) -> None:
        (self.user / "AGENT.md").write_text("user: prefer short answers", encoding="utf-8")
        (self.repo / "AGENTS.md").write_text("root: run make test", encoding="utf-8")
        (self.repo / "apps" / "AGENTS.md").write_text("apps: Kotlin only", encoding="utf-8")
        (self.work / "AGENTS.md").write_text("car: use the car API", encoding="utf-8")
        (self.work / ".crit" / "AGENT.md").write_text(
            "# template\n<!-- notes start -->\ncar notes: test with gradle", encoding="utf-8")
        memory = prompt_build.load_memory(self.work, user_dir=self.user)
        labels = [item.label for item in memory.files]
        self.assertEqual(labels[0].split()[0], "~/.crit/AGENT.md")
        self.assertEqual(labels[1:], ["AGENTS.md", "apps/AGENTS.md", "apps/car/AGENTS.md", "apps/car/.crit/AGENT.md"])
        text = memory.text
        self.assertLess(text.index("root: run"), text.index("car: use"))
        self.assertNotIn("# template", text)  # .crit/AGENT.md: only the notes after the marker
        self.assertEqual(memory.warning, "")

    def test_over_the_cap_the_least_specific_files_are_cut_with_a_warning(self) -> None:
        (self.user / "AGENT.md").write_text("u" * 20_000, encoding="utf-8")
        (self.work / "AGENTS.md").write_text("deep " * 4_000, encoding="utf-8")
        with mock.patch.object(prompt_build.log, "warn") as warned:
            memory = prompt_build.load_memory(self.work, user_dir=self.user)
        self.assertIn("over the 32 KiB cap", memory.warning)
        self.assertIn("~/.crit/AGENT.md", memory.warning)
        warned.assert_called_once()
        self.assertIn("deep " * 100, memory.text)  # the deeper file is whole
        self.assertLessEqual(len(memory.text.encode()), prompt_build.MEMORY_CAP + 2_000)
        self.assertIn("over the 32 KiB cap", memory.text)  # the model is told too

    def test_no_files_means_no_notes(self) -> None:
        self.assertEqual(prompt_build.load_memory(self.work, user_dir=self.user).text, "")

    def test_the_seed_puts_memory_files_after_environment(self) -> None:
        from critique_bot.agent_shell import Shell

        (self.repo / "AGENTS.md").write_text("root rule", encoding="utf-8")
        notes = prompt_build.load_memory(self.work, user_dir=self.user).text
        seed = prompt_build.seed_message(self.work, "INSTRUCTIONS", shell=Shell("bash", "bash", "bash"),
                                         platform_name="linux", notes=notes)
        self.assertLess(seed.index("INSTRUCTIONS"), seed.index("ENVIRONMENT"))
        self.assertLess(seed.index("ENVIRONMENT"), seed.index("PROJECT NOTES (memory files"))
        self.assertIn("=== AGENTS.md ===\nroot rule", seed)
        self.assertNotRegex(seed, r"\d{4}-\d{2}-\d{2}")  # no date in the prefix


class FirstTurnTests(unittest.TestCase):
    def test_session_context_has_the_date_and_git_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            import subprocess

            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=False, capture_output=True)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            text = prompt_build.session_context(root, now=datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc))
        self.assertTrue(text.startswith("SESSION\ndate: 2026-11-27"))
        if "git:" in text:  # git is on this machine
            self.assertIn("1 changed file (a.py)", text)

    def test_change_notes_are_short_and_labelled(self) -> None:
        self.assertEqual(prompt_build.change_note("mode", "auto."), "MODE CHANGED: auto.")
        self.assertTrue(prompt_build.change_note("role", "Test Engineer.").startswith("ROLE CHANGED: "))


class SlotPinningTests(unittest.TestCase):
    def setUp(self) -> None:
        local_model._reported_slots.clear()

    def tearDown(self) -> None:
        local_model._reported_slots.clear()

    def test_llama_cpp_gets_a_stable_slot_and_cache_prompt(self) -> None:
        with mock.patch.object(local_model, "_get_json", return_value={"total_slots": 4, "n_ctx": 8192}):
            first = LocalModelSession(LocalModelClient(_config("http://127.0.0.1:9")))
            slot = first.pin_slot("20261127T101500Z-1a2b")
            second = LocalModelSession(LocalModelClient(_config("http://127.0.0.1:9")))
            self.assertEqual(second.pin_slot("20261127T101500Z-1a2b"), slot)
        self.assertIn(slot, range(4))
        _path, body = first.client._chat_request([{"role": "user", "content": "hi"}])
        self.assertEqual(body["id_slot"], slot)
        self.assertTrue(body["cache_prompt"])

    def test_other_servers_get_no_slot(self) -> None:
        with mock.patch.object(local_model, "_get_json", return_value=None):
            session = LocalModelSession(LocalModelClient(_config("http://127.0.0.1:9")))
            self.assertIsNone(session.pin_slot("abc"))
        _path, body = session.client._chat_request([{"role": "user", "content": "hi"}])
        self.assertNotIn("id_slot", body)
        forced = LocalModelSession(LocalModelClient(_config("http://127.0.0.1:9")))
        self.assertIn(forced.pin_slot("abc", 2), (0, 1))  # a slot count from settings needs no detection

    def test_replace_history_keeps_call_ids_unique(self) -> None:
        session = LocalModelSession(LocalModelClient(_config("http://127.0.0.1:9")))
        session.replace_history([{"role": "assistant", "content": "", "tool_calls": [{"id": "call_7"}]}])
        self.assertEqual(session.history()[0]["tool_calls"][0]["id"], "call_7")
        self.assertEqual(session._call_count, 7)


if __name__ == "__main__":
    unittest.main()
