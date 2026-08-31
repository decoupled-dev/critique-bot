"""Local setup UI: check the install, pick selectors by clicking, test live.

Serves a small page on 127.0.0.1 using only the standard library. The hard part
of setting this bot up is discovering the CSS selectors for someone else's chat
UI, so the page drives a real Edge window and lets you click the prompt box,
the send button, and a reply instead of hand-writing selectors.
"""

from __future__ import annotations

import json
import queue
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from critique_bot import diagnostics, log
from critique_bot.config import ConfigError, load_config

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_COMMAND_TIMEOUT = 300.0

#: Fields the click-to-pick flow knows how to fill in.
PICK_FIELDS: dict[str, dict[str, str]] = {
    "prompt_input": {
        "label": "message box you type into",
        "mode": "unique",
    },
    "send_button": {
        "label": "send button",
        "mode": "unique",
    },
    "assistant_messages": {
        "label": "assistant reply (click the reply text itself)",
        "mode": "group",
    },
    "stop_button": {
        "label": "stop-generating button (send a message first, then click it)",
        "mode": "unique",
    },
    "model_dropdown": {
        "label": "control that opens the model picker",
        "mode": "unique",
    },
}

# Ranks candidate selectors for the element the user clicked. Attribute-based
# selectors survive redeploys of a chat UI; positional ones usually do not.
_PICKER_JS = r"""
(payload) => {
  const state = (window.__critiqueBot = window.__critiqueBot || {});
  if (state.cleanup) { try { state.cleanup(); } catch (e) {} }

  const quote = (value) => '"' + String(value).replace(/["\\]/g, '\\$&') + '"';
  const looksGenerated = (value) =>
    !value ||
    /\d{4,}/.test(value) ||
    /^:r[0-9a-z]+:$/i.test(value) ||
    /^[0-9]/.test(value) ||
    value.length > 60;

  const classSelector = (el) => {
    const classes = Array.from(el.classList || []).filter(
      (name) => name.length > 1 && name.length < 30 && !looksGenerated(name)
    );
    if (!classes.length) return '';
    return el.tagName.toLowerCase() + '.' + classes.slice(0, 2).map(CSS.escape).join('.');
  };

  const candidatesFor = (el) => {
    const out = [];
    const tag = el.tagName.toLowerCase();
    for (const attr of ['data-testid', 'data-test-id', 'data-message-author-role',
                        'data-message-role', 'data-qa', 'name']) {
      const value = el.getAttribute && el.getAttribute(attr);
      if (value && !looksGenerated(value)) out.push('[' + attr + '=' + quote(value) + ']');
    }
    const id = el.getAttribute && el.getAttribute('id');
    if (id && !looksGenerated(id)) out.push('#' + CSS.escape(id));
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria && aria.length < 40) out.push(tag + '[aria-label=' + quote(aria) + ']');
    const role = el.getAttribute && el.getAttribute('role');
    if (role) out.push(tag + '[role=' + quote(role) + ']');
    if (tag === 'textarea' || tag === 'input') out.push(tag);
    if (el.isContentEditable) out.push('[contenteditable="true"]');
    const cls = classSelector(el);
    if (cls) out.push(cls);
    return out;
  };

  const score = (selector, el, mode) => {
    let nodes;
    try {
      nodes = Array.from(document.querySelectorAll(selector));
    } catch (err) {
      return null;
    }
    if (!nodes.length) return null;
    const hits = nodes.filter((node) => node === el || node.contains(el));
    if (!hits.length) return null;
    const positional = /nth-child|>/.test(selector);
    let rank = 0;
    if (selector.startsWith('[data-')) rank += 40;
    if (selector.includes('aria-label')) rank += 20;
    if (selector.startsWith('#')) rank += 15;
    if (positional) rank -= 30;
    if (mode === 'unique') {
      rank += nodes.length === 1 ? 30 : -10 * Math.min(nodes.length, 5);
    } else {
      rank += nodes.length > 1 ? 20 : 0;
    }
    rank -= Math.min(selector.length, 80) / 10;
    return { selector, matches: nodes.length, rank };
  };

  const build = (target, mode) => {
    const seen = new Set();
    const scored = [];
    let el = target;
    for (let depth = 0; el && depth < 4; depth += 1, el = el.parentElement) {
      for (const selector of candidatesFor(el)) {
        if (seen.has(selector)) continue;
        seen.add(selector);
        const result = score(selector, target, mode);
        if (result) scored.push(result);
      }
    }
    scored.sort((a, b) => b.rank - a.rank);
    return scored.slice(0, 6);
  };

  const box = document.createElement('div');
  box.style.cssText =
    'position:fixed;z-index:2147483646;pointer-events:none;border:2px solid #2f81f7;' +
    'background:rgba(47,129,247,.14);border-radius:4px;';
  const banner = document.createElement('div');
  banner.textContent = 'critique-bot: click the ' + payload.label + '   (Esc to cancel)';
  banner.style.cssText =
    'position:fixed;z-index:2147483647;pointer-events:none;left:50%;top:16px;' +
    'transform:translateX(-50%);background:#1f6feb;color:#fff;padding:10px 16px;' +
    'border-radius:999px;font:600 13px/1.2 system-ui,sans-serif;' +
    'box-shadow:0 6px 24px rgba(0,0,0,.35)';
  document.body.appendChild(box);
  document.body.appendChild(banner);

  const move = (event) => {
    const el = event.target;
    if (!el || !el.getBoundingClientRect) return;
    const rect = el.getBoundingClientRect();
    box.style.left = rect.left + 'px';
    box.style.top = rect.top + 'px';
    box.style.width = rect.width + 'px';
    box.style.height = rect.height + 'px';
  };

  const finish = (result) => {
    state.cleanup();
    if (window.critiqueBotPicked) {
      try { window.critiqueBotPicked(result); } catch (e) {}
    }
  };

  const click = (event) => {
    event.preventDefault();
    event.stopPropagation();
    const options = build(event.target, payload.mode);
    finish({ field: payload.field, cancelled: false, options });
  };

  const key = (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      finish({ field: payload.field, cancelled: true, options: [] });
    }
  };

  state.cleanup = () => {
    document.removeEventListener('mousemove', move, true);
    document.removeEventListener('click', click, true);
    document.removeEventListener('keydown', key, true);
    box.remove();
    banner.remove();
    state.cleanup = null;
  };

  document.addEventListener('mousemove', move, true);
  document.addEventListener('click', click, true);
  document.addEventListener('keydown', key, true);
  return true;
}
"""


