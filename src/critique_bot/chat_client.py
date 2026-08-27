from __future__ import annotations

import time
from typing import TYPE_CHECKING

from critique_bot import log
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

    log.debug(f"waiting up to {timeout_ms}ms for {what} ({locator.first})")
    try:
        locator.first.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        log.error(f"timed out waiting for {what}: {locator.first}")
        raise ChatError(f"timed out waiting for {what}: {locator.first}") from exc
    log.debug(f"{what} is visible")


def _fill_prompt(locator: Locator, text: str, timeout_ms: int) -> None:
    locator = locator.first
    log.info(f"filling prompt ({len(text)} chars, preview={log.preview(text)!r})")
    _wait_visible(locator, timeout_ms, "prompt input")
    try:
        locator.fill(text, timeout=timeout_ms)
        log.debug("prompt filled via locator.fill")
        return
    except Exception as exc:
        log.warn(f"locator.fill failed ({exc}); falling back to DOM value setter")
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
    log.debug("prompt filled via DOM value setter")


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
                log.info(f"selected {model!r} via native <select> in frame {frame.url!r}")
                return True
        except Exception as exc:
            log.debug(f"native <select> scan failed in {frame.url!r}: {exc}")
            continue
    log.debug(f"no native <select> option matched {model!r}")
    return False


def _describe_element(element) -> str:
    try:
        info = element.evaluate(
            """el => ({
              tag: el.tagName,
              role: el.getAttribute("role") || "",
              text: String(el.innerText || el.textContent || "")
                .replace(/\\s+/g, " ").trim().slice(0, 80)
            })"""
        )
        return f"{info.get('tag')} role={info.get('role')!r} text={info.get('text')!r}"
    except Exception as exc:
        return f"<unreadable element: {exc}>"


def _find_model_element(page: Page, model: str):
    for frame in _frames(page):
        handle = None
        try:
            handle = frame.evaluate_handle(_FIND_MODEL_JS, model)
            element = handle.as_element()
            if element is not None:
                log.debug(
                    f"DOM match for {model!r} in {frame.url!r}: {_describe_element(element)}"
                )
                return element
        except Exception as exc:
            log.debug(f"model DOM scan failed in {frame.url!r}: {exc}")
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
    desc = _describe_element(element)
    try:
        element.scroll_into_view_if_needed(timeout=timeout_ms)
        log.debug(f"scrolled into view: {desc}")
    except Exception as exc:
        log.debug(f"scroll_into_view skipped: {exc}")
    try:
        element.click(timeout=timeout_ms)
        log.debug(f"clicked {desc}")
        return True
    except Exception as exc:
        log.debug(f"normal click failed ({exc}); trying force click on {desc}")
        try:
            element.click(timeout=timeout_ms, force=True)
            log.debug(f"force-clicked {desc}")
            return True
        except Exception as exc2:
            log.warn(f"could not click {desc}: {exc2}")
            return False


def _open_model_menu(page: Page, selectors: Selectors, timeout_ms: int, trigger=None) -> bool:
    log.debug("trying to open the model dropdown/combobox")
    if selectors.model_dropdown:
        dropdown = page.locator(selectors.model_dropdown)
        count = dropdown.count()
        log.debug(f"model_dropdown selector {selectors.model_dropdown!r} matched {count}")
        if count > 0:
            if _click_element(dropdown.first, timeout_ms):
                page.wait_for_timeout(MENU_OPEN_MS)
                log.info("opened model menu via selectors.model_dropdown")
                return True

    if trigger is not None and _click_element(trigger, timeout_ms):
        page.wait_for_timeout(MENU_OPEN_MS)
        log.info(f"opened model menu via trigger {_describe_element(trigger)}")
        return True

    for frame in _frames(page):
        handle = None
        try:
            handle = frame.evaluate_handle(_FIND_OPENER_JS)
            opener = handle.as_element()
            if opener is not None and _click_element(opener, timeout_ms):
                page.wait_for_timeout(MENU_OPEN_MS)
                log.info(f"opened model menu via inferred opener in {frame.url!r}")
                return True
        except Exception as exc:
            log.debug(f"opener scan failed in {frame.url!r}: {exc}")
        finally:
            if handle is not None:
                _dispose(handle)
    log.warn("could not open a model dropdown/combobox")
    return False


