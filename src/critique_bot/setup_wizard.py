"""First-run questions: how crit reaches the model, and what it needs to do that.

A config file found without a question (``CRIT_CONFIG_FILE`` or the system location:
``/etc/crit/crit.json``, ``%ProgramData%\\crit\\crit.json``) wins: it is used
and nothing is asked. Otherwise ``crit`` asks, one question at a time:

- **Local model**: a model server (llama.cpp's llama-server, LM Studio, vLLM) on this PC or
  the network. Asks for ``ip:port``, checks that it answers and which models it lists, then
  which model. A server on another PC goes into ``allowed_hosts`` (config validation needs it).
- **Web UI**: a page in the browser. Asks for its address, the model, and the page elements
  (starter guesses that Enter accepts); ``crit setup`` checks them against the real page.

Either way it then asks for names to hide (``redact_terms``) and says that personal data is
hidden too. Everything goes to the user config (``config.user_config_path()``) unless the
command named another file, and everything can be changed later with ``crit config``. Ctrl+C
or end of input at any question leaves the file as it was.

The questions read and write through ``ask`` and ``say`` so they can be tested without a
terminal. Nothing here sends anything anywhere except the check of the server the person named.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from critique_bot import log, redaction
from critique_bot.config import (
    STARTER_SELECTORS,
    CompanySettings,
    ConfigError,
    _local_url,
    backend_kind,
    clean_https_url,
    company_config_location,
    company_summary,
    load_company_settings,
    redact_term_list,
    user_config_path,
    write_config_file,
)

#: local_model.PROBE_NEEDS_KEY: the server answered 401 or 403.
PROBE_NEEDS_KEY = "needs-key"

Ask = Callable[[str], str]
Say = Callable[[str], None]

_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


@dataclass
class WizardResult:
    path: Path
    backend: str
    #: The config file in use ("" when the person answered the questions instead).
    config_file: str = ""


class _Stop(Exception):
    """Ctrl+C or end of input at a question."""


CHOICE_LINES = (
    "How does crit reach the model?",
    "  1) Local model (a model server on this PC or the network)",
    "  2) Web UI (a page in the browser)",
    "(Or use a config file: crit config --config-file PATH)",
)
CHOICE_PROMPT = "Choose 1 or 2: "
SERVER_PROMPT = "Model server (ip:port): "
STOPPED = "Stopped; nothing was written."
CHANGE_LATER = "Change any of this later with: crit config (see crit config --help)"
WEB_UI_DONE = "Saved. To check the page elements against the real page: crit setup"
NO_PICKER = "To switch models crit needs the model picker; without it the page's current model is used."

#: The page elements asked for, in order: (key, label, required).
ELEMENT_QUESTIONS = (
    ("prompt_input", "Message box", True),
    ("send_button", "Send button", False),
    ("stop_button", "Stop button", False),
    ("assistant_messages", "Replies", True),
    ("model_dropdown", "Model picker", False),
    ("model_dropdown_identifier", "Model picker identifier", False),
    ("model_option", "Model option", False),
)
#: Keys asked with "(Enter to skip)" and no starter default.
_SKIPPABLE = ("model_dropdown", "model_dropdown_identifier", "model_option")


def use_found_config_file(path: Path, *, say: Say | None = None) -> WizardResult | None:
    """A config file found without asking (CRIT_CONFIG_FILE or the system location): write
    ``path`` (an empty user config: the file keeps being found where it is) and say which.
    None when there is none; ConfigError when it is there but unusable."""
    say = say or log.info  # found without a question: the log file says which, the screen nothing
    found = company_config_location({})
    if found is None:
        return None
    settings = load_company_settings(*found)
    _write(path, {})
    say(company_summary(settings))
    return _company_result(path, settings)


def _company_result(path: Path, settings: CompanySettings) -> WizardResult:
    return WizardResult(path, backend_kind(settings.data.get("backend")), config_file=settings.path)


def default_config_path() -> Path:
    """Where a first-run config goes when no --config was given: the user config (see config.user_config_path)."""
    return user_config_path()


def run_wizard(
    path: Path,
    *,
    ask: Ask | None = None,
    say: Say | None = None,
    probe: Callable[..., Any] | None = None,
    ask_secret: Ask | None = None,
) -> WizardResult | None:
    """Ask how crit reaches the model and write ``path``. None when the person stops (Ctrl+C, EOF).

    ``ask_secret`` asks for the model server's API key without echo (getpass by default)."""
    raw_ask = ask or input
    say = say or print
    if ask_secret is None:
        import getpass

        ask_secret = getpass.getpass
    raw_secret = ask_secret
    if probe is None:
        from critique_bot.model_api import probe_server as probe

    def asking(prompt: str) -> str:
        try:
            return raw_ask(prompt)
        except (KeyboardInterrupt, EOFError) as exc:
            raise _Stop from exc

    def asking_secret(prompt: str) -> str:
        try:
            return raw_secret(prompt)
        except (KeyboardInterrupt, EOFError) as exc:
            raise _Stop from exc

    found = use_found_config_file(path)
    if found is not None:
        return found
    before = path.read_bytes() if path.is_file() else None
    try:
        say("")
        for line in CHOICE_LINES:
            say(line)
        choice = _choose(asking, say)
        if choice == "1":
            data, after = _local(asking, say, probe, asking_secret)
        else:
            data, after = _web_ui(asking, say)
        _write(path, data)
        for line in after:
            say(line)
        _ask_redact_terms(path, asking, say)
    except _Stop:
        _restore(path, before)
        say("")
        say(STOPPED)
        return None
    say(PII_LINE)
    say(f"Saved to {path}.")
    say(CHANGE_LATER)
    return WizardResult(path, data["backend"])


