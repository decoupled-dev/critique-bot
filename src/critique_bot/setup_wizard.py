"""First-run questions: where the model runs, and what crit needs to reach it.

``crit`` asks these when it has no config.json yet (and ``crit configure`` asks them again):

- **Cloud**: the company's chat page in a browser. Asks for the chat URL, writes a config
  with starter selectors, and offers to open ``critique-bot setup`` to pick the page's
  real elements by clicking them.
- **Local**: an OpenAI-compatible model server (llama.cpp's llama-server, LM Studio, vLLM)
  on this PC or another PC on the network. Asks for the host and port, then lists the
  server's models to pick from. A server on another PC must be allowed explicitly: its host goes into
  ``allowed_hosts`` only after a yes, with a note that plain http is not encrypted.

The questions read and write through ``ask`` and ``say`` so they can be tested without a
terminal. Nothing here sends anything anywhere except the model list request to the
server the person named.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from critique_bot.config import BotConfig, ConfigError, load_config

Ask = Callable[[str], str]
Say = Callable[[str], None]

#: Starter selectors for a chat page nobody has picked elements on yet. They match the
#: common shapes; ``critique-bot setup`` replaces them with the page's own.
STARTER_SELECTORS = {
    "model_dropdown": "",
    "model_dropdown_identifier": "",
    "model_option": "",
    "prompt_input": "textarea, [contenteditable='true'][role='textbox']",
    "send_button": "button[type='submit'], button[aria-label='Send' i]",
    "stop_button": "button[aria-label*='Stop' i]",
    "assistant_messages": "[data-message-author-role='assistant'], .assistant-message",
}
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


@dataclass
class WizardResult:
    path: Path
    backend: str
    #: True when the cloud config still has the starter selectors (run critique-bot setup).
    needs_selectors: bool = False


def default_config_path() -> Path:
    """Where a first-run config goes when no --config was given: the user's own config folder."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "critique-bot" / "config.json"


def run_wizard(
    path: Path,
    *,
    ask: Ask = input,
    say: Say = print,
    list_models: Callable[[BotConfig], list[str]] | None = None,
) -> WizardResult | None:
    """Ask where the model runs and write ``path``. None when the person stops (Ctrl+C, empty answers)."""
    if list_models is None:
        from critique_bot.model_api import list_models as _list

        list_models = _list
    try:
        say("")
        say("Where does the model run?")
        say("  1) Cloud: the company chat page, used in a browser")
        say("  2) Local: a model server (llama.cpp's llama-server, LM Studio, vLLM) on this PC or another PC on the network")
        choice = _choose(ask, say, "Choose 1 or 2", ("1", "2"))
        if choice is None:
            return None
        if choice == "1":
            return _cloud(path, ask, say)
        return _local(path, ask, say, list_models)
    except (KeyboardInterrupt, EOFError):
        say("")
        say("Stopped; nothing was written.")
        return None


# --------------------------------------------------------------------------- cloud


def _cloud(path: Path, ask: Ask, say: Say) -> WizardResult | None:
    while True:
        url = ask("Chat URL (for example https://chat.company.com/): ").strip()
        if not url:
            return None
        problem = _chat_url_problem(url)
        if problem is None:
            break
        say(f"  {problem}")
    data = {
        "backend": "browser",
        "url": url,
        "selectors": dict(STARTER_SELECTORS),
        "timeout_ms": 180000,
        "idle_ms": 6000,
    }
    _write(path, data)
    say(f"Saved {path}.")
    say("Prompts go only to that chat site: crit refuses to type into a page on any other site.")
    return WizardResult(path, "browser", needs_selectors=True)


