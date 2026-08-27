from __future__ import annotations

import time
from typing import TYPE_CHECKING

from critique_bot.config import BotConfig, Selectors

if TYPE_CHECKING:
    from playwright.sync_api import Frame, Locator, Page


class ChatError(RuntimeError):
    """The web chat UI did not complete a review."""


POLL_MS = 250
MENU_OPEN_MS = 400

# Walks light DOM + open shadow roots. Returns the smallest/best matching
# element for the configured model label (e.g. "GPT-5.1").
_FIND_MODEL_JS = """
(needle) => {
  const target = String(needle || "").replace(/\\s+/g, " ").trim();
  if (!target) return null;
  const needleLower = target.toLowerCase();
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "META", "LINK"]);

  const norm = (s) => String(s || "").replace(/\\s+/g, " ").trim();

  const isVisible = (el) => {
    if (!(el instanceof Element)) return false;
    const st = getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden" || Number(st.opacity) === 0) {
      return false;
    }
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const hostParent = (node) => {
    const root = node.getRootNode && node.getRootNode();
    return root && root.host ? root.host : null;
  };

  const inPopup = (el) => {
    let n = el;
    while (n) {
      if (n instanceof Element) {
        const role = (n.getAttribute("role") || "").toLowerCase();
        if (["listbox", "menu", "dialog", "list", "group"].includes(role)) return true;
        if (n.tagName === "SELECT") return true;
        const cls = typeof n.className === "string" ? n.className.toLowerCase() : "";
        if (/(dropdown|popover|listbox|menu-list|combobox|picker)/.test(cls)) return true;
      }
      n = n.parentElement || hostParent(n);
    }
    return false;
  };

  const ownText = (el) => {
    let t = "";
    for (const child of el.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) t += child.textContent || "";
    }
    return norm(t);
  };

  const clickable = (el) =>
    el.closest('button, a, [role="option"], [role="menuitem"], [role="combobox"], li, option, [tabindex]') || el;

  const candidates = [];
  const visit = (root) => {
    if (root instanceof Document) {
      if (root.documentElement) visit(root.documentElement);
      return;
    }
    if (root instanceof Element) {
      if (SKIP.has(root.tagName)) return;
      if (root.shadowRoot) visit(root.shadowRoot);
    }
    const children =
      root instanceof Element || root instanceof ShadowRoot
        ? Array.from(root.children)
        : [];
    for (const child of children) visit(child);
    if (!(root instanceof Element)) return;
    if (root.tagName === "HTML" || root.tagName === "BODY") return;

    const own = ownText(root);
    const inner = norm(root.innerText || "");
    const content = norm(root.textContent || "");
    const hay = own || inner || content;
    if (!hay.toLowerCase().includes(needleLower)) return;

    let score = 0;
    if (own.toLowerCase() === needleLower) score += 130;
    else if (inner.toLowerCase() === needleLower) score += 110;
    else if (own.toLowerCase().includes(needleLower)) score += 80;
    else if (inner.toLowerCase().includes(needleLower)) score += 50;
    else score += 20;

    if (root.tagName === "DIV") score += 8;
    const role = (root.getAttribute("role") || "").toLowerCase();
    if (role === "option" || role === "menuitem" || root.tagName === "OPTION" || root.tagName === "LI") {
      score += 25;
    }
    if (inPopup(root)) score += 22;
    if (isVisible(root)) score += 15;
    score -= Math.min(hay.length, 240) * 0.08;
    candidates.push({ el: clickable(root), score, visible: isVisible(root), inPopup: inPopup(root) });
  };

  visit(document);
  candidates.sort((a, b) => b.score - a.score);
  return candidates.length ? candidates[0].el : null;
}
"""

_SELECT_NATIVE_JS = """
(needle) => {
  const target = String(needle || "").replace(/\\s+/g, " ").trim().toLowerCase();
  if (!target) return false;
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"]);
  const norm = (s) => String(s || "").replace(/\\s+/g, " ").trim().toLowerCase();
  const selects = [];
  const visit = (root) => {
    if (root instanceof Document) {
      if (root.documentElement) visit(root.documentElement);
      return;
    }
    if (root instanceof Element) {
      if (SKIP.has(root.tagName)) return;
      if (root.tagName === "SELECT") selects.push(root);
      if (root.shadowRoot) visit(root.shadowRoot);
    }
    const children =
      root instanceof Element || root instanceof ShadowRoot
        ? Array.from(root.children)
        : [];
    for (const child of children) visit(child);
  };
  visit(document);
  for (const sel of selects) {
    for (const opt of Array.from(sel.options)) {
      if (norm(opt.text).includes(target) || norm(opt.value) === target) {
        sel.value = opt.value;
        opt.selected = true;
        sel.dispatchEvent(new Event("input", { bubbles: true }));
        sel.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      }
    }
  }
  return false;
}
"""

