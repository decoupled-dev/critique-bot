from __future__ import annotations

import time
from typing import TYPE_CHECKING

from critique_bot.config import BotConfig, Selectors

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page


class ChatError(RuntimeError):
    """The web chat UI did not complete a review."""


POLL_MS = 250


def _wait_visible(locator: Locator, timeout_ms: int, what: str) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        locator.first.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise ChatError(f"timed out waiting for {what}: {locator.first}") from exc


def _fill_prompt(locator: Locator, text: str, timeout_ms: int) -> None:
    locator = locator.first
    _wait_visible(locator, timeout_ms, "prompt input")
    try:
        locator.fill(text, timeout=timeout_ms)
        return
    except Exception:
        pass
    locator.evaluate(
        """(el, value) => {
            const proto = el instanceof HTMLTextAreaElement
                ? HTMLTextAreaElement.prototype
                : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
            if (setter) {
                setter.call(el, value);
            } else {
                el.value = value;
            }
            el.dispatchEvent(new Event("input", { bubbles: true }));
            el.dispatchEvent(new Event("change", { bubbles: true }));
        }""",
        text,
    )


def _select_model(page: Page, selectors: Selectors, model: str, timeout_ms: int) -> None:
    if not selectors.model_dropdown or not model:
        return

    dropdown = page.locator(selectors.model_dropdown).first
    _wait_visible(dropdown, timeout_ms, "model dropdown")

    tag = dropdown.evaluate("el => (el.tagName || '').toLowerCase()")
    if tag == "select":
        try:
            dropdown.select_option(value=model, timeout=timeout_ms)
            return
        except Exception:
            pass
        try:
            dropdown.select_option(label=model, timeout=timeout_ms)
            return
        except Exception as exc:
            raise ChatError(
                f"could not select model {model!r} on native <select>"
            ) from exc

    dropdown.click(timeout=timeout_ms)
    try:
        if selectors.model_option:
            page.locator(selectors.model_option).filter(has_text=model).first.click(
                timeout=timeout_ms
            )
        else:
            page.get_by_role("option", name=model, exact=True).click(timeout=timeout_ms)
    except Exception as exc:
        raise ChatError(
            f"could not select model {model!r} from custom dropdown"
        ) from exc


def _send(page: Page, selectors: Selectors, timeout_ms: int) -> None:
    if selectors.send_button:
        button = page.locator(selectors.send_button).first
        _wait_visible(button, timeout_ms, "send button")
        button.click(timeout=timeout_ms)
        return
    page.locator(selectors.prompt_input).first.press("Enter", timeout=timeout_ms)


def _wait_for_reply(
    page: Page,
    selector: str,
    *,
    previous_count: int,
    timeout_ms: int,
    idle_ms: int,
) -> str:
    deadline = time.monotonic() + timeout_ms / 1000
    messages = page.locator(selector)

    while time.monotonic() < deadline:
        if messages.count() > previous_count:
            break
        page.wait_for_timeout(POLL_MS)
    else:
        raise ChatError(
            "no assistant message appeared "
            f"(selector={selector!r}, previous_count={previous_count})"
        )

    last_text = ""
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        count = messages.count()
        text = messages.nth(count - 1).inner_text()
        if text != last_text:
            last_text = text
            last_change = time.monotonic()
        elif last_text.strip() and (time.monotonic() - last_change) * 1000 >= idle_ms:
            return last_text.strip()
        page.wait_for_timeout(POLL_MS)

    raise ChatError(
        "timed out waiting for the assistant reply to finish streaming "
        f"({len(last_text)} chars captured)"
    )


def submit_review(page: Page, config: BotConfig, prompt: str) -> str:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    selectors = config.selectors
    timeout_ms = config.timeout_ms

    try:
        page.goto(config.url, wait_until="domcontentloaded", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise ChatError(f"timed out loading chat UI: {config.url}") from exc

    _wait_visible(
        page.locator(selectors.prompt_input),
        timeout_ms,
        "prompt input after navigation",
    )
    _select_model(page, selectors, config.model, timeout_ms)

    previous_count = page.locator(selectors.assistant_messages).count()
    _fill_prompt(page.locator(selectors.prompt_input), prompt, timeout_ms)
    _send(page, selectors, timeout_ms)

    return _wait_for_reply(
        page,
        selectors.assistant_messages,
        previous_count=previous_count,
        timeout_ms=timeout_ms,
        idle_ms=config.idle_ms,
    )