def _select_model(page: Page, selectors: Selectors, model: str, timeout_ms: int) -> None:
    if not model:
        log.info("no model configured; skipping model selection")
        return

    log.info(f"selecting model {model!r} (timeout={timeout_ms}ms)")
    if _try_native_select(page, model):
        return

    if selectors.model_dropdown:
        dropdown = page.locator(selectors.model_dropdown)
        count = dropdown.count()
        log.debug(f"configured model_dropdown matched {count} node(s)")
        if count > 0:
            tag = dropdown.first.evaluate("el => (el.tagName || '').toLowerCase()")
            log.debug(f"model_dropdown tag={tag!r}")
            if tag == "select":
                try:
                    dropdown.first.select_option(value=model, timeout=timeout_ms)
                    log.info(f"selected {model!r} via <select> value")
                    return
                except Exception as exc:
                    log.debug(f"select_option(value=) failed: {exc}")
                try:
                    dropdown.first.select_option(label=model, timeout=timeout_ms)
                    log.info(f"selected {model!r} via <select> label")
                    return
                except Exception as exc:
                    log.debug(f"select_option(label=) failed: {exc}")

    deadline = time.monotonic() + timeout_ms / 1000
    opened = False
    last_trigger = None
    attempts = 0
    last_status_log = 0.0
    while time.monotonic() < deadline:
        attempts += 1
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        element = _find_model_element(page, model)
        if element is None:
            now = time.monotonic()
            if now - last_status_log >= 5:
                log.debug(
                    f"model {model!r} not in DOM yet "
                    f"(attempt={attempts}, remaining={remaining_ms}ms, menu_opened={opened})"
                )
                last_status_log = now
            if not opened:
                opened = _open_model_menu(page, selectors, timeout_ms)
            else:
                page.wait_for_timeout(POLL_MS)
            continue

        meta = _element_meta(element)
        log.debug(
            f"candidate for {model!r}: {_describe_element(element)} "
            f"visible={meta.get('visible')} inPopup={meta.get('inPopup')}"
        )
        if meta.get("inPopup") and meta.get("visible"):
            if _click_element(element, timeout_ms):
                log.info(f"clicked model option {model!r} inside dropdown")
                return
            raise ChatError(f"found {model!r} in a dropdown but could not click it")

        if meta.get("visible") and not meta.get("inPopup"):
            last_trigger = element
            if not opened:
                opened = _open_model_menu(page, selectors, timeout_ms, trigger=element)
                continue
            if _click_element(element, timeout_ms):
                log.info(f"clicked visible model control {model!r}")
                return

        if not meta.get("visible") and not opened:
            opened = _open_model_menu(page, selectors, timeout_ms, trigger=last_trigger)
            continue

        page.wait_for_timeout(POLL_MS)

    log.error(f"timed out selecting model {model!r} after {attempts} attempt(s)")
    raise ChatError(
        f"timed out selecting model {model!r} from the page DOM"
    )


