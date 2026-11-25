"""First-run questions: where the model runs, and what crit needs to reach it.

First comes the config file (url, selectors, names to hide, ...), which a team hands out:
one found at ``CRITIQUE_CONFIG_FILE`` or the system location (``/etc/critique-bot/crit.json``,
``%ProgramData%\\critique-bot\\crit.json``) is used without a question; otherwise crit asks
for its path, and Enter goes on to the questions below (the do-it-yourself path).

``crit`` asks these when it has no config.json yet (and ``crit configure`` asks them again):

- **Cloud**: the company chat page in a browser. Asks for its https address, writes starter
  selectors, and then ``crit setup`` opens so the person points at the page's real elements.
- **Local**: an OpenAI-compatible model server (llama.cpp's llama-server, LM Studio, vLLM)
  on this PC or another PC on the network. Asks for the host and port, then lists the
  server's models to pick from. A server on another PC must be allowed explicitly: its host goes into
  ``allowed_hosts`` only after a yes, with a note that plain http is not encrypted.

Either way it then asks for company, customer, or project names to hide (``redact_terms``)
and says that personal data is hidden too. The answers go to the user config
(``config.user_config_path()``) unless the command named another file.

The questions read and write through ``ask`` and ``say`` so they can be tested without a
terminal. Nothing here sends anything anywhere except the model list request to the
server the person named.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from critique_bot.config import (
    COMPANY_CONFIG_KEY,
    STARTER_SELECTORS,
    BotConfig,
    CompanySettings,
    ConfigError,
    backend_kind,
    check_company_file,
    company_config_location,
    company_summary,
    load_company_settings,
    load_config,
    redact_term_list,
    user_config_path,
    write_config_file,
)

Ask = Callable[[str], str]
Say = Callable[[str], None]

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


@dataclass
class WizardResult:
    path: Path
    backend: str
    #: True when the cloud config still has the starter selectors (run critique-bot setup).
    needs_selectors: bool = False
    #: The config file in use ("" when the person answered the questions instead).
    config_file: str = ""


CONFIG_FILE_PROMPT = "Config file path (Enter if none): "
DIY_PREFACE = "No config file: crit needs the url and the page elements."


def use_found_config_file(path: Path, *, say: Say | None = None) -> WizardResult | None:
    """A config file found without asking (CRITIQUE_CONFIG_FILE or the system location): write
    ``path`` (an empty user config: the file keeps being found where it is) and say which.
    None when there is none; ConfigError when it is there but unusable."""
    say = say or print
    found = company_config_location({})
    if found is None:
        return None
    settings = load_company_settings(*found)
    _write(path, {})
    say(company_summary(settings))
    return _company_result(path, settings)


def _company_result(path: Path, settings: CompanySettings) -> WizardResult:
    return WizardResult(path, backend_kind(settings.data.get("backend")), config_file=settings.path)


def ask_config_file(path: Path, ask: Ask, say: Say) -> WizardResult | None:
    """Ask for the config file's path until it is good or the person presses Enter (None)."""
    while True:
        answer = ask(CONFIG_FILE_PROMPT).strip()
        if not answer:
            return None
        try:
            settings = check_company_file(answer)
        except ConfigError as exc:
            say(f"  {exc}")
            continue
        _write(path, {COMPANY_CONFIG_KEY: settings.path})
        say(company_summary(settings))
        return _company_result(path, settings)


def default_config_path() -> Path:
    """Where a first-run config goes when no --config was given: the user config (see config.user_config_path)."""
    return user_config_path()


def run_wizard(
    path: Path,
    *,
    ask: Ask | None = None,
    say: Say | None = None,
    list_models: Callable[[BotConfig], list[str]] | None = None,
) -> WizardResult | None:
    """Ask where the model runs and write ``path``. None when the person stops (Ctrl+C, empty answers)."""
    ask = ask or input
    say = say or print
    if list_models is None:
        from critique_bot.model_api import list_models as _list

        list_models = _list
    try:
        found = use_found_config_file(path, say=say)
        if found is not None:
            return found
        say("")
        chosen = ask_config_file(path, ask, say)
        if chosen is not None:
            return chosen
        say(DIY_PREFACE)
        say("")
        say("Where does the model run?")
        say("  1) Cloud: a web page (your url), used in a browser")
        say("  2) Local: a model server (llama.cpp's llama-server, LM Studio, vLLM) on this PC or another PC on the network")
        choice = _choose(ask, say, "Choose 1 or 2", ("1", "2"))
        if choice is None:
            return None
        if choice == "1":
            result = _cloud(path, ask, say)
        else:
            result = _local(path, ask, say, list_models)
        if result is not None:
            try:
                _ask_redact_terms(path, ask, say)
            except (KeyboardInterrupt, EOFError):
                say("")
                say("No names added; the rest is saved.")
            say(PII_LINE)
            say(f"Saved to {path}. Review it any time with: crit config")
        return result
    except (KeyboardInterrupt, EOFError):
        say("")
        say("Stopped; nothing was written.")
        return None


# --------------------------------------------------------------------------- cloud


def _cloud(path: Path, ask: Ask, say: Say) -> WizardResult | None:
    say("")
    while True:
        url = ask("url (for example https://ai.company.com/): ").strip()
        if not url:
            return None
        problem = _chat_url_problem(url)
        if problem is None:
            break
        say(f"  {problem}")
    host = (urlsplit(url).hostname or "").lower()
    data = {
        "backend": "browser",
        "url": url,
        "egress": "strict",
        "selectors": dict(STARTER_SELECTORS),
        "timeout_ms": 180000,
        "idle_ms": 6000,
    }
    _write(path, data)
    say(f"crit will contact only {host}: it is the only site crit sends anything to, and it refuses "
        "to type into a page on any other site.")
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


# --------------------------------------------------------------------------- names to hide

PII_LINE = (
    "Personal data (email addresses, phone numbers, card and account numbers, national IDs) "
    "is hidden from what crit sends too; it is on by default."
)

TERMS_PROMPT = "Company, customer, or project names to hide (comma-separated, Enter for none): "


def split_terms(text: str) -> list[str]:
    """``"Acme Corp, Globex"`` as ``["Acme Corp", "Globex"]`` (blank entries dropped)."""
    return [item.strip() for item in text.split(",") if item.strip()]


def _ask_redact_terms(path: Path, ask: Ask, say: Say) -> None:
    """Ask for names to replace with [REDACTED:name] and add them to ``redact_terms`` in ``path``."""
    while True:
        answer = ask(TERMS_PROMPT).strip()
        if not answer:
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        existing = data.get("redact_terms") if isinstance(data.get("redact_terms"), list) else []
        try:
            terms = redact_term_list(list(existing) + split_terms(answer))
        except ConfigError as exc:
            say(f"  {exc}")
            continue
        data["redact_terms"] = list(terms)
        _write(path, data)
        say(f"These names are sent as [REDACTED:name]: {', '.join(terms)}")
        return


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
    """Write the config readable by its owner only (it names hosts and, later, selectors).

    The person's preferences already in the file (theme, welcomed) are kept."""
    from critique_bot.config import USER_PREF_KEYS, read_config_dict

    try:
        old = read_config_dict(path)
    except ConfigError:
        old = {}
    kept = {key: old[key] for key in USER_PREF_KEYS if key in old and key not in data}
    write_config_file(path, {**data, **kept})
