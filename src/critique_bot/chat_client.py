from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from critique_bot import log
from critique_bot.config import BotConfig, Selectors
from critique_bot.patch import strip_unsafe_controls

if TYPE_CHECKING:
    from playwright.sync_api import Frame, Locator, Page


class ChatError(RuntimeError):
    """The web chat UI did not complete a reply."""


POLL_MS = 250
MENU_OPEN_MS = 700
_FILL_DIRECT_MAX = 8_000
_FILL_CHUNK = 12_000
_FILL_SINGLE_EVAL_MAX = 48_000
_MODELISH_RE = re.compile(
    r"model|gpt|claude|gemini|grok|llama|mistral|sonnet|opus|haiku|flash|chatgpt",
    re.I,
)

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
        if (["listbox", "menu", "dialog", "list", "group", "panel"].includes(role)) return true;
        if (n.hasAttribute("popover") || n.tagName === "DIALOG") return true;
        if ((n.getAttribute("data-state") || "") === "open") return true;
        const cls = typeof n.className === "string" ? n.className.toLowerCase() : "";
        if (/(dropdown|popover|listbox|menu-list|combobox|picker|panel|overlay|popup|flyout|portal|floating)/.test(cls)) {
          return true;
        }
        const st = getComputedStyle(n);
        const z = parseInt(st.zIndex, 10);
        if ((st.position === "fixed" || st.position === "absolute") && z > 5) {
          const r = n.getBoundingClientRect();
          if (r.height > 40 && r.width > 80) return true;
        }
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

  const clickable = (el) => {
    const close = el.closest(
      'button, a, [role="option"], [role="menuitem"], [role="button"], [role="combobox"], li, [tabindex], [onclick]'
    );
    if (close) return close;
    let n = el;
    while (n && n instanceof Element) {
      const st = getComputedStyle(n);
      if (st.cursor === "pointer" || n.hasAttribute("onclick")) return n;
      const root = n.getRootNode && n.getRootNode();
      n = n.parentElement || (root && root.host) || null;
    }
    return el;
  };

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

    if (root.tagName === "DIV" || root.tagName === "SPAN") score += 8;
    const role = (root.getAttribute("role") || "").toLowerCase();
    if (role === "option" || role === "menuitem" || role === "button" || root.tagName === "BUTTON" || root.tagName === "LI") {
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

_MENU_OPEN_JS = """
() => {
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "META", "LINK"]);
  const isVisible = (el) => {
    if (!(el instanceof Element)) return false;
    const st = getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden" || Number(st.opacity) === 0) {
      return false;
    }
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  let found = false;
  const visit = (root) => {
    if (found) return;
    if (root instanceof Document) {
      if (root.documentElement) visit(root.documentElement);
      return;
    }
    if (root instanceof Element) {
      if (SKIP.has(root.tagName)) return;
      const role = (root.getAttribute("role") || "").toLowerCase();
      if (["listbox", "menu", "dialog"].includes(role) && isVisible(root)) {
        found = true;
        return;
      }
      if (["option", "menuitem"].includes(role) && isVisible(root)) {
        found = true;
        return;
      }
      if (root.hasAttribute("popover") && isVisible(root)) {
        found = true;
        return;
      }
      if ((root.getAttribute("data-state") || "") === "open" && isVisible(root)) {
        const r = root.getBoundingClientRect();
        if (r.height > 40) { found = true; return; }
      }
      const cls = typeof root.className === "string" ? root.className.toLowerCase() : "";
      if (/(dropdown|popover|listbox|menu-list|picker|panel|overlay|popup|flyout|portal|floating)/.test(cls) && isVisible(root)) {
        const r = root.getBoundingClientRect();
        if (r.height > 40 && r.width > 80) {
          found = true;
          return;
        }
      }
      const st = getComputedStyle(root);
      const z = parseInt(st.zIndex, 10);
      if (isVisible(root) && (st.position === "fixed" || st.position === "absolute") && z > 5) {
        const r = root.getBoundingClientRect();
        if (r.height > 60 && r.width > 100) {
          found = true;
          return;
        }
      }
      if (root.shadowRoot) visit(root.shadowRoot);
    }
    const children =
      root instanceof Element || root instanceof ShadowRoot
        ? Array.from(root.children)
        : [];
    for (const child of children) visit(child);
  };
  visit(document);
  return found;
}
"""

_FIND_OPENER_JS = """
(needle) => {
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "META", "LINK"]);
  const MODELISH = /model|gpt|claude|gemini|grok|llama|mistral|sonnet|opus|haiku|flash|o1|o3|o4|chatgpt/i;
  const SKIP_LABEL = /search|filter|timezone|language|locale/i;
  const target = String(needle || "").replace(/\\s+/g, " ").trim().toLowerCase();

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

  const labelOf = (el) => {
    const bits = [
      el.getAttribute("aria-label"),
      el.getAttribute("title"),
      el.getAttribute("data-testid"),
      typeof el.className === "string" ? el.className : "",
      el.id,
      el.innerText,
    ];
    return bits.filter(Boolean).join(" ").replace(/\\s+/g, " ").trim();
  };

  const asOpener = (el) => {
    const close = el.closest(
      'button, a, summary, [role="combobox"], [role="button"], [aria-haspopup], [aria-expanded], [tabindex], [onclick]'
    );
    if (close) return close;
    let n = el;
    while (n && n instanceof Element) {
      const st = getComputedStyle(n);
      if (n.tagName === "BUTTON" || n.tagName === "DIV" || n.tagName === "SPAN") {
        if (st.cursor === "pointer" || n.hasAttribute("onclick")) return n;
      }
      const root = n.getRootNode && n.getRootNode();
      n = n.parentElement || (root && root.host) || null;
    }
    return el;
  };

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
    if (!isVisible(root)) return;

    const role = (root.getAttribute("role") || "").toLowerCase();
    const hasPopup = (root.getAttribute("aria-haspopup") || "").toLowerCase();
    const expanded = (root.getAttribute("aria-expanded") || "").toLowerCase();
    const type = (root.getAttribute("type") || "").toLowerCase();
    const label = labelOf(root);
    const testid = (root.getAttribute("data-testid") || "").toLowerCase();
    if (type === "search" || SKIP_LABEL.test(root.getAttribute("placeholder") || "")) {
      return;
    }
    if (SKIP_LABEL.test(root.getAttribute("aria-label") || "")) return;

    let score = 0;
    if (role === "combobox" || role === "button") score += 40;
    if (hasPopup === "listbox" || hasPopup === "menu" || hasPopup === "dialog" || hasPopup === "true") score += 35;
    if (root.hasAttribute("aria-expanded")) score += 18;
    if (expanded === "false") score += 8;
    if (root.tagName === "BUTTON" || root.tagName === "SUMMARY") score += 24;
    if (root.tagName === "DIV" || root.tagName === "SPAN") {
      const st = getComputedStyle(root);
      if (st.cursor === "pointer" || root.hasAttribute("onclick") || root.hasAttribute("tabindex")) {
        score += 22;
      }
    }
    if (MODELISH.test(label) || MODELISH.test(testid)) score += 32;
    if (target && label.toLowerCase().includes(target)) score += 12;
    if (/model/.test(testid) || /model/.test(typeof root.className === "string" ? root.className : "")) {
      score += 20;
    }
    if (score < 22) return;
    candidates.push({ el: asOpener(root), score });
  };

  visit(document);
  candidates.sort((a, b) => b.score - a.score);
  return candidates.length ? candidates[0].el : null;
}
"""

_FIND_IDENTIFIER_JS = """
(needle) => {
  const target = String(needle || "").replace(/\\s+/g, " ").trim().toLowerCase();
  if (!target) return null;
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "META", "LINK"]);

  const isVisible = (el) => {
    if (!(el instanceof Element)) return false;
    const st = getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden" || Number(st.opacity) === 0) {
      return false;
    }
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const asOpener = (el) => {
    const close = el.closest(
      'button, a, summary, [role="combobox"], [role="button"], [aria-haspopup], [aria-expanded], [tabindex], [onclick]'
    );
    if (close) return close;
    let n = el;
    while (n && n instanceof Element) {
      const st = getComputedStyle(n);
      if (st.cursor === "pointer" || n.hasAttribute("onclick")) return n;
      const root = n.getRootNode && n.getRootNode();
      n = n.parentElement || (root && root.host) || null;
    }
    return el;
  };

  const hayOf = (el) => {
    const cls = typeof el.className === "string" ? el.className : "";
    return [
      el.getAttribute("aria-label"),
      el.getAttribute("title"),
      el.getAttribute("data-testid"),
      el.getAttribute("data-id"),
      el.id,
      cls,
      el.innerText,
    ]
      .filter(Boolean)
      .join(" ")
      .replace(/\\s+/g, " ")
      .trim()
      .toLowerCase();
  };

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
    if (!isVisible(root)) return;
    const hay = hayOf(root);
    if (!hay.includes(target)) return;
    let score = 40;
    const aria = (root.getAttribute("aria-label") || "").trim().toLowerCase();
    const testid = (root.getAttribute("data-testid") || "").trim().toLowerCase();
    const id = (root.id || "").trim().toLowerCase();
    const text = (root.innerText || "").replace(/\\s+/g, " ").trim().toLowerCase();
    if (aria === target || testid === target || id === target || text === target) score += 80;
    else if (aria.includes(target) || testid.includes(target) || id.includes(target)) score += 40;
    else if (text.includes(target)) score += 20;
    if (root.tagName === "BUTTON" || (root.getAttribute("role") || "") === "button") score += 25;
    candidates.push({ el: asOpener(root), score });
  };

  visit(document);
  candidates.sort((a, b) => b.score - a.score);
  return candidates.length ? candidates[0].el : null;
}
"""

_OPENER_LOCATORS = (
    '[data-testid*="model" i]',
    '[class*="model-picker" i]',
    '[class*="model-select" i]',
    '[class*="ModelSelector"]',
    'button[class*="model" i]',
    'div[class*="model" i]',
    'div[role="button"]',
    '[role="button"][aria-expanded]',
    '[role="button"][aria-haspopup]',
    "button[aria-haspopup]",
    "button[aria-expanded]",
    '[role="combobox"]',
    '[aria-haspopup="listbox"]',
    '[aria-haspopup="menu"]',
    '[aria-haspopup="dialog"]',
)


_SET_PROMPT_JS = """
(el, args) => {
  const mode = args.mode;
  const chunk = args.chunk || "";
  const dispatch = () => {
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const setValue = (value) => {
    if (el.isContentEditable) {
      el.textContent = value;
      return;
    }
    const proto = el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : (el instanceof HTMLInputElement ? HTMLInputElement.prototype : null);
    const setter = proto && Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value);
    else if ("value" in el) el.value = value;
    else el.textContent = value;
  };
  const getValue = () => {
    if (el.isContentEditable) return el.textContent || "";
    if ("value" in el) return el.value || "";
    return el.textContent || "";
  };
  if (mode === "set") {
    setValue(chunk);
    dispatch();
    return getValue().length;
  }
  if (mode === "append") {
    setValue(getValue() + chunk);
    return getValue().length;
  }
  if (mode === "dispatch") {
    dispatch();
    return getValue().length;
  }
  return getValue().length;
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


def _fill_prompt_via_dom(locator: Locator, text: str) -> None:
    if len(text) <= _FILL_SINGLE_EVAL_MAX:
        locator.evaluate(_SET_PROMPT_JS, {"mode": "set", "chunk": text})
        log.debug("prompt filled via DOM value setter")
        return
    log.info(f"filling prompt in {_FILL_CHUNK}-char chunks ({len(text)} chars)")
    locator.evaluate(_SET_PROMPT_JS, {"mode": "set", "chunk": ""})
    for index in range(0, len(text), _FILL_CHUNK):
        chunk = text[index : index + _FILL_CHUNK]
        locator.evaluate(_SET_PROMPT_JS, {"mode": "append", "chunk": chunk})
    locator.evaluate(_SET_PROMPT_JS, {"mode": "dispatch", "chunk": ""})
    log.debug("prompt filled via chunked DOM value setter")


def _fill_prompt(locator: Locator, text: str, timeout_ms: int) -> None:
    locator = locator.first
    text = strip_unsafe_controls(text)
    log.info(f"filling prompt ({len(text)} chars, preview={log.preview(text)!r})")
    _wait_visible(locator, timeout_ms, "prompt input")
    if len(text) <= _FILL_DIRECT_MAX:
        try:
            locator.fill(text, timeout=timeout_ms)
            log.debug("prompt filled via locator.fill")
            return
        except Exception as exc:
            log.warn(f"locator.fill failed ({exc}); falling back to DOM value setter")
        _fill_prompt_via_dom(locator, text)
        return

    log.info(
        f"skipping locator.fill for large prompt ({len(text)} chars); "
        "using DOM setter so the page does not freeze"
    )
    try:
        _fill_prompt_via_dom(locator, text)
        return
    except Exception as exc:
        log.warn(f"DOM setter failed ({exc}); trying locator.fill as last resort")
    try:
        locator.fill(text, timeout=timeout_ms)
        log.debug("prompt filled via locator.fill after DOM setter failed")
    except Exception as exc:
        raise ChatError(
            f"could not paste prompt ({len(text)} chars) into the chat input: {exc}"
        ) from exc


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
              const role = (el.getAttribute("role") || "").toLowerCase();
              const visible = st.display !== "none" && st.visibility !== "hidden"
                && Number(st.opacity) !== 0 && r.width > 0 && r.height > 0;
              let n = el;
              let inPopup = false;
              while (n) {
                if (n instanceof Element) {
                  const nRole = (n.getAttribute("role") || "").toLowerCase();
                  if (["listbox", "menu", "dialog", "list", "group"].includes(nRole)
                      || n.hasAttribute("popover") || n.tagName === "DIALOG") {
                    inPopup = true;
                    break;
                  }
                  if ((n.getAttribute("data-state") || "") === "open") {
                    inPopup = true;
                    break;
                  }
                  const cls = typeof n.className === "string" ? n.className.toLowerCase() : "";
                  if (/(dropdown|popover|listbox|menu-list|combobox|picker|panel|overlay|popup|flyout|portal|floating)/.test(cls)) {
                    inPopup = true;
                    break;
                  }
                  const ns = getComputedStyle(n);
                  const z = parseInt(ns.zIndex, 10);
                  if ((ns.position === "fixed" || ns.position === "absolute") && z > 5) {
                    const nr = n.getBoundingClientRect();
                    if (nr.height > 40 && nr.width > 80) {
                      inPopup = true;
                      break;
                    }
                  }
                }
                const root = n.getRootNode && n.getRootNode();
                n = n.parentElement || (root && root.host) || null;
              }
              const isControl = el.tagName === "BUTTON" || el.tagName === "A"
                || role === "combobox" || role === "button"
                || el.hasAttribute("aria-expanded") || el.hasAttribute("aria-haspopup")
                || st.cursor === "pointer";
              return { visible, inPopup, isControl };
            }"""
        )
    except Exception:
        return {"visible": False, "inPopup": False, "isControl": False}


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