_FIND_OPENER_JS = """
() => {
  const selectors = [
    '[role="combobox"]',
    '[aria-haspopup="listbox"]',
    '[aria-haspopup="menu"]',
    'button[aria-expanded]',
    '[aria-expanded="false"]',
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) return el;
  }
  return null;
}
"""


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


def _frames(page: Page) -> list[Frame]:
    return list(page.frames)


def _dispose(handle: object) -> None:
    dispose = getattr(handle, "dispose", None)
    if callable(dispose):
        try:
            dispose()
        except Exception:
            pass


def _try_native_select(page: Page, model: str) -> bool:
    for frame in _frames(page):
        try:
            if frame.evaluate(_SELECT_NATIVE_JS, model):
                return True
        except Exception:
            continue
    return False


def _find_model_element(page: Page, model: str):
    for frame in _frames(page):
        handle = None
        try:
            handle = frame.evaluate_handle(_FIND_MODEL_JS, model)
            element = handle.as_element()
            if element is not None:
                return element
        except Exception:
            pass
        _dispose(handle)
    return None


def _element_meta(element) -> dict[str, bool]:
    try:
        return element.evaluate(
            """el => {
              const st = getComputedStyle(el);
              const r = el.getBoundingClientRect();
              const visible = st.display !== "none" && st.visibility !== "hidden"
                && Number(st.opacity) !== 0 && r.width > 0 && r.height > 0;
              let n = el;
              let inPopup = false;
              while (n) {
                if (n instanceof Element) {
                  const role = (n.getAttribute("role") || "").toLowerCase();
                  if (["listbox", "menu", "dialog", "list", "group"].includes(role)
                      || n.tagName === "SELECT") {
                    inPopup = true;
                    break;
                  }
                }
                const root = n.getRootNode && n.getRootNode();
                n = n.parentElement || (root && root.host) || null;
              }
              return { visible, inPopup };
            }"""
        )
    except Exception:
        return {"visible": False, "inPopup": False}


def _click_element(element, timeout_ms: int) -> bool:
    try:
        element.scroll_into_view_if_needed(timeout=timeout_ms)
    except Exception:
        pass
    try:
        element.click(timeout=timeout_ms)
        return True
    except Exception:
        try:
            element.click(timeout=timeout_ms, force=True)
            return True
        except Exception:
            return False


def _open_model_menu(page: Page, selectors: Selectors, timeout_ms: int, trigger=None) -> bool:
    if selectors.model_dropdown:
        dropdown = page.locator(selectors.model_dropdown)
        if dropdown.count() > 0:
            if _click_element(dropdown.first, timeout_ms):
                page.wait_for_timeout(MENU_OPEN_MS)
                return True

    if trigger is not None and _click_element(trigger, timeout_ms):
        page.wait_for_timeout(MENU_OPEN_MS)
        return True

    for frame in _frames(page):
        handle = None
        try:
            handle = frame.evaluate_handle(_FIND_OPENER_JS)
            opener = handle.as_element()
            if opener is not None and _click_element(opener, timeout_ms):
                page.wait_for_timeout(MENU_OPEN_MS)
                return True
        except Exception:
            pass
        finally:
            if handle is not None:
                _dispose(handle)
    return False


def _select_model(page: Page, selectors: Selectors, model: str, timeout_ms: int) -> None:
    if not model:
        return

    if _try_native_select(page, model):
        return

    if selectors.model_dropdown:
        dropdown = page.locator(selectors.model_dropdown)
        if dropdown.count() > 0:
            tag = dropdown.first.evaluate("el => (el.tagName || '').toLowerCase()")
            if tag == "select":
                try:
                    dropdown.first.select_option(value=model, timeout=timeout_ms)
                    return
                except Exception:
                    pass
                try:
                    dropdown.first.select_option(label=model, timeout=timeout_ms)
                    return
                except Exception:
                    pass

    deadline = time.monotonic() + timeout_ms / 1000
    opened = False
    last_trigger = None
    while time.monotonic() < deadline:
        element = _find_model_element(page, model)
        if element is None:
            if not opened:
                _open_model_menu(page, selectors, timeout_ms)
                opened = True
            else:
                page.wait_for_timeout(POLL_MS)
            continue

        meta = _element_meta(element)
        if meta.get("inPopup") and meta.get("visible"):
            if _click_element(element, timeout_ms):
                return
            raise ChatError(f"found {model!r} in a dropdown but could not click it")

        if meta.get("visible") and not meta.get("inPopup"):
            last_trigger = element
            if not opened:
                _open_model_menu(page, selectors, timeout_ms, trigger=element)
                opened = True
                continue
            if _click_element(element, timeout_ms):
                return

        if not meta.get("visible") and not opened:
            _open_model_menu(page, selectors, timeout_ms, trigger=last_trigger)
            opened = True
            continue

        page.wait_for_timeout(POLL_MS)

    raise ChatError(
        f"timed out selecting model {model!r} from the page DOM"
    )


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
