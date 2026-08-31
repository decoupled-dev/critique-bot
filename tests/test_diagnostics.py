"""Structured setup checks behind `doctor` and the setup UI."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from critique_bot import diagnostics
from critique_bot.config import BACKEND_OLLAMA, BACKEND_OPENAI, BotConfig, Selectors
from critique_bot.diagnostics import FAIL, OK, SKIP, WARN, Check, Report


def _browser_config(**overrides: object) -> BotConfig:
    values: dict[str, object] = {
        "url": "https://chat.example/",
        "selectors": Selectors(
            prompt_input="#p",
            assistant_messages=".a",
            send_button="button.send",
        ),
        "model": "GPT-5.1",
        "queue_dir": "",
    }
    values.update(overrides)
    return BotConfig(**values)  # type: ignore[arg-type]


class ReportTests(unittest.TestCase):
    def test_warnings_do_not_fail_a_report(self) -> None:
        report = Report()
        report.add(Check("a", OK, "fine"))
        report.add(Check("b", WARN, "hmm"))
        report.add(Check("c", SKIP, "later"))
        self.assertTrue(report.ok)
        self.assertEqual(len(report.warnings), 1)
        self.assertEqual(report.failures, [])

    def test_a_single_failure_fails_the_report(self) -> None:
        report = Report()
        report.add(Check("a", OK, "fine"))
        report.add(Check("b", FAIL, "broken"))
        self.assertFalse(report.ok)
        self.assertEqual([check.name for check in report.failures], ["b"])

    def test_json_round_trips(self) -> None:
        report = Report()
        report.add(Check("a", FAIL, "broken", "try this"))
        parsed = json.loads(diagnostics.render_json(report))
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["checks"][0]["hint"], "try this")

    def test_text_render_includes_hints_for_problems_only(self) -> None:
        report = Report()
        report.add(Check("good", OK, "fine", "unused hint"))
        report.add(Check("bad", FAIL, "broken", "do the thing"))
        text = diagnostics.render_text(report)
        self.assertIn("[PASS] good", text)
        self.assertIn("[FAIL] bad", text)
        self.assertIn("do the thing", text)
        self.assertNotIn("unused hint", text)


class StaticCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_stop_button_is_a_warning_not_a_failure(self) -> None:
        checks = diagnostics.check_selectors_configured(_browser_config())
        by_name = {check.name: check for check in checks}
        self.assertEqual(by_name["selector.stop_button"].status, WARN)
        self.assertIn("half-written", by_name["selector.stop_button"].hint)
        self.assertEqual(by_name["selector.prompt_input"].status, OK)

    def test_missing_required_selector_fails(self) -> None:
        config = _browser_config(
            selectors=Selectors(prompt_input="", assistant_messages=".a")
        )
        by_name = {
            check.name: check
            for check in diagnostics.check_selectors_configured(config)
        }
        self.assertEqual(by_name["selector.prompt_input"].status, FAIL)
        self.assertEqual(by_name["selector.send_button"].status, WARN)

    def test_profile_missing_is_a_warning(self) -> None:
        config = _browser_config(user_data_dir=str(self.root / "nope"))
        check = diagnostics.check_profile(config)
        self.assertEqual(check.status, WARN)
        self.assertIn("does not exist", check.detail)

    def test_profile_with_cookies_passes(self) -> None:
        profile = self.root / "profile" / "Default"
        profile.mkdir(parents=True)
        (profile / "Cookies").write_text("", encoding="utf-8")
        config = _browser_config(user_data_dir=str(self.root / "profile"))
        self.assertEqual(diagnostics.check_profile(config).status, OK)

    def test_openai_without_key_fails(self) -> None:
        config = _browser_config(backend=BACKEND_OPENAI, api_key="")
        self.assertEqual(diagnostics.check_api_key(config).status, FAIL)

    def test_ollama_needs_no_key(self) -> None:
        config = _browser_config(backend=BACKEND_OLLAMA)
        self.assertEqual(diagnostics.check_api_key(config).status, SKIP)

    def test_queue_without_worker_warns(self) -> None:
        config = _browser_config(queue_dir=str(self.root / "queue"))
        check = diagnostics.check_queue(config)
        self.assertEqual(check.status, WARN)
        self.assertIn("critique-bot worker", check.hint)

    def test_static_checks_cover_the_browser_backend(self) -> None:
        config = _browser_config(queue_dir=str(self.root / "queue"))
        with patch(
            "critique_bot.browser.resolve_browser",
            return_value=("/usr/bin/microsoft-edge", "msedge"),
        ):
            report = diagnostics.static_checks(config)
        names = {check.name for check in report.checks}
        self.assertIn("browser", names)
        self.assertIn("selector.prompt_input", names)
        self.assertIn("url", names)

    def test_static_checks_skip_selectors_for_http_backends(self) -> None:
        config = _browser_config(
            backend=BACKEND_OLLAMA,
            base_url="http://127.0.0.1:11434/v1",
            queue_dir=str(self.root / "queue"),
        )
        report = diagnostics.static_checks(config)
        names = {check.name for check in report.checks}
        self.assertIn("base_url", names)
        self.assertNotIn("selector.prompt_input", names)

    def test_missing_browser_is_reported_not_raised(self) -> None:
        from critique_bot.browser import BrowserError

        with patch(
            "critique_bot.browser.resolve_browser",
            side_effect=BrowserError("No Chromium browser was found."),
        ):
            check = diagnostics.check_browser()
        self.assertEqual(check.status, FAIL)
        self.assertIn("microsoft-edge-stable", check.hint)

    def test_chrome_fallback_warns(self) -> None:
        with patch(
            "critique_bot.browser.resolve_browser",
            return_value=("/usr/bin/google-chrome", "chrome"),
        ):
            self.assertEqual(diagnostics.check_browser().status, WARN)


class _FakePage:
    def __init__(self, counts: dict[str, tuple[int, int]]) -> None:
        self.counts = counts

    def evaluate(self, script: str, selectors: object = None) -> object:
        del script
        out = []
        for selector in selectors or []:  # type: ignore[union-attr]
            total, visible = self.counts.get(selector, (0, 0))
            out.append(
                {"selector": selector, "total": total, "visible": visible, "error": ""}
            )
        return out


class LiveProbeTests(unittest.TestCase):
    def test_selector_matches_reports_per_selector(self) -> None:
        page = _FakePage({"#p": (1, 1), ".missing": (0, 0)})
        result = diagnostics.selector_matches(page, ["#p", ".missing"])  # type: ignore[arg-type]
        self.assertEqual(result[0]["visible"], 1)
        self.assertEqual(result[1]["total"], 0)

    def test_selector_matches_survives_an_evaluate_failure(self) -> None:
        class Boom:
            def evaluate(self, script: str, selectors: object = None) -> object:
                raise RuntimeError("detached")

        result = diagnostics.selector_matches(Boom(), ["#p"])  # type: ignore[arg-type]
        self.assertIn("detached", result[0]["error"])

    def test_probe_selectors_flags_a_selector_that_matches_nothing(self) -> None:
        selectors = Selectors(
            prompt_input="#p", assistant_messages=".a", send_button="button.send"
        )
        page = _FakePage({"#p": (1, 1), "button.send": (0, 0), ".a": (0, 0)})
        by_name = {
            check.name: check
            for check in diagnostics.probe_selectors(page, selectors)  # type: ignore[arg-type]
        }
        self.assertEqual(by_name["live.prompt_input"].status, OK)
        self.assertEqual(by_name["live.send_button"].status, WARN)
        # No reply on screen yet is expected on a fresh chat.
        self.assertEqual(by_name["live.assistant_messages"].status, WARN)

    def test_probe_selectors_warns_when_matches_are_hidden(self) -> None:
        selectors = Selectors(prompt_input="#p", assistant_messages="")
        page = _FakePage({"#p": (3, 0)})
        check = diagnostics.probe_selectors(page, selectors)[0]  # type: ignore[arg-type]
        self.assertEqual(check.status, WARN)
        self.assertIn("none visible", check.detail)

    def test_probe_login_flags_a_block_page(self) -> None:
        with patch(
            "critique_bot.browser.page_block_hint", return_value="login/SSO page"
        ):
            check = diagnostics.probe_login(object())  # type: ignore[arg-type]
        self.assertEqual(check.status, FAIL)
        self.assertIn("--headed", check.hint)

    def test_probe_login_passes_on_a_normal_page(self) -> None:
        with patch("critique_bot.browser.page_block_hint", return_value=""):
            self.assertEqual(diagnostics.probe_login(object()).status, OK)  # type: ignore[arg-type]

    def test_round_trip_warns_when_only_idle_detected_completion(self) -> None:
        from critique_bot.llm import COMPLETION_IDLE

        def fake_send(page, config, prompt, *, detail=None):
            del page, config, prompt
            if detail is not None:
                detail["completion"] = COMPLETION_IDLE
            return "PONG"

        with patch("critique_bot.chat_client.send_turn", fake_send):
            check = diagnostics.probe_round_trip(object(), _browser_config())  # type: ignore[arg-type]
        self.assertEqual(check.status, WARN)
        self.assertIn("stop_button", check.hint)

    def test_round_trip_passes_on_a_clean_finish(self) -> None:
        from critique_bot.llm import COMPLETION_STOPPED

        def fake_send(page, config, prompt, *, detail=None):
            del page, config, prompt
            if detail is not None:
                detail["completion"] = COMPLETION_STOPPED
            return "PONG"

        with patch("critique_bot.chat_client.send_turn", fake_send):
            check = diagnostics.probe_round_trip(object(), _browser_config())  # type: ignore[arg-type]
        self.assertEqual(check.status, OK)

    def test_round_trip_reports_a_chat_error(self) -> None:
        from critique_bot.chat_client import ChatError

        with patch("critique_bot.chat_client.send_turn", side_effect=ChatError("no input")):
            check = diagnostics.probe_round_trip(object(), _browser_config())  # type: ignore[arg-type]
        self.assertEqual(check.status, FAIL)
        self.assertIn("no input", check.detail)

    def test_round_trip_rejects_an_empty_reply(self) -> None:
        with patch("critique_bot.chat_client.send_turn", return_value="  "):
            check = diagnostics.probe_round_trip(object(), _browser_config())  # type: ignore[arg-type]
        self.assertEqual(check.status, FAIL)


class SnapshotHelperTests(unittest.TestCase):
    def test_config_snapshot_hides_the_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"url": "https://x/", "api_key": "secret"}), encoding="utf-8"
            )
            snapshot = diagnostics.config_snapshot(path)
        self.assertEqual(snapshot["url"], "https://x/")
        self.assertNotIn("api_key", snapshot)

    def test_config_snapshot_of_broken_json_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ nope", encoding="utf-8")
            self.assertEqual(diagnostics.config_snapshot(path), {})

    def test_env_snapshot_never_leaks_a_key_value(self) -> None:
        with patch.dict(
            "os.environ",
            {"CRITIQUE_MODEL": "GPT-5.1", "OPENAI_API_KEY": "sk-secret"},
            clear=False,
        ):
            snapshot = diagnostics.env_snapshot()
        self.assertEqual(snapshot["CRITIQUE_MODEL"], "GPT-5.1")
        self.assertEqual(snapshot["API key"], "set (value hidden)")
        self.assertNotIn("sk-secret", json.dumps(snapshot))


if __name__ == "__main__":
    unittest.main()