class BrowserBridge:
    """Owns one Playwright page on a dedicated thread.

    Playwright's sync API is thread-affine, so every page operation is funnelled
    through a command queue and executed on the thread that opened the browser.
    """

    def __init__(self) -> None:
        self._commands: queue.Queue[tuple[Callable[[Any], Any], queue.Queue]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self._lock = threading.Lock()
        self.pick: dict[str, Any] | None = None
        self.url = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, config: Any) -> None:
        if self.running:
            return
        self._ready.clear()
        self._stop.clear()
        self._error = None
        self.url = config.url
        self._thread = threading.Thread(
            target=self._run, args=(config,), name="critique-setup-browser", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=180):
            raise RuntimeError("timed out opening the browser")
        if self._error is not None:
            raise RuntimeError(str(self._error))

    def _on_pick(self, result: Any) -> None:
        with self._lock:
            self.pick = result if isinstance(result, dict) else None

    def current_pick(self) -> dict[str, Any] | None:
        with self._lock:
            return self.pick

    def clear_pick(self) -> None:
        with self._lock:
            self.pick = None

    def _run(self, config: Any) -> None:
        from critique_bot.browser import launch_edge

        try:
            with launch_edge(
                headed=True,
                storage_state=config.storage_state,
                user_data_dir=config.user_data_dir,
                cdp_url=config.cdp_url,
                start_url=config.url,
                timeout_ms=config.timeout_ms,
            ) as page:
                try:
                    page.expose_function("critiqueBotPicked", self._on_pick)
                except Exception as exc:
                    log.debug(f"picker callback already registered: {exc}")
                self._ready.set()
                while not self._stop.is_set():
                    try:
                        func, reply = self._commands.get(timeout=0.2)
                    except queue.Empty:
                        # Keep Playwright pumping so page callbacks arrive.
                        try:
                            page.wait_for_timeout(50)
                        except Exception:
                            break
                        continue
                    try:
                        reply.put(("ok", func(page)))
                    except BaseException as exc:  # noqa: BLE001 - reported to caller
                        reply.put(("error", exc))
        except BaseException as exc:  # noqa: BLE001 - reported to caller
            self._error = exc
            log.debug(f"setup browser thread ended: {exc}")
        finally:
            self._ready.set()

    def call(self, func: Callable[[Any], Any], *, timeout: float = _COMMAND_TIMEOUT) -> Any:
        if not self.running:
            raise RuntimeError("the browser is not open")
        reply: queue.Queue = queue.Queue(maxsize=1)
        self._commands.put((func, reply))
        try:
            kind, value = reply.get(timeout=timeout)
        except queue.Empty as exc:
            raise RuntimeError(f"browser command timed out after {timeout:.0f}s") from exc
        if kind == "error":
            raise RuntimeError(str(value))
        return value

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=30)
        self._thread = None


