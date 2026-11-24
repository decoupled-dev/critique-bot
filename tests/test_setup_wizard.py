"""First-run questions: cloud chat page or local model, and the config they write."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from critique_bot.config import load_config
from critique_bot.model_api import ChatError
from critique_bot.redaction import redact_outgoing, set_custom_terms
from critique_bot.setup_wizard import _clean_host, run_wizard


class _Script:
    """Answers questions in order and records what was said."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.said: list[str] = []
        self.asked: list[str] = []

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected question: {prompt}")
        answer = self.answers.pop(0)
        if answer == "^C":
            raise KeyboardInterrupt
        return answer

    def say(self, text: str) -> None:
        self.said.append(text)


class WizardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "conf" / "config.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()
        from critique_bot import egress

        egress._policy = egress.Policy()  # load_config set the process-wide policy

    def _run(self, script: _Script, models=None):
        def listing(config):
            if isinstance(models, Exception):
                raise models
            return list(models or [])

        return run_wizard(self.path, ask=script.ask, say=script.say, list_models=listing)

    def test_cloud_needs_https_and_writes_a_loadable_config(self) -> None:
        script = _Script("1", "http://chat.corp.example/", "https://chat.corp.example/", "")
        result = self._run(script)
        assert result is not None
        self.assertEqual((result.backend, result.needs_selectors), ("browser", True))
        self.assertTrue(any("https://" in line for line in script.said))
        self.assertTrue(any("only chat.corp.example" in line for line in script.said))
        self.assertTrue(any(line.startswith("Personal data") for line in script.said))
        config = load_config(self.path)
        self.assertEqual(config.url, "https://chat.corp.example/")
        self.assertFalse(config.is_local)
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_local_on_this_pc_picks_a_model_from_the_server(self) -> None:
        result = self._run(_Script("2", "y", "", "2", ""), models=["qwen3:27b", "gemma3:12b"])
        assert result is not None
        config = load_config(self.path)
        self.assertTrue(config.is_local)
        self.assertEqual(config.url, "http://127.0.0.1:8080")
        self.assertEqual(config.model, "gemma3:12b")
        self.assertEqual(config.allowed_hosts, ())
        self.assertEqual(json.loads(self.path.read_text())["local"]["num_ctx"], 32768)

    def test_local_on_another_pc_is_allowed_only_after_a_yes(self) -> None:
        script = _Script("2", "n", "192.168.1.50", "8080", "y", "1", "")
        result = self._run(script, models=["qwen3-27b"])
        assert result is not None
        config = load_config(self.path)
        self.assertEqual(config.url, "http://192.168.1.50:8080")
        self.assertEqual(config.allowed_hosts, ("192.168.1.50",))
        self.assertEqual(json.loads(self.path.read_text())["local"]["api"], "openai")
        self.assertTrue(any("not encrypted" in line for line in script.said))
        self.assertTrue(any("ssh -N -L 8080:127.0.0.1:8080" in line for line in script.said))

    def test_declining_another_pc_writes_nothing(self) -> None:
        result = self._run(_Script("2", "n", "gpu-box.corp.example", "", "n"))
        self.assertIsNone(result)
        self.assertFalse(self.path.exists())

    def test_unreachable_server_falls_back_to_a_typed_model_name(self) -> None:
        script = _Script("2", "y", "", "n", "qwen3:27b", "")
        result = self._run(script, models=ChatError("connection refused"))
        assert result is not None
        self.assertEqual(load_config(self.path).model, "qwen3:27b")
        self.assertTrue(any("connection refused" in line for line in script.said))

    def test_ctrl_c_stops_without_writing(self) -> None:
        self.assertIsNone(self._run(_Script("1", "^C")))
        self.assertFalse(self.path.exists())

    def test_names_to_hide_are_saved_and_registered(self) -> None:
        script = _Script("1", "https://chat.corp.example/", "Globex, ab", "Acme Corp, Globex, acme corp")
        result = self._run(script)
        assert result is not None
        self.assertTrue(any("too short" in line for line in script.said))
        self.assertEqual(json.loads(self.path.read_text())["redact_terms"], ["Acme Corp", "Globex"])
        config = load_config(self.path)
        self.assertEqual(config.redact_terms, ("Acme Corp", "Globex"))
        self.assertEqual(redact_outgoing("AcmeCorpClient for globex"), "[REDACTED:name]Client for [REDACTED:name]")
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        set_custom_terms(())

    def test_ctrl_c_at_the_names_question_keeps_the_config(self) -> None:
        result = self._run(_Script("1", "https://chat.corp.example/", "^C"))
        self.assertIsNotNone(result)
        self.assertNotIn("redact_terms", json.loads(self.path.read_text()))

    def test_default_path_is_the_user_config(self) -> None:
        from critique_bot.config import user_config_path
        from critique_bot.setup_wizard import default_config_path

        self.assertEqual(default_config_path(), user_config_path())

    def test_host_answers(self) -> None:
        self.assertEqual(_clean_host("192.168.1.50"), "192.168.1.50")
        self.assertEqual(_clean_host("http://GPU-Box.corp.example:11434/"), "gpu-box.corp.example")
        self.assertEqual(_clean_host("fe80::1"), "fe80::1")
        self.assertIsNone(_clean_host("bad host!"))
        self.assertIsNone(_clean_host("gpu-box:11434"))


if __name__ == "__main__":
    unittest.main()