def _text_looks_modelish(text: str, model: str) -> bool:
    compact = " ".join(str(text or "").split())
    if not compact:
        return False
    if model and model.lower() in compact.lower():
        return True
    return bool(_MODELISH_RE.search(compact))


def _click_modelish_opener(page: Page, click_ms: int, model: str) -> bool:
    loc = page.locator("button, [role='button'], div[tabindex], div[onclick], span[tabindex]")
    try:
        count = loc.count()
    except Exception as exc:
        log.debug(f"modelish opener scan failed: {exc}")
        return False
    limit = min(count, 40)
    for index in range(limit):
        item = loc.nth(index)
        try:
            if not item.is_visible():
                continue
            label = " ".join(
                [
                    item.inner_text() or "",
                    item.get_attribute("aria-label") or "",
                    item.get_attribute("title") or "",
                ]
            )
        except Exception:
            continue
        if not _text_looks_modelish(label, model):
            continue
        log.debug(f"modelish opener candidate {index}: {log.preview(label, 80)!r}")
        if _click_element(item, click_ms):
            page.wait_for_timeout(MENU_OPEN_MS)
            log.info(f"clicked button/div model opener {log.preview(label, 60)!r}")
            return True
    return False


def _click_configured_option(page: Page, selectors: Selectors, model: str, timeout_ms: int) -> bool:
    if not selectors.model_option:
        return False
    loc = page.locator(selectors.model_option)
    try:
        count = loc.count()
    except Exception as exc:
        log.debug(f"model_option selector failed: {exc}")
        return False
    if count <= 0:
        return False
    matched = loc.filter(has_text=model)
    target = matched.first if matched.count() > 0 else loc.first
    try:
        if not target.is_visible():
            return False
    except Exception:
        return False
    if _click_element(target, timeout_ms):
        log.info(f"clicked model option via selectors.model_option ({model!r})")
        return True
    return False