class SetupState:
    """Config file plus the shared browser, guarded for concurrent requests."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)
        self.bridge = BrowserBridge()
        self.lock = threading.RLock()

    def raw_config(self) -> dict[str, Any]:
        return diagnostics.config_snapshot(self.config_path)

    def load(self):
        return load_config(self.config_path)

    def save(self, data: dict[str, Any]) -> dict[str, Any]:
        """Merge edits into config.json, then report whether it still loads."""
        with self.lock:
            current = self.raw_config()
            selectors = dict(current.get("selectors") or {})
            incoming_selectors = data.pop("selectors", None)
            if isinstance(incoming_selectors, dict):
                selectors.update(
                    {key: value for key, value in incoming_selectors.items()}
                )
            current.update(data)
            if selectors:
                current["selectors"] = selectors
            self.config_path.write_text(
                json.dumps(current, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        try:
            self.load()
        except ConfigError as exc:
            return {"saved": True, "valid": False, "error": str(exc)}
        return {"saved": True, "valid": True, "error": ""}


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, default=str) + "\n").encode("utf-8")


def make_handler(state: SetupState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "critique-bot-setup"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("setup ui " + (fmt % args))

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if self.path == "/api/state":
                self._json(_state_payload(state))
                return
            if self.path == "/api/pick":
                self._json(
                    {
                        "pick": state.bridge.current_pick(),
                        "browser": state.bridge.running,
                    }
                )
                return
            self._json({"error": "not found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            body = self._body()
            try:
                if self.path == "/api/config":
                    self._json(state.save(body.get("config") or {}))
                    return
                if self.path == "/api/browser/open":
                    self._json(_open_browser(state))
                    return
                if self.path == "/api/browser/close":
                    state.bridge.stop()
                    self._json({"browser": False})
                    return
                if self.path == "/api/pick":
                    self._json(_arm_picker(state, str(body.get("field") or "")))
                    return
                if self.path == "/api/validate":
                    self._json(_validate(state))
                    return
                if self.path == "/api/test":
                    self._json(_round_trip(state, str(body.get("prompt") or "")))
                    return
                if self.path == "/api/doctor":
                    self._json(_doctor(state))
                    return
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                log.debug(f"setup request {self.path} failed: {exc}")
                self._json({"error": str(exc)}, status=200)
                return
            self._json({"error": "not found"}, status=404)

    return Handler


def _state_payload(state: SetupState) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "config_path": str(state.config_path.resolve()),
        "config": state.raw_config(),
        "env": diagnostics.env_snapshot(),
        "browser": state.bridge.running,
        "fields": PICK_FIELDS,
        "error": "",
    }
    try:
        config = state.load()
    except ConfigError as exc:
        payload["error"] = str(exc)
        return payload
    payload["checks"] = diagnostics.static_checks(
        config, config_path=state.config_path
    ).to_dict()
    try:
        from critique_bot.queue import FileQueue

        payload["queue"] = FileQueue(Path(config.queue_dir)).snapshot(recent=5)
    except Exception as exc:  # noqa: BLE001 - queue is optional here
        payload["queue"] = {"error": str(exc)}
    return payload


def _open_browser(state: SetupState) -> dict[str, Any]:
    config = state.load()
    if not config.uses_browser:
        return {"error": "the setup browser is only for the browser backend"}
    state.bridge.start(config)
    return {"browser": True, "url": config.url}


def _arm_picker(state: SetupState, field: str) -> dict[str, Any]:
    spec = PICK_FIELDS.get(field)
    if spec is None:
        return {"error": f"unknown field {field!r}"}
    if not state.bridge.running:
        return {"error": "open the browser first"}
    state.bridge.clear_pick()
    state.bridge.call(
        lambda page: page.evaluate(
            _PICKER_JS,
            {"field": field, "label": spec["label"], "mode": spec["mode"]},
        ),
        timeout=30,
    )
    return {"armed": field}


def _validate(state: SetupState) -> dict[str, Any]:
    if not state.bridge.running:
        return {"error": "open the browser first"}
    config = state.load()
    checks = state.bridge.call(
        lambda page: [
            {"name": check.name, "status": check.status, "detail": check.detail,
             "hint": check.hint}
            for check in (
                [diagnostics.probe_login(page)]
                + diagnostics.probe_selectors(page, config.selectors)
            )
        ],
        timeout=120,
    )
    return {"checks": checks}


def _round_trip(state: SetupState, prompt: str) -> dict[str, Any]:
    if not state.bridge.running:
        return {"error": "open the browser first"}
    config = state.load()
    text = prompt.strip() or diagnostics.DEFAULT_PROBE_PROMPT

    def run(page: Any) -> dict[str, Any]:
        from critique_bot.chat_client import ChatError, prepare_chat

        try:
            prepare_chat(page, config)
        except ChatError as exc:
            return {"status": diagnostics.FAIL, "detail": str(exc)}
        check = diagnostics.probe_round_trip(page, config, prompt=text)
        return {"status": check.status, "detail": check.detail, "hint": check.hint}

    return {"result": state.bridge.call(run, timeout=_COMMAND_TIMEOUT)}


def _doctor(state: SetupState) -> dict[str, Any]:
    config = state.load()
    return diagnostics.static_checks(config, config_path=state.config_path).to_dict()


def run_setup(
    config_path: Path,
    *,
    port: int = DEFAULT_PORT,
    open_page: bool = True,
) -> int:
    state = SetupState(Path(config_path))
    if not state.config_path.is_file():
        print(
            f"error: config file not found: {state.config_path}\n"
            "Copy one of the config.*.example.json files first.",
            flush=True,
        )
        return 1
    handler = make_handler(state)
    try:
        server = ThreadingHTTPServer((BIND_HOST, port), handler)
    except OSError as exc:
        print(f"error: cannot listen on {BIND_HOST}:{port}: {exc}", flush=True)
        return 1
    url = f"http://{BIND_HOST}:{server.server_port}/"
    print(f"critique-bot setup UI: {url}   (Ctrl-C to stop)", flush=True)
    if open_page:
        try:
            webbrowser.open(url)
        except Exception as exc:
            log.debug(f"could not open a browser window: {exc}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        state.bridge.stop()
    return 0


PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>critique-bot setup</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0d1117; --panel: #161b22; --line: #30363d; --text: #e6edf3;
    --muted: #9198a1; --accent: #2f81f7; --ok: #3fb950; --warn: #d29922;
    --fail: #f85149;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  }
  header {
    padding: 24px 32px; border-bottom: 1px solid var(--line);
    display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap;
  }
  h1 { font-size: 20px; margin: 0; letter-spacing: -0.01em; }
  header code { color: var(--muted); font-size: 13px; }
  main { max-width: 1080px; margin: 0 auto; padding: 28px 32px 80px; }
  section {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 22px 24px; margin-bottom: 20px;
  }
  h2 { font-size: 15px; margin: 0 0 4px; letter-spacing: -0.01em; }
  .sub { color: var(--muted); font-size: 13px; margin: 0 0 18px; }
  button {
    background: var(--accent); color: #fff; border: 0; border-radius: 7px;
    padding: 9px 15px; font: 600 13px/1 inherit; cursor: pointer;
  }
  button.ghost { background: transparent; border: 1px solid var(--line); color: var(--text); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button:hover:not(:disabled) { filter: brightness(1.12); }
  .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .field { display: grid; grid-template-columns: 190px 1fr auto; gap: 12px;
           align-items: center; margin-bottom: 10px; }
  .field label { color: var(--muted); font-size: 13px; }
  input[type=text] {
    width: 100%; background: #0d1117; border: 1px solid var(--line); color: var(--text);
    border-radius: 7px; padding: 8px 11px; font: 13px/1.4 ui-monospace, SFMono-Regular, monospace;
  }
  input[type=text]:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  .check { display: flex; gap: 12px; padding: 7px 0; border-top: 1px solid var(--line); }
  .check:first-child { border-top: 0; }
  .badge {
    flex: none; width: 52px; text-align: center; border-radius: 5px; height: 21px;
    font: 700 10px/21px ui-monospace, monospace; letter-spacing: .06em;
  }
  .ok { background: rgba(63,185,80,.16); color: var(--ok); }
  .warn { background: rgba(210,153,34,.16); color: var(--warn); }
  .fail { background: rgba(248,81,73,.16); color: var(--fail); }
  .skip { background: rgba(145,152,161,.16); color: var(--muted); }
  .check-body { min-width: 0; }
  .check-name { font: 600 13px/1.5 ui-monospace, monospace; }
  .check-detail { color: var(--muted); font-size: 13px; word-break: break-word; }
  .check-hint { color: var(--warn); font-size: 12.5px; margin-top: 2px; }
  .options { margin-top: 8px; display: grid; gap: 6px; }
  .option {
    display: flex; justify-content: space-between; gap: 12px; align-items: center;
    border: 1px solid var(--line); border-radius: 7px; padding: 7px 11px;
    font: 12.5px/1.4 ui-monospace, monospace; cursor: pointer; background: #0d1117;
  }
  .option:hover { border-color: var(--accent); }
  .option span:last-child { color: var(--muted); flex: none; font-size: 11.5px; }
  .status { color: var(--muted); font-size: 13px; min-height: 20px; }
  .pill { font: 600 11px/18px ui-monospace, monospace; padding: 0 9px;
          border-radius: 999px; background: rgba(145,152,161,.16); color: var(--muted); }
  .pill.live { background: rgba(63,185,80,.16); color: var(--ok); }
  pre { background:#0d1117; border:1px solid var(--line); border-radius:8px;
        padding:12px; overflow:auto; font-size:12.5px; margin:0; }
</style>
</head>
<body>
<header>
  <h1>critique-bot setup</h1>
  <code id="configPath"></code>
  <span class="pill" id="browserPill">browser closed</span>
</header>
<main>
  <section>
    <h2>1. Environment</h2>
    <p class="sub">Everything that can be checked without opening a browser.</p>
    <div id="checks"></div>
    <div class="row" style="margin-top:16px">
      <button class="ghost" onclick="refresh()">Re-check</button>
    </div>
    <div id="configError" class="check-hint"></div>
  </section>

  <section>
    <h2>2. Chat UI and selectors</h2>
    <p class="sub">
      Open Edge, sign in if asked, then click <b>Pick</b> and click the element on
      the page. Selectors are saved to your config file.
    </p>
    <div class="row" style="margin-bottom:18px">
      <button id="openBtn" onclick="openBrowser()">Open browser</button>
      <button class="ghost" onclick="closeBrowser()">Close browser</button>
      <button class="ghost" onclick="validate()">Validate selectors</button>
      <span class="status" id="browserStatus"></span>
    </div>
    <div class="field">
      <label for="url">Chat URL</label>
      <input type="text" id="url" placeholder="https://chatgpt.com/" />
      <span></span>
    </div>
    <div class="field">
      <label for="model">Model label</label>
      <input type="text" id="model" placeholder="GPT-5.1 (blank = leave as is)" />
      <span></span>
    </div>
    <div id="selectorFields"></div>
    <div class="row" style="margin-top:16px">
      <button onclick="save()">Save config</button>
      <span class="status" id="saveStatus"></span>
    </div>
    <div id="pickOptions"></div>
    <div id="validateResults" style="margin-top:16px"></div>
  </section>

  <section>
    <h2>3. Live test</h2>
    <p class="sub">
      Sends a real prompt and waits for a real reply. This is the check that
      proves the selectors work end to end.
    </p>
    <div class="row">
      <button onclick="runTest()" id="testBtn">Send test prompt</button>
      <span class="status" id="testStatus"></span>
    </div>
    <div id="testResult" style="margin-top:14px"></div>
  </section>

  <section>
    <h2>4. Queue and worker</h2>
    <p class="sub">What CI sees when it submits a review.</p>
    <pre id="queue">-</pre>
  </section>
</main>
<script>
const SELECTOR_FIELDS = [
  ["prompt_input", "Prompt input"],
  ["send_button", "Send button"],
  ["assistant_messages", "Assistant reply"],
  ["stop_button", "Stop button"],
  ["model_dropdown", "Model dropdown"],
];
let pickTimer = null;
let armedField = "";

const el = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, body) {
  const options = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) };
  const response = await fetch(path, options);
  return response.json();
}

function renderChecks(target, checks) {
  if (!checks || !checks.length) { target.innerHTML = ""; return; }
  target.innerHTML = checks.map((check) => `
    <div class="check">
      <span class="badge ${esc(check.status)}">${esc(check.status)}</span>
      <div class="check-body">
        <div class="check-name">${esc(check.name)}</div>
        <div class="check-detail">${esc(check.detail)}</div>
        ${check.hint ? `<div class="check-hint">${esc(check.hint)}</div>` : ""}
      </div>
    </div>`).join("");
}

function renderSelectorFields(config) {
  const selectors = (config && config.selectors) || {};
  el("selectorFields").innerHTML = SELECTOR_FIELDS.map(([key, label]) => `
    <div class="field">
      <label for="sel_${key}">${esc(label)}</label>
      <input type="text" id="sel_${key}" value="${esc(selectors[key] || "")}" />
      <button class="ghost" onclick="pick('${key}')">Pick</button>
    </div>`).join("");
}

async function refresh() {
  const state = await api("/api/state");
  el("configPath").textContent = state.config_path || "";
  el("configError").textContent = state.error || "";
  renderChecks(el("checks"), (state.checks || {}).checks);
  const config = state.config || {};
  if (document.activeElement !== el("url")) el("url").value = config.url || "";
  if (document.activeElement !== el("model")) el("model").value = config.model || "";
  if (!document.querySelector("#selectorFields input")) renderSelectorFields(config);
  el("queue").textContent = JSON.stringify(state.queue || {}, null, 2);
  setBrowser(state.browser);
}

function setBrowser(live) {
  el("browserPill").textContent = live ? "browser open" : "browser closed";
  el("browserPill").className = live ? "pill live" : "pill";
  el("openBtn").disabled = !!live;
}

async function openBrowser() {
  el("browserStatus").textContent = "opening Edge, this can take a few seconds...";
  el("openBtn").disabled = true;
  const result = await api("/api/browser/open", {});
  el("browserStatus").textContent = result.error
    ? "error: " + result.error
    : "browser open at " + result.url + " - sign in if the page asks.";
  setBrowser(!result.error);
}

async function closeBrowser() {
  await api("/api/browser/close", {});
  el("browserStatus").textContent = "browser closed";
  setBrowser(false);
}

async function pick(field) {
  const result = await api("/api/pick", { field });
  if (result.error) { el("browserStatus").textContent = "error: " + result.error; return; }
  armedField = field;
  el("browserStatus").textContent =
    "Switch to the Edge window and click the element. Esc cancels.";
  el("pickOptions").innerHTML = "";
  if (pickTimer) clearInterval(pickTimer);
  pickTimer = setInterval(pollPick, 400);
}

async function pollPick() {
  const state = await api("/api/pick");
  if (!state.pick) return;
  clearInterval(pickTimer);
  pickTimer = null;
  const pick = state.pick;
  if (pick.cancelled) { el("browserStatus").textContent = "picking cancelled"; return; }
  const options = pick.options || [];
  if (!options.length) {
    el("browserStatus").textContent = "no stable selector found for that element";
    return;
  }
  applySelector(pick.field, options[0].selector);
  el("browserStatus").textContent = "picked " + pick.field + " - review the options below";
  el("pickOptions").innerHTML = `
    <p class="sub" style="margin:14px 0 6px">
      Candidates for <b>${esc(pick.field)}</b> - the first is applied; click another to swap.
    </p>
    <div class="options">${options.map((option) => `
      <div class="option" data-field="${esc(pick.field)}"
           data-selector="${esc(option.selector)}">
        <span>${esc(option.selector)}</span>
        <span>${option.matches} match${option.matches === 1 ? "" : "es"}</span>
      </div>`).join("")}</div>`;
}

document.addEventListener("click", (event) => {
  const option = event.target.closest(".option");
  if (!option) return;
  applySelector(option.dataset.field, option.dataset.selector);
});

function applySelector(field, selector) {
  const input = el("sel_" + field);
  if (input) input.value = selector;
}

async function save() {
  const selectors = {};
  for (const [key] of SELECTOR_FIELDS) selectors[key] = el("sel_" + key).value.trim();
  const payload = { url: el("url").value.trim(), selectors };
  const model = el("model").value.trim();
  if (model) payload.model = model;
  const result = await api("/api/config", { config: payload });
  el("saveStatus").textContent = result.valid
    ? "saved"
    : "saved, but the config does not load: " + (result.error || "unknown error");
  refresh();
}

async function validate() {
  el("browserStatus").textContent = "checking selectors against the live page...";
  const result = await api("/api/validate", {});
  if (result.error) { el("browserStatus").textContent = "error: " + result.error; return; }
  el("browserStatus").textContent = "selector check done";
  renderChecks(el("validateResults"), result.checks);
}

async function runTest() {
  el("testBtn").disabled = true;
  el("testStatus").textContent = "sending a prompt and waiting for the reply...";
  const result = await api("/api/test", {});
  el("testBtn").disabled = false;
  if (result.error) { el("testStatus").textContent = "error: " + result.error; return; }
  el("testStatus").textContent = "";
  renderChecks(el("testResult"), [{
    name: "round_trip",
    status: result.result.status,
    detail: result.result.detail,
    hint: result.result.hint || "",
  }]);
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