def _send(page: Page, selectors: Selectors, timeout_ms: int) -> None:
    if selectors.send_button:
        log.info(f"clicking send button {selectors.send_button!r}")
        button = page.locator(selectors.send_button).first
        _wait_visible(button, timeout_ms, "send button")
        button.click(timeout=timeout_ms)
        log.debug("send button clicked")
        return
    log.info("no send_button selector; pressing Enter in the prompt")
    page.locator(selectors.prompt_input).first.press("Enter", timeout=timeout_ms)
    log.debug("Enter pressed")


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
    log.info(
        f"waiting for assistant reply selector={selector!r} "
        f"previous_count={previous_count} timeout={timeout_ms}ms idle={idle_ms}ms"
    )

    last_status_log = 0.0
    while time.monotonic() < deadline:
        count = messages.count()
        if count > previous_count:
            log.info(f"assistant message appeared (count {previous_count} -> {count})")
            break
        now = time.monotonic()
        if now - last_status_log >= 5:
            remaining = int((deadline - now) * 1000)
            log.debug(
                f"still waiting for a new assistant message "
                f"(count={count}, remaining={remaining}ms)"
            )
            last_status_log = now
        page.wait_for_timeout(POLL_MS)
    else:
        log.error(
            "no assistant message appeared "
            f"(selector={selector!r}, previous_count={previous_count}, "
            f"current_count={messages.count()})"
        )
        raise ChatError(
            "no assistant message appeared "
            f"(selector={selector!r}, previous_count={previous_count})"
        )

    last_text = ""
    last_change = time.monotonic()
    last_growth_log = 0.0
    while time.monotonic() < deadline:
        count = messages.count()
        text = messages.nth(count - 1).inner_text()
        idle_so_far = (time.monotonic() - last_change) * 1000
        if text != last_text:
            last_text = text
            last_change = time.monotonic()
            now = time.monotonic()
            if now - last_growth_log >= 1.0:
                log.debug(
                    f"reply streaming: {len(text)} chars, {count} message(s), "
                    f"preview={log.preview(text, 80)!r}"
                )
                last_growth_log = now
        elif last_text.strip() and idle_so_far >= idle_ms:
            log.info(
                f"reply idle for {int(idle_so_far)}ms; treating as complete "
                f"({len(last_text)} chars)"
            )
            return last_text.strip()
        page.wait_for_timeout(POLL_MS)

    log.error(
        "timed out waiting for the assistant reply to finish streaming "
        f"({len(last_text)} chars captured, preview={log.preview(last_text)!r})"
    )
    raise ChatError(
        "timed out waiting for the assistant reply to finish streaming "
        f"({len(last_text)} chars captured)"
    )


def submit_review(page: Page, config: BotConfig, prompt: str) -> str:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    from critique_bot.browser import describe_page, warn_if_login_page

    selectors = config.selectors
    timeout_ms = config.timeout_ms
    log.info(
        "starting chat flow "
        + log.kv(
            url=config.url,
            model=config.model or "(none)",
            timeout_ms=timeout_ms,
            idle_ms=config.idle_ms,
            prompt_chars=len(prompt),
            prompt_input=selectors.prompt_input,
            send_button=selectors.send_button or "(Enter)",
            assistant_messages=selectors.assistant_messages,
            model_dropdown=selectors.model_dropdown or "(auto)",
        )
    )
    log.debug(f"before navigation: {describe_page(page)}")

    try:
        log.info(f"navigating to {config.url}")
        page.goto(config.url, wait_until="domcontentloaded", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        log.error(f"timed out loading chat UI: {config.url} ({describe_page(page)})")
        raise ChatError(f"timed out loading chat UI: {config.url}") from exc

    log.info(f"loaded {describe_page(page)}")
    warn_if_login_page(page)
    frames = list(page.frames)
    log.debug(f"{len(frames)} frame(s): {[frame.url for frame in frames]}")

    _wait_visible(
        page.locator(selectors.prompt_input),
        timeout_ms,
        "prompt input after navigation",
    )
    _select_model(page, selectors, config.model, timeout_ms)

    previous_count = page.locator(selectors.assistant_messages).count()
    log.info(f"assistant messages already on page: {previous_count}")
    _fill_prompt(page.locator(selectors.prompt_input), prompt, timeout_ms)
    _send(page, selectors, timeout_ms)

    reply = _wait_for_reply(
        page,
        selectors.assistant_messages,
        previous_count=previous_count,
        timeout_ms=timeout_ms,
        idle_ms=config.idle_ms,
    )
    log.info(f"captured review ({len(reply)} chars)")
    return reply