def _restore(path: Path, before: bytes | None) -> None:
    """Put ``path`` back as it was before the questions (gone when it was not there)."""
    try:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(before)
    except OSError:
        pass


def _choose(ask: Ask, say: Say) -> str:
    while True:
        answer = ask(CHOICE_PROMPT).strip()
        if answer in ("1", "2"):
            return answer
        say("  Type 1 or 2.")


# --------------------------------------------------------------------------- local model


def parse_server(raw: str) -> str | None:
    """The server's base url for ``ip:port``, ``host:port``, or ``http(s)://host[:port][/path]``.

    http when no scheme is given; then a port is required. None when it is not an address."""
    text = raw.strip().rstrip("/")
    if not text:
        return None
    has_scheme = "://" in text
    try:
        parts = urlsplit(text if has_scheme else "http://" + text)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        return None
    host = (parts.hostname or "").lower()
    if not host or (not has_scheme and port is None):
        return None
    if not has_scheme and (parts.path or parts.query or parts.fragment):
        return None
    try:
        ip = ipaddress.ip_address(host)
        shown = f"[{ip}]" if ip.version == 6 else str(ip)
    except ValueError:
        if not _HOST_RE.match(host):
            return None
        shown = host
    port_text = f":{port}" if port is not None else ""
    return f"{parts.scheme}://{shown}{port_text}{parts.path.rstrip('/')}"


def _is_remote(url: str) -> bool:
    """True when config validation needs the server's host in allowed_hosts."""
    try:
        _local_url(url, ())
    except ConfigError:
        return True
    return False


def _ask_api_key(ask: Ask, say: Say, probe: Callable[..., Any], url: str, ask_secret: Ask) -> tuple[str, Any]:
    """The server answered 401/403: ask for its key (no echo) and check again. A refused key is
    asked once more, then "Try another key?". The key and the last probe; "" when given up."""
    tries = 0
    while True:
        key = ask_secret("API key: ").strip()
        if not key:
            say("  Type the key the server was started with (--api-key).")
            continue
        redaction.add_secret_value(key)
        result = probe(url, key)
        say(result.line)
        if result.outcome != PROBE_NEEDS_KEY:
            return key, result
        tries += 1
        if tries == 1:
            continue
        if not _yes_no(ask, "Try another key?", default=True):
            say("Saving it without a key; set it later with: crit config --api-key")
            return "", result


def _local(ask: Ask, say: Say, probe: Callable[..., Any], ask_secret: Ask | None = None) -> tuple[dict, list[str]]:
    say("")
    api_key = ""
    while True:
        raw = ask(SERVER_PROMPT).strip()
        url = parse_server(raw)
        if url is None:
            say("  Type the address with its port, for example 192.168.1.50:8080 or localhost:8080.")
            continue
        say(f"Checking {url} ...")
        result = probe(url)
        say(result.line)
        if getattr(result, "outcome", "") == PROBE_NEEDS_KEY and ask_secret is not None:
            api_key, result = _ask_api_key(ask, say, probe, url, ask_secret)
        if result.reachable:
            break
        if not _yes_no(ask, "Try another address?", default=True):
            say("Saving it anyway; crit checks the server again on the next run.")
            break
    model = _ask_model(ask, say, list(result.models) if result.reachable else [])
    data: dict[str, Any] = {"backend": "local", "url": url, "model": model}
    if api_key:
        data["local"] = {"api_key": api_key}
    after: list[str] = []
    if _is_remote(url):
        host = (urlsplit(url).hostname or "").lower()
        data["allowed_hosts"] = [host]
        after.append(f"crit will send prompts only to {host}.")
    return data, after