def _click_timeout(timeout_ms: int) -> int:
    return min(max(timeout_ms, 1), 8_000)


def _looks_like_locator(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if text.startswith((".", "#", "[", "/", "xpath=", "text=", "internal:")):
        return True
    return bool(re.match(r"^[a-zA-Z][\w-]*(\[|#|\.|:)", text))


def _has_pinned_opener(selectors: Selectors) -> bool:
    return bool(selectors.model_dropdown_identifier or selectors.model_dropdown)


def _click_dropdown_identifier(page: Page, identifier: str, click_ms: int) -> bool:
    log.info(f"finding model dropdown by identifier {identifier!r}")
    if _looks_like_locator(identifier):
        loc = page.locator(identifier)
        try:
            count = loc.count()
        except Exception as exc:
            log.debug(f"identifier locator {identifier!r} failed: {exc}")
            count = 0
        log.debug(f"identifier as locator matched {count}")
        if count > 0 and _click_element(loc.first, click_ms):
            page.wait_for_timeout(MENU_OPEN_MS)
            log.info(f"clicked model_dropdown_identifier locator {identifier!r}")
            return True

    try:
        loc = page.get_by_role("button", name=re.compile(re.escape(identifier), re.I))
        if loc.count() > 0 and loc.first.is_visible() and _click_element(loc.first, click_ms):
            page.wait_for_timeout(MENU_OPEN_MS)
            log.info(f"clicked model_dropdown_identifier button name {identifier!r}")
            return True
    except Exception as exc:
        log.debug(f"get_by_role(button, name={identifier!r}) failed: {exc}")

    try:
        loc = page.get_by_label(identifier, exact=False)
        if loc.count() > 0 and loc.first.is_visible() and _click_element(loc.first, click_ms):
            page.wait_for_timeout(MENU_OPEN_MS)
            log.info(f"clicked model_dropdown_identifier label {identifier!r}")
            return True
    except Exception as exc:
        log.debug(f"get_by_label({identifier!r}) failed: {exc}")

    for frame in _frames(page):
        handle = None
        try:
            handle = frame.evaluate_handle(_FIND_IDENTIFIER_JS, identifier)
            opener = handle.as_element()
            if opener is not None and _click_element(opener, click_ms):
                page.wait_for_timeout(MENU_OPEN_MS)
                log.info(
                    f"clicked model_dropdown_identifier {identifier!r} "
                    f"in {frame.url!r}: {_describe_element(opener)}"
                )
                return True
        except Exception as exc:
            log.debug(f"identifier DOM scan failed in {frame.url!r}: {exc}")
        finally:
            if handle is not None:
                _dispose(handle)
    log.warn(f"no control matched model_dropdown_identifier {identifier!r}")
    return False


def _menu_looks_open(page: Page) -> bool:
    for frame in _frames(page):
        try:
            if frame.evaluate(_MENU_OPEN_JS):
                return True
        except Exception as exc:
            log.debug(f"menu-open scan failed in {frame.url!r}: {exc}")
    return False


def _open_model_menu(
    page: Page,
    selectors: Selectors,
    timeout_ms: int,
    trigger=None,
    model: str = "",
) -> bool:
    log.debug("trying to open the model picker (button/div + panel)")
    click_ms = _click_timeout(timeout_ms)
    pinned = _has_pinned_opener(selectors)

    if selectors.model_dropdown_identifier:
        if _click_dropdown_identifier(page, selectors.model_dropdown_identifier, click_ms):
            return True
        log.warn(
            "model_dropdown_identifier did not match; not clicking other buttons"
        )
        return False

    if selectors.model_dropdown:
        dropdown = page.locator(selectors.model_dropdown)
        count = dropdown.count()
        log.debug(f"model_dropdown selector {selectors.model_dropdown!r} matched {count}")
        if count > 0:
            if _click_element(dropdown.first, click_ms):
                page.wait_for_timeout(MENU_OPEN_MS)
                log.info("clicked selectors.model_dropdown")
                return True
        if pinned:
            log.warn("selectors.model_dropdown did not open the picker; not clicking other buttons")
            return False

    for frame in _frames(page):
        handle = None
        try:
            handle = frame.evaluate_handle(_FIND_OPENER_JS, model)
            opener = handle.as_element()
            if opener is not None and _click_element(opener, click_ms):
                page.wait_for_timeout(MENU_OPEN_MS)
                log.info(f"clicked inferred model opener in {frame.url!r}")
                return True
        except Exception as exc:
            log.debug(f"opener scan failed in {frame.url!r}: {exc}")
        finally:
            if handle is not None:
                _dispose(handle)

    if _click_modelish_opener(page, click_ms, model):
        return True

    for sel in _OPENER_LOCATORS:
        loc = page.locator(sel)
        try:
            count = loc.count()
        except Exception as exc:
            log.debug(f"opener locator {sel!r} failed: {exc}")
            continue
        if count <= 0:
            continue
        log.debug(f"opener locator {sel!r} matched {count}")
        if _click_element(loc.first, click_ms):
            page.wait_for_timeout(MENU_OPEN_MS)
            log.info(f"clicked model opener {sel!r}")
            return True

    if trigger is not None and _click_element(trigger, click_ms):
        page.wait_for_timeout(MENU_OPEN_MS)
        log.info(f"clicked model trigger {_describe_element(trigger)}")
        return True

    log.warn("could not open a model picker button/panel")
    return False


def _select_model(page: Page, selectors: Selectors, model: str, timeout_ms: int) -> None:
    if not model:
        log.info("no model configured; skipping model selection")
        return

    log.info(f"selecting model {model!r} from a button/panel picker (timeout={timeout_ms}ms)")
    if selectors.model_dropdown:
        dropdown = page.locator(selectors.model_dropdown)
        count = dropdown.count()
        log.debug(f"configured model_dropdown matched {count} node(s)")
        if count > 0:
            tag = dropdown.first.evaluate("el => (el.tagName || '').toLowerCase()")
            log.debug(f"model_dropdown tag={tag!r}")
            if tag == "select":
                try:
                    dropdown.first.select_option(label=model, timeout=_click_timeout(timeout_ms))
                    log.info(f"selected {model!r} via <select> label")
                    return
                except Exception as exc:
                    log.debug(f"select_option(label=) failed: {exc}")

    deadline = time.monotonic() + timeout_ms / 1000
    last_trigger = None
    last_open_try = 0.0
    attempts = 0
    last_status_log = 0.0
    click_ms = _click_timeout(timeout_ms)
    while time.monotonic() < deadline:
        attempts += 1
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if _click_configured_option(page, selectors, model, click_ms):
            return
        menu_open = _menu_looks_open(page)
        element = _find_model_element(page, model)
        if element is not None:
            meta = _element_meta(element)
            log.debug(
                f"candidate for {model!r}: {_describe_element(element)} "
                f"visible={meta.get('visible')} inPopup={meta.get('inPopup')} "
                f"isControl={meta.get('isControl')} menu_open={menu_open}"
            )
            if meta.get("visible") and (
                meta.get("inPopup") or menu_open or (last_open_try > 0 and not meta.get("isControl"))
            ):
                if _click_element(element, click_ms):
                    log.info(f"clicked model {model!r} in the open panel")
                    return
                raise ChatError(f"found {model!r} in the panel but could not click it")
            if (
                meta.get("visible")
                and meta.get("isControl")
                and not _has_pinned_opener(selectors)
            ):
                last_trigger = element

        if (menu_open or last_open_try > 0) and _click_visible_model_label(page, model, click_ms):
            return

        now = time.monotonic()
        if now - last_status_log >= 5:
            log.debug(
                f"model {model!r} waiting "
                f"(attempt={attempts}, remaining={remaining_ms}ms, menu_open={menu_open})"
            )
            last_status_log = now

        if not menu_open and now - last_open_try >= 0.8:
            if last_open_try > 0:
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(150)
                except Exception:
                    pass
            _open_model_menu(
                page,
                selectors,
                timeout_ms,
                trigger=last_trigger,
                model=model,
            )
            last_open_try = now

        page.wait_for_timeout(POLL_MS)

    if _try_native_select(page, model):
        return

    log.error(f"timed out selecting model {model!r} after {attempts} attempt(s)")
    raise ChatError(
        f"timed out selecting model {model!r} from the page DOM"
    )


def _click_visible_model_label(page: Page, model: str, timeout_ms: int) -> bool:
    loc = page.get_by_text(model, exact=True)
    try:
        count = loc.count()
    except Exception as exc:
        log.debug(f"get_by_text({model!r}) failed: {exc}")
        return False
    for index in range(count - 1, -1, -1):
        item = loc.nth(index)
        try:
            if not item.is_visible():
                continue
        except Exception:
            continue
        if _click_element(item, timeout_ms):
            log.info(f"clicked visible label {model!r}")
            return True
    return False


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


def prepare_chat(page: Page, config: BotConfig) -> None:
    from critique_bot.browser import BrowserError, describe_page, navigate, warn_if_login_page

    selectors = config.selectors
    timeout_ms = config.timeout_ms
    log.info(
        "starting chat flow "
        + log.kv(
            url=config.url,
            model=config.model or "(none)",
            timeout_ms=timeout_ms,
            idle_ms=config.idle_ms,
            prompt_input=selectors.prompt_input,
            send_button=selectors.send_button or "(Enter)",
            assistant_messages=selectors.assistant_messages,
            model_dropdown=selectors.model_dropdown or "(auto)",
            model_dropdown_identifier=selectors.model_dropdown_identifier or "(none)",
            model_option=selectors.model_option or "(auto)",
        )
    )
    log.debug(f"before navigation: {describe_page(page)}")

    with log.loading("Opening chat..."):
        try:
            navigate(page, config.url, timeout_ms)
        except BrowserError as exc:
            raise ChatError(str(exc)) from exc

        warn_if_login_page(page)
        frames = list(page.frames)
        log.debug(f"{len(frames)} frame(s): {[frame.url for frame in frames]}")

        _wait_visible(
            page.locator(selectors.prompt_input),
            timeout_ms,
            "prompt input after navigation",
        )
        _select_model(page, selectors, config.model, timeout_ms)
    log.info("chat UI is ready")


def send_turn(page: Page, config: BotConfig, prompt: str) -> str:
    selectors = config.selectors
    timeout_ms = config.timeout_ms
    previous_count = page.locator(selectors.assistant_messages).count()
    log.info(
        "sending turn "
        + log.kv(prompt_chars=len(prompt), previous_messages=previous_count)
    )
    with log.loading("Waiting for assistant..."):
        _fill_prompt(page.locator(selectors.prompt_input), prompt, timeout_ms)
        _send(page, selectors, timeout_ms)

        reply = _wait_for_reply(
            page,
            selectors.assistant_messages,
            previous_count=previous_count,
            timeout_ms=timeout_ms,
            idle_ms=config.idle_ms,
        )
    log.info(f"captured reply ({len(reply)} chars)")
    return reply


def submit_review(page: Page, config: BotConfig, prompt: str) -> str:
    prepare_chat(page, config)
    return send_turn(page, config, prompt)
