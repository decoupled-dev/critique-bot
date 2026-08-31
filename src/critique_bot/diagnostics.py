"""Structured setup checks shared by `critique-bot doctor` and the setup UI.

Everything here returns data instead of raising, so the same probes can render
as terminal output, as JSON for scripts, or as rows in the local web UI.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from critique_bot import log
from critique_bot.config import (
    BACKEND_BROWSER,
    BACKEND_OLLAMA,
    BACKEND_OPENAI,
    BotConfig,
    Selectors,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

DEFAULT_PROBE_PROMPT = (
    "Reply with exactly one word: PONG. Do not explain."
)

# One round trip to the page instead of one per selector.
_MATCH_COUNT_JS = """
(selectors) => {
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    if (parseFloat(style.opacity || '1') === 0) return false;
    return true;
  };
  return selectors.map((selector) => {
    if (!selector) return { selector, total: 0, visible: 0, error: 'empty' };
    let nodes;
    try {
      nodes = Array.from(document.querySelectorAll(selector));
    } catch (err) {
      return { selector, total: 0, visible: 0, error: String(err.message || err) };
    }
    return {
      selector,
      total: nodes.length,
      visible: nodes.filter(visible).length,
      error: '',
    };
  });
}
"""


@dataclass
class Check:
    """One diagnostic line: what was tested, how it went, what to do next."""

    name: str
    status: str
    detail: str
    hint: str = ""

    @property
    def ok(self) -> bool:
        """Warnings are advice, not failures; only FAIL blocks a green run."""
        return self.status != FAIL


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if check.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [check for check in self.checks if check.status == WARN]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [asdict(check) for check in self.checks],
        }


def _check(name: str, status: str, detail: str, hint: str = "") -> Check:
    return Check(name=name, status=status, detail=detail, hint=hint)


def check_python() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info < (3, 10):
        return _check(
            "python",
            FAIL,
            f"Python {version}",
            "critique-bot needs Python 3.10 or newer",
        )
    return _check("python", OK, f"Python {version}")


def check_playwright() -> Check:
    try:
        import playwright  # noqa: F401
    except ModuleNotFoundError:
        return _check(
            "playwright",
            FAIL,
            "not installed",
            "pip install -r requirements.txt",
        )
    try:
        from importlib.metadata import version

        return _check("playwright", OK, f"installed ({version('playwright')})")
    except Exception:
        return _check("playwright", OK, "installed")


def check_browser() -> Check:
    """Edge (or Chrome) has to be on the machine; the bot drives system Edge."""
    from critique_bot.browser import BrowserError, resolve_browser

    try:
        executable, channel = resolve_browser()
    except BrowserError as exc:
        return _check(
            "browser",
            FAIL,
            str(exc),
            "Linux: sudo apt install microsoft-edge-stable. "
            "Windows/macOS: install Microsoft Edge or Google Chrome.",
        )
    if channel != "msedge":
        return _check(
            "browser",
            WARN,
            f"{channel} at {executable}",
            "Microsoft Edge was not found; Chrome is being used instead",
        )
    return _check("browser", OK, f"Microsoft Edge at {executable}")


def check_profile(config: BotConfig) -> Check:
    """A populated profile directory is how the bot stays signed in."""
    raw = config.user_data_dir or ""
    if not raw:
        return _check("profile", WARN, "no user_data_dir resolved")
    path = Path(raw)
    if not path.exists():
        return _check(
            "profile",
            WARN,
            f"{path} does not exist yet",
            "Run `critique-bot doctor --live --headed` (or any --headed run) "
            "and sign in once; the profile is created then.",
        )
    has_login_state = (path / "Default" / "Cookies").is_file() or (
        path / "Default" / "Network" / "Cookies"
    ).is_file()
    if not has_login_state:
        return _check(
            "profile",
            WARN,
            f"{path} exists but holds no cookie store",
            "Sign in once with --headed so the session is saved",
        )
    return _check("profile", OK, f"{path} (has a saved session)")


def check_selectors_configured(config: BotConfig) -> list[Check]:
    """Static config sanity: which selectors are set, before touching a page."""
    selectors = config.selectors
    checks: list[Check] = []
    for name, value, required in (
        ("prompt_input", selectors.prompt_input, True),
        ("assistant_messages", selectors.assistant_messages, True),
        ("send_button", selectors.send_button, False),
        ("stop_button", selectors.stop_button, False),
    ):
        if value:
            checks.append(_check(f"selector.{name}", OK, value))
        elif required:
            checks.append(
                _check(
                    f"selector.{name}",
                    FAIL,
                    "not set",
                    f"selectors.{name} is required for the browser backend",
                )
            )
        elif name == "send_button":
            checks.append(
                _check(
                    "selector.send_button",
                    WARN,
                    "not set; Enter will be pressed instead",
                    "Set selectors.send_button if the UI needs a real click",
                )
            )
        else:
            checks.append(
                _check(
                    "selector.stop_button",
                    WARN,
                    "not set",
                    "Without it, a reply that pauses longer than idle_ms can be "
                    "captured half-written. Set selectors.stop_button to the "
                    "'stop generating' control.",
                )
            )
    return checks


def check_queue(config: BotConfig) -> Check:
    from critique_bot.queue import FileQueue

    root = Path(config.queue_dir)
    try:
        queue = FileQueue(root)
    except OSError as exc:
        return _check(
            "queue",
            FAIL,
            f"cannot use {root}: {exc}",
            "Point queue_dir at a directory this user can write to",
        )
    if queue.worker_alive():
        return _check("queue", OK, f"{root} (worker {queue.worker_hint()})")
    return _check(
        "queue",
        WARN,
        f"{root} (no live worker: {queue.worker_hint()})",
        "CI submits need a worker: critique-bot worker --config CONFIG --logs",
    )


def check_api_key(config: BotConfig) -> Check:
    if config.backend == BACKEND_OLLAMA:
        return _check("api_key", SKIP, "not needed for ollama")
    if config.api_key:
        return _check("api_key", OK, "set")
    if config.backend == BACKEND_OPENAI:
        return _check(
            "api_key",
            FAIL,
            "missing",
            "export OPENAI_API_KEY=... or CRITIQUE_API_KEY=...",
        )
    return _check(
        "api_key",
        WARN,
        "not set (fine if the server allows anonymous access)",
    )


def static_checks(config: BotConfig, *, config_path: Path | None = None) -> Report:
    """Everything that can be answered without opening a browser or socket."""
    report = Report()
    report.add(check_python())
    if config_path is not None:
        report.add(_check("config", OK, str(Path(config_path).resolve())))
    report.add(_check("backend", OK, config.backend))
    if config.uses_browser:
        report.add(check_playwright())
        report.add(check_browser())
        report.add(_check("url", OK, config.url))
        report.add(check_profile(config))
        for check in check_selectors_configured(config):
            report.add(check)
        report.add(
            _check(
                "model",
                OK if config.model else WARN,
                config.model or "not set; whatever the UI has selected is used",
                "" if config.model else "Set `model` to the visible label, e.g. GPT-5.1",
            )
        )
    else:
        report.add(_check("base_url", OK, config.base_url))
        report.add(_check("model", OK, config.model))
        report.add(check_api_key(config))
    report.add(check_queue(config))
    return report


def selector_matches(page: Page, selectors: list[str]) -> list[dict[str, Any]]:
    """Match counts for arbitrary selectors on the live page."""
    cleaned = [item or "" for item in selectors]
    if not cleaned:
        return []
    try:
        result = page.evaluate(_MATCH_COUNT_JS, cleaned)
    except Exception as exc:
        return [
            {"selector": item, "total": 0, "visible": 0, "error": str(exc)}
            for item in cleaned
        ]
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict)]


def probe_login(page: Page) -> Check:
    from critique_bot.browser import page_block_hint

    hint = page_block_hint(page)
    if hint:
        return _check(
            "login",
            FAIL,
            hint,
            "Run with --headed and sign in; the profile keeps the session "
            "for later headless runs.",
        )
    return _check("login", OK, "no login or bot-challenge page detected")


def probe_selectors(page: Page, selectors: Selectors) -> list[Check]:
    """Do the configured selectors actually match anything on this page?"""
    wanted = [
        ("prompt_input", selectors.prompt_input, True),
        ("send_button", selectors.send_button, False),
        ("assistant_messages", selectors.assistant_messages, False),
        ("stop_button", selectors.stop_button, False),
        ("model_dropdown", selectors.model_dropdown, False),
    ]
    active = [(name, value, required) for name, value, required in wanted if value]
    results = selector_matches(page, [value for _, value, _ in active])
    checks: list[Check] = []
    for (name, value, required), found in zip(active, results):
        error = str(found.get("error") or "")
        total = int(found.get("total") or 0)
        visible = int(found.get("visible") or 0)
        label = f"live.{name}"
        if error:
            checks.append(_check(label, FAIL, f"{value!r}: invalid CSS ({error})"))
        elif visible:
            checks.append(
                _check(label, OK, f"{value!r} matches {visible} visible element(s)")
            )
        elif total:
            checks.append(
                _check(
                    label,
                    WARN,
                    f"{value!r} matches {total} element(s), none visible",
                    "Hidden matches usually mean the selector is too broad",
                )
            )
        elif name == "assistant_messages":
            # No reply on screen yet is normal on a fresh chat.
            checks.append(
                _check(label, WARN, f"{value!r} matches nothing yet (no reply on screen)")
            )
        else:
            checks.append(
                _check(
                    label,
                    FAIL if required else WARN,
                    f"{value!r} matches nothing on this page",
                    "Use `critique-bot setup` to pick this element by clicking it",
                )
            )
    return checks


def probe_round_trip(
    page: Page,
    config: BotConfig,
    *,
    prompt: str = DEFAULT_PROBE_PROMPT,
) -> Check:
    """Send a real prompt and read a real reply."""
    from critique_bot.chat_client import ChatError, send_turn
    from critique_bot.llm import COMPLETION_IDLE

    detail: dict[str, Any] = {}
    try:
        reply = send_turn(page, config, prompt, detail=detail)
    except ChatError as exc:
        return _check("round_trip", FAIL, str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _check("round_trip", FAIL, f"unexpected failure: {exc}")
    if not reply.strip():
        return _check("round_trip", FAIL, "the assistant replied with nothing")
    preview = log.preview(reply, 60)
    if detail.get("completion") == COMPLETION_IDLE:
        return _check(
            "round_trip",
            WARN,
            f"got {len(reply)} chars ({preview!r}) but only via an idle timeout",
            "Set selectors.stop_button so long replies are not cut off",
        )
    return _check("round_trip", OK, f"got {len(reply)} chars ({preview!r})")


def probe_http_round_trip(
    config: BotConfig,
    *,
    prompt: str = DEFAULT_PROBE_PROMPT,
) -> Check:
    from critique_bot.llm import HttpChatClient, LLMError

    try:
        reply = HttpChatClient(config).complete([{"role": "user", "content": prompt}])
    except LLMError as exc:
        return _check("round_trip", FAIL, str(exc))
    return _check("round_trip", OK, f"got {len(reply)} chars ({log.preview(reply, 60)!r})")


def run_live_checks(
    config: BotConfig,
    *,
    headed: bool = False,
    round_trip: bool = True,
) -> Report:
    """Open the backend for real and check login, selectors, and a round trip."""
    report = Report()
    if not config.uses_browser:
        if round_trip:
            report.add(probe_http_round_trip(config))
        else:
            report.add(_check("round_trip", SKIP, "skipped"))
        return report

    from critique_bot.browser import BrowserError, launch_edge
    from critique_bot.chat_client import ChatError, prepare_chat

    try:
        with launch_edge(
            headed=headed,
            storage_state=config.storage_state,
            user_data_dir=config.user_data_dir,
            cdp_url=config.cdp_url,
            start_url=config.url,
            timeout_ms=config.timeout_ms,
        ) as page:
            report.add(_check("page", OK, f"opened {config.url}"))
            login = report.add(probe_login(page))
            for check in probe_selectors(page, config.selectors):
                report.add(check)
            if not login.ok:
                report.add(
                    _check("round_trip", SKIP, "skipped: sign in first")
                )
                return report
            try:
                prepare_chat(page, config)
                report.add(_check("chat_ready", OK, "prompt input is usable"))
            except ChatError as exc:
                report.add(_check("chat_ready", FAIL, str(exc)))
                report.add(_check("round_trip", SKIP, "skipped: chat UI not ready"))
                return report
            if round_trip:
                report.add(probe_round_trip(page, config))
            else:
                report.add(_check("round_trip", SKIP, "skipped"))
    except BrowserError as exc:
        report.add(_check("page", FAIL, str(exc)))
    return report


def render_text(report: Report) -> str:
    marks = {OK: "PASS", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP"}
    width = max((len(check.name) for check in report.checks), default=0)
    lines: list[str] = []
    for check in report.checks:
        lines.append(
            f"[{marks.get(check.status, '????')}] {check.name.ljust(width)}  {check.detail}"
        )
        if check.hint and check.status in (WARN, FAIL):
            lines.append(f"{' ' * (width + 9)}-> {check.hint}")
    return "\n".join(lines)


def render_json(report: Report) -> str:
    return json.dumps(report.to_dict(), indent=2) + "\n"


def config_snapshot(path: Path) -> dict[str, Any]:
    """Raw config JSON for the setup UI (never includes a live API key)."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    data.pop("api_key", None)
    return data


def env_snapshot() -> dict[str, str]:
    """Which CRITIQUE_* overrides are active, so the UI can explain surprises."""
    names = (
        "CRITIQUE_BACKEND",
        "CRITIQUE_CHAT_URL",
        "CRITIQUE_MODEL",
        "CRITIQUE_BASE_URL",
        "CRITIQUE_USER_DATA_DIR",
        "CRITIQUE_CDP_URL",
        "CRITIQUE_QUEUE_DIR",
        "CRITIQUE_STORAGE_STATE",
    )
    found = {name: os.environ[name] for name in names if os.environ.get(name)}
    if os.environ.get("CRITIQUE_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        found["API key"] = "set (value hidden)"
    return found


__all__ = [
    "BACKEND_BROWSER",
    "Check",
    "DEFAULT_PROBE_PROMPT",
    "FAIL",
    "OK",
    "Report",
    "SKIP",
    "WARN",
    "config_snapshot",
    "env_snapshot",
    "probe_http_round_trip",
    "probe_login",
    "probe_round_trip",
    "probe_selectors",
    "render_json",
    "render_text",
    "run_live_checks",
    "selector_matches",
    "static_checks",
]