def _chat_url_problem(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return "Enter the full address, starting with https://"
    if parts.scheme == "http" and host not in _LOOPBACK:
        return "Use https:// (plain http would send your code over the network unencrypted)."
    return None


# --------------------------------------------------------------------------- local


def _local(
    path: Path, ask: Ask, say: Say, list_models: Callable[[BotConfig], list[str]]
) -> WizardResult | None:
    here = _yes_no(ask, "Is the model server on this PC?", default=True)
    if here:
        host = "127.0.0.1"
    else:
        host = _ask_host(ask, say)
        if host is None:
            return None
    port = _ask_port(ask, say, 8080)
    if port is None:
        return None
    remote = host not in _LOOPBACK
    if remote:
        say("")
        say(f"crit will send your code to {host}:{port} over plain http, which is not encrypted on the network,")
        say("and anyone who can reach that port can use the model. An SSH tunnel avoids both:")
        say(f"  ssh -N -L {port}:127.0.0.1:{port} you@{host}   (then answer 'yes, on this PC' above)")
        if not _yes_no(ask, f"Allow crit to send code to {host}?", default=False):
            say("Nothing was written.")
            return None
    data: dict[str, Any] = {
        "backend": "local",
        "url": f"http://{_url_host(host)}:{port}",
        "model": "",
        "local": {"api": "openai", "temperature": 0.2, "num_ctx": 32768},
        "reply_start_ms": 120000,
    }
    if remote:
        data["allowed_hosts"] = [host]
    _write(path, data)
    model = _pick_model(path, ask, say, list_models)
    if model is None:
        model = ask("Model name as the server knows it (empty: the first one it lists): ").strip()
    data["model"] = model
    _write(path, data)
    say(f"Saved {path}.")
    return WizardResult(path, "local")


def _ask_host(ask: Ask, say: Say) -> str | None:
    while True:
        raw = ask("Host name or IP address of the PC running the model: ").strip()
        if not raw:
            return None
        host = _clean_host(raw)
        if host:
            return host
        say("  Enter a host name (gpu-box.corp.example) or an IP address (192.168.1.50), without http:// or a port.")


def _clean_host(raw: str) -> str | None:
    text = raw.strip().lower()
    if "://" in text:
        text = urlsplit(text).hostname or ""
    text = text.strip("[]")
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        pass
    if ":" in text:
        return None
    return text if _HOST_RE.match(text) else None


def _url_host(host: str) -> str:
    try:
        return f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
    except ValueError:
        return host


def _ask_port(ask: Ask, say: Say, default: int) -> int | None:
    while True:
        raw = ask(f"Port [{default}]: ").strip()
        if not raw:
            return default
        if raw.isdigit() and 0 < int(raw) < 65536:
            return int(raw)
        say("  Enter a port number between 1 and 65535.")


def _pick_model(
    path: Path, ask: Ask, say: Say, list_models: Callable[[BotConfig], list[str]]
) -> str | None:
    """The model chosen from the server's list; None when the list cannot be had."""
    while True:
        try:
            config = load_config(path)
            names = [name for name in list_models(config) if name]
        except (ConfigError, RuntimeError, OSError) as exc:
            say(f"Could not get the model list: {exc}")
            if _yes_no(ask, "Try again?", default=True):
                continue
            return None
        if not names:
            say("The server lists no models. Start it with one (llama-server -m model.gguf).")
            if _yes_no(ask, "Try again?", default=True):
                continue
            return None
        say("Models on the server:")
        for index, name in enumerate(names, start=1):
            say(f"  {index}) {name}")
        picked = _choose(ask, say, f"Choose 1-{len(names)}", tuple(str(i) for i in range(1, len(names) + 1)))
        return names[int(picked) - 1] if picked else None


# --------------------------------------------------------------------------- helpers


def _choose(ask: Ask, say: Say, prompt: str, options: tuple[str, ...]) -> str | None:
    while True:
        answer = ask(f"{prompt}: ").strip()
        if not answer:
            return None
        if answer in options:
            return answer
        say(f"  Type one of: {', '.join(options)}")


def _yes_no(ask: Ask, prompt: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = ask(f"{prompt} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _write(path: Path, data: dict[str, Any]) -> None:
    """Write the config readable by its owner only (it names hosts and, later, selectors)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