def _ask_model(ask: Ask, say: Say, models: list[str]) -> str:
    if not models:
        return ask("Model name: ").strip()
    shown = models[:5]
    if len(shown) > 1:
        for index, name in enumerate(shown, start=1):
            say(f"  {index}) {name}")
    while True:
        answer = ask(f"Model [{models[0]}]: ").strip()
        if not answer:
            return models[0]
        if answer.isdigit():
            if 1 <= int(answer) <= len(shown):
                return shown[int(answer) - 1]
            say(f"  Type a number from 1 to {len(shown)}, or the model's name.")
            continue
        return answer


# --------------------------------------------------------------------------- Web UI


def _web_ui(ask: Ask, say: Say) -> tuple[dict, list[str]]:
    say("")
    while True:
        raw = ask("url: ").strip()
        if not raw:
            say("  Type the page's address, starting with https://")
            continue
        try:
            url = clean_https_url(raw, "url")
            break
        except ConfigError as exc:
            say(f"  {str(exc)[0].upper()}{str(exc)[1:]}.")
    model = ask("Model (Enter for the page's default): ").strip()
    selectors: dict[str, str] = {}
    for key, label, required in ELEMENT_QUESTIONS:
        selectors[key] = _ask_element(ask, say, key, label, required)
    if model and not (selectors["model_dropdown"] or selectors["model_dropdown_identifier"]):
        say(NO_PICKER)
    data: dict[str, Any] = {"backend": "browser", "url": url, "selectors": selectors, "egress": "strict"}
    if model:
        data["model"] = model
    return data, [WEB_UI_DONE]


def _ask_element(ask: Ask, say: Say, key: str, label: str, required: bool) -> str:
    default = "" if key in _SKIPPABLE else str(STARTER_SELECTORS.get(key) or "")
    suffix = "(Enter to skip)" if key in _SKIPPABLE else (f"[{default}]" if default else "")
    prompt = f"{label} ({key}) {suffix}".rstrip() + ": "
    while True:
        answer = ask(prompt).strip() or default
        if not answer:
            if required:
                say("  This one is needed: type a CSS selector.")
                continue
            return ""
        if key == "model_dropdown_identifier" and not _looks_like_css(answer):
            return answer  # plain text (a label or id) is fine here
        problem = css_problem(answer)
        if problem is None:
            return answer
        say(f"  {problem}")


def _looks_like_css(text: str) -> bool:
    return text[:1] in ".#[" or any(ch in text for ch in "[]()'\"")


def css_problem(text: str) -> str | None:
    """Why ``text`` is obviously not a CSS selector (unbalanced brackets or quotes); None when
    it may be one. The real check is ``crit setup`` against the page."""
    stack: list[str] = []
    pairs = {")": "(", "]": "["}
    quote = ""
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "([":
            stack.append(ch)
        elif ch in ")]":
            if not stack or stack.pop() != pairs[ch]:
                return f"That is not a CSS selector: an unmatched {ch!r}."
    if quote:
        return f"That is not a CSS selector: a {quote} quote is not closed."
    if stack:
        return f"That is not a CSS selector: a {stack[-1]!r} is not closed."
    if text[0] in ",>+~" or text[-1] in ",>+~":
        return "That is not a CSS selector: it starts or ends with a combinator or comma."
    return None


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


def _yes_no(ask: Ask, prompt: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = ask(f"{prompt} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _write(path: Path, data: dict[str, Any]) -> None:
    """Write the config readable by its owner only (it names hosts and selectors).

    The person's preferences already in the file (theme, welcomed) are kept."""
    from critique_bot.config import USER_PREF_KEYS, read_config_dict

    try:
        old = read_config_dict(path)
    except ConfigError:
        old = {}
    kept = {key: old[key] for key in USER_PREF_KEYS if key in old and key not in data}
    write_config_file(path, {**data, **kept})
