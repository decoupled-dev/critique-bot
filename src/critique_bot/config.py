from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from critique_bot import egress, log
from critique_bot import redaction
from critique_bot.patch import (
    ABSOLUTE_MAX_FILE_CHARS,
    ABSOLUTE_MAX_FILES,
    ABSOLUTE_MAX_PROMPT_CHARS,
    ABSOLUTE_MAX_READ_BYTES,
    DEFAULT_MAX_FILE_CHARS,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_PROMPT_CHARS,
    DEFAULT_MAX_READ_BYTES,
    InputLimits,
)

PLACEHOLDER_URL = "YOUR_CHAT_UI"
PLACEHOLDER_MODEL = "YOUR_MODEL_NAME"

ENV_CHAT_URL = "CRITIQUE_CHAT_URL"
ENV_MODEL = "CRITIQUE_MODEL"
ENV_STORAGE_STATE = "CRITIQUE_STORAGE_STATE"
ENV_USER_DATA_DIR = "CRITIQUE_USER_DATA_DIR"
ENV_CDP_URL = "CRITIQUE_CDP_URL"
ENV_QUEUE_DIR = "CRITIQUE_QUEUE_DIR"
ENV_MAX_PARALLEL_TABS = "CRITIQUE_MAX_PARALLEL_TABS"
ENV_GITLAB_URL = "CRITIQUE_GITLAB_URL"

BACKEND_BROWSER = "browser"
#: A model served over the OpenAI-compatible API (llama.cpp, LM Studio, vLLM, ...).
BACKEND_LOCAL = "local"

#: Where llama.cpp's llama-server listens by default; the ``url`` of a local backend when none is given.
DEFAULT_LOCAL_URL = "http://127.0.0.1:8080"
#: ``local.api``: the OpenAI-compatible chat completions API, the only one crit speaks.
LOCAL_API_OPENAI = "openai"
LOCAL_APIS = (LOCAL_API_OPENAI,)

DEFAULT_USER_DATA_DIR = ".edge-profile"
DEFAULT_QUEUE_DIR_NAME = ".critique-queue"
DEFAULT_MIN_INTERVAL_SECONDS = 30.0
DEFAULT_INTERVAL_JITTER_SECONDS = 5.0
DEFAULT_TURN_PAUSE_SECONDS = 0.0
DEFAULT_MAX_PARALLEL_TABS = 1
ABSOLUTE_MAX_PARALLEL_TABS = 8
DEFAULT_MAX_ATTEMPTS = 3
ABSOLUTE_MAX_ATTEMPTS = 20
DEFAULT_RESULT_RETENTION = 200

_BROWSER_ALIASES = frozenset({"", "browser", "web", "playwright", "ui", "chat"})
_LOCAL_ALIASES = frozenset(
    {"local", "llama.cpp", "llamacpp", "llama-cpp", "llama-server", "lmstudio", "lm-studio", "vllm", "openai-compatible", "http"}
)
# a list of this PC's addresses, not a bind
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # nosec B104


class ConfigError(ValueError):
    """Invalid or incomplete bot configuration."""


_NO_OLLAMA = (
    "Ollama is not supported. Serve the model with an OpenAI-compatible server instead, such as "
    "llama.cpp's llama-server (llama-server -m model.gguf --port 8080 -c 32768), and set "
    '"backend": "local", "url": "http://127.0.0.1:8080" (crit configure asks for these).'
)


@dataclass(frozen=True)
class GitLabConfig:
    """GitLab instance used to call API v4.

    ``base_url`` is the origin (``https://gitlab.example.com``). Project ID,
    MR IID, and MR URL are never stored here; they come from CI or CLI for
    the current merge request. The bot builds
    ``{base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}``.
    """

    base_url: str = ""


@dataclass(frozen=True)
class Selectors:
    prompt_input: str
    assistant_messages: str
    model_dropdown: str = ""
    model_dropdown_identifier: str = ""
    model_option: str = ""
    send_button: str = ""
    stop_button: str = ""
    #: Optional "Continue generating" control. When set and shown after a
    #: reply ends, it is clicked once and the rest is merged into the reply.
    continue_button: str = ""


@dataclass(frozen=True)
class LocalConfig:
    """The ``local`` object of ``config.json``: how a model on this PC is called."""

    #: ``openai``: ``/v1/chat/completions``, as llama.cpp, LM Studio, and vLLM serve it.
    api: str = "openai"
    #: The server's context window in tokens (``llama-server -c``). crit sizes its chats to it;
    #: 0 assumes 32768.
    num_ctx: int = 0
    #: Sampling temperature sent with every request. None leaves the server's default.
    temperature: float | None = None


@dataclass(frozen=True)
class BotConfig:
    url: str
    selectors: Selectors
    model: str = ""
    timeout_ms: int = 180_000
    idle_ms: int = 4_000
    #: No sign of a reply this long after a send: send again once if the text is still in the box, then fail (the caller retries).
    reply_start_ms: int = 60_000
    #: A started reply with no text and no generating signal for this long has stalled.
    reply_stall_ms: int = 90_000
    #: The page may show "generating" with no text yet (a thinking model) for this long.
    thinking_max_ms: int = 600_000
    #: Hard cap on one reply while it keeps making progress.
    reply_max_ms: int = 1_200_000
    #: More hosts the chat page may reach (an internal chat's API, static files, or sign-in host).
    #: Each also covers its subdomains. Used only with filter_chat_page.
    allowed_hosts: tuple[str, ...] = ()
    #: True: the chat page may reach only its own site and allowed_hosts. False (the default):
    #: the page works as it does for a person; crit controls what it types into it.
    filter_chat_page: bool = False
    #: "strict" (the default): crit connects to the chat host and allowed_hosts only, and the
    #: agent's commands run offline. "open": the chat page and builds reach what they need.
    #: See egress.py and docs/security.md.
    egress: str = "strict"
    storage_state: str | None = None
    user_data_dir: str | None = None
    cdp_url: str | None = None
    queue_dir: str = ""
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS
    interval_jitter_seconds: float = DEFAULT_INTERVAL_JITTER_SECONDS
    turn_pause_seconds: float = DEFAULT_TURN_PAUSE_SECONDS
    max_parallel_tabs: int = DEFAULT_MAX_PARALLEL_TABS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    result_retention: int = DEFAULT_RESULT_RETENTION
    job_timeout_seconds: float = 0.0
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS
    max_file_chars: int = DEFAULT_MAX_FILE_CHARS
    max_files: int = DEFAULT_MAX_FILES
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES
    backend: str = BACKEND_BROWSER
    local: LocalConfig = LocalConfig()
    gitlab: GitLabConfig = GitLabConfig()
    #: Company, customer, or project names replaced by ``[REDACTED:name]`` in everything sent.
    redact_terms: tuple[str, ...] = ()
    #: Personal data (emails, phone numbers, cards, IBANs, national IDs) is redacted too.
    redact_pii: bool = True

    @property
    def is_local(self) -> bool:
        """The model runs on this PC (``backend: local``): no browser, no sign-in."""
        return self.backend == BACKEND_LOCAL

    @property
    def input_limits(self) -> InputLimits:
        return InputLimits(
            max_prompt_chars=self.max_prompt_chars,
            max_file_chars=self.max_file_chars,
            max_files=self.max_files,
            max_read_bytes=self.max_read_bytes,
        )

    @property
    def job_timeout_sec(self) -> float:
        """Wall-clock ceiling for one queued job.

        Defaults to twice the per-call timeout plus a minute of setup, which is
        generous for a healthy run but still unblocks a waiting CI job when the
        browser wedges below the Playwright timeout.
        """
        return self.job_timeout_sec_for(0)

    def job_timeout_sec_for(self, staged_files: int = 0) -> float:
        """Ceiling for one job, scaled when files are sent as extra chat turns."""
        if self.job_timeout_seconds > 0:
            return self.job_timeout_seconds
        per_turn = self.timeout_ms / 1000.0
        base = per_turn * 2 + 60.0
        extra = max(int(staged_files), 0)
        if extra <= 0:
            return base
        # instructions + each file + the patch paste
        return max(base, (extra + 2) * per_turn + 60.0)


def _host_list(value: object) -> tuple[str, ...]:
    """``allowed_hosts``: host names (a URL is reduced to its host), lowercased, without duplicates."""
    if value is None or value == "":
        return ()
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list):
        raise ConfigError("allowed_hosts must be a list of host names, for example [\"api.company.net\"]")
    hosts: list[str] = []
    for item in items:
        text = str(item or "").strip().lower()
        if "://" in text:
            from urllib.parse import urlsplit

            text = urlsplit(text).hostname or ""
        text = text.strip().strip(".").lstrip("*.")
        if not text:
            continue
        if "/" in text or " " in text:
            raise ConfigError(f"allowed_hosts: {item!r} is not a host name")
        if text not in hosts:
            hosts.append(text)
    return tuple(hosts)


def _clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _positive_int(name: str, value: object, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be > 0, got {parsed}")
    return parsed


def _clamped_positive_int(
    name: str, value: object, default: int, maximum: int
) -> int:
    parsed = _positive_int(name, value, default)
    if parsed > maximum:
        log.warn(f"{name}={parsed} exceeds {maximum}; using {maximum}")
        return maximum
    return parsed


def _non_negative_float(name: str, value: object, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if parsed < 0:
        raise ConfigError(f"{name} must be >= 0, got {parsed}")
    return parsed


def load_config(
    path: str | Path,
    *,
    model_override: str | None = None,
    cdp_url_override: str | None = None,
) -> BotConfig:
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    log.info(f"reading config {config_path.resolve()}")

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")

    selectors_raw = raw.get("selectors") or {}
    if not isinstance(selectors_raw, dict):
        raise ConfigError("selectors must be a JSON object")

    backend = backend_kind(raw.get("backend"))

    selectors = Selectors(
        prompt_input=_clean(selectors_raw.get("prompt_input")),
        assistant_messages=_clean(selectors_raw.get("assistant_messages")),
        model_dropdown=_clean(selectors_raw.get("model_dropdown")),
        model_dropdown_identifier=_clean(
            selectors_raw.get("model_dropdown_identifier")
            or raw.get("model_dropdown_identifier")
        ),
        model_option=_clean(selectors_raw.get("model_option")),
        send_button=_clean(selectors_raw.get("send_button")),
        stop_button=_clean(selectors_raw.get("stop_button")),
        continue_button=_clean(selectors_raw.get("continue_button")),
    )

    url = os.environ.get(ENV_CHAT_URL) or _clean(raw.get("url"))
    if os.environ.get(ENV_CHAT_URL):
        log.debug(f"url overridden by {ENV_CHAT_URL}")
    model = (
        _clean(model_override)
        or os.environ.get(ENV_MODEL)
        or _clean(raw.get("model"))
    )
    if _clean(model_override):
        log.debug("model overridden by --model")
    elif os.environ.get(ENV_MODEL):
        log.debug(f"model overridden by {ENV_MODEL}")
    storage_state = (
        os.environ.get(ENV_STORAGE_STATE)
        or _clean(raw.get("storage_state"))
        or None
    )
    if os.environ.get(ENV_STORAGE_STATE):
        log.debug(f"storage_state overridden by {ENV_STORAGE_STATE}")
    if storage_state:
        state_path = Path(storage_state)
        if not state_path.is_file():
            raise ConfigError(f"storage_state file not found: {state_path}")
        storage_state = str(state_path)

    cdp_url = (
        _clean(cdp_url_override)
        or os.environ.get(ENV_CDP_URL)
        or _clean(raw.get("cdp_url"))
        or None
    )
    if _clean(cdp_url_override):
        log.debug("cdp_url overridden by --cdp-url")
    elif os.environ.get(ENV_CDP_URL):
        log.debug(f"cdp_url overridden by {ENV_CDP_URL}")
    raw_user_data = os.environ.get(ENV_USER_DATA_DIR) or _clean(raw.get("user_data_dir"))
    if os.environ.get(ENV_USER_DATA_DIR):
        log.debug(f"user_data_dir overridden by {ENV_USER_DATA_DIR}")
    user_data_dir = _resolve_user_data_dir(raw_user_data, config_path)
    queue_dir = _resolve_queue_dir(
        os.environ.get(ENV_QUEUE_DIR) or _clean(raw.get("queue_dir")),
        config_path,
    )
    if os.environ.get(ENV_QUEUE_DIR):
        log.debug(f"queue_dir overridden by {ENV_QUEUE_DIR}")
    raw_parallel = os.environ.get(ENV_MAX_PARALLEL_TABS)
    if raw_parallel:
        log.debug(f"max_parallel_tabs overridden by {ENV_MAX_PARALLEL_TABS}")

    if model and PLACEHOLDER_MODEL in model:
        raise ConfigError(
            "model is still a placeholder. Set it with crit config --model NAME "
            "or pass --model / CRITIQUE_MODEL."
        )
    local = LocalConfig()
    if backend == BACKEND_LOCAL:
        url = _local_url(url or DEFAULT_LOCAL_URL, _host_list(raw.get("allowed_hosts")))
        local = _load_local(raw.get("local"), url)
    else:
        if not selectors.prompt_input:
            raise ConfigError(
                "selectors.prompt_input is required: crit does not know the chat's message box yet. "
                "Run crit setup to point at it, or crit config --preset chatgpt for ChatGPT"
            )
        if not selectors.assistant_messages:
            raise ConfigError(
                "selectors.assistant_messages is required: crit does not know where the chat shows replies yet. "
                "Run crit setup to point at them"
            )
        if not url:
            raise ConfigError(
                "no chat address is set. Run crit config --preset chatgpt, or "
                "crit config --url https://chat.company.com/ (or set CRITIQUE_CHAT_URL)"
            )
        if PLACEHOLDER_URL in url:
            raise ConfigError(
                "url is still a placeholder. Set the chat address with "
                "crit config --url https://chat.company.com/ (or crit config --preset chatgpt)."
            )

    egress_mode = _egress_mode(raw.get("egress"))
    allowed_hosts = _host_list(raw.get("allowed_hosts"))
    if egress_mode == egress.STRICT and cdp_url:
        raise ConfigError(
            'egress is "strict", but cdp_url attaches to a browser crit did not start, whose own traffic '
            'crit cannot limit. Remove cdp_url (crit starts its own browser), or set "egress": "open".'
        )
    gitlab_raw = raw.get("gitlab") if isinstance(raw.get("gitlab"), dict) else {}
    egress.configure(
        egress_mode,
        url if backend != BACKEND_LOCAL else "",
        allowed_hosts,
        gitlab_url=os.environ.get("CRITIQUE_GITLAB_URL") or _clean(gitlab_raw.get("base_url")),
    )

    return BotConfig(
        url=url,
        selectors=selectors,
        model=model,
        timeout_ms=_positive_int("timeout_ms", raw.get("timeout_ms"), 180_000),
        idle_ms=_positive_int("idle_ms", raw.get("idle_ms"), 4_000),
        reply_start_ms=_positive_int("reply_start_ms", raw.get("reply_start_ms"), 60_000),
        reply_stall_ms=_positive_int("reply_stall_ms", raw.get("reply_stall_ms"), 90_000),
        thinking_max_ms=_positive_int("thinking_max_ms", raw.get("thinking_max_ms"), 600_000),
        reply_max_ms=_positive_int("reply_max_ms", raw.get("reply_max_ms"), 1_200_000),
        storage_state=storage_state,
        user_data_dir=user_data_dir,
        cdp_url=cdp_url,
        allowed_hosts=_host_list(raw.get("allowed_hosts")),
        filter_chat_page=_bool_field("filter_chat_page", raw.get("filter_chat_page"), False) or egress_mode == egress.STRICT,
        egress=egress_mode,
        queue_dir=queue_dir,
        min_interval_seconds=_non_negative_float(
            "min_interval_seconds",
            raw.get("min_interval_seconds"),
            DEFAULT_MIN_INTERVAL_SECONDS,
        ),
        interval_jitter_seconds=_non_negative_float(
            "interval_jitter_seconds",
            raw.get("interval_jitter_seconds"),
            DEFAULT_INTERVAL_JITTER_SECONDS,
        ),
        turn_pause_seconds=_non_negative_float(
            "turn_pause_seconds",
            raw.get("turn_pause_seconds"),
            DEFAULT_TURN_PAUSE_SECONDS,
        ),
        max_parallel_tabs=_clamped_positive_int(
            "max_parallel_tabs",
            raw_parallel if raw_parallel else raw.get("max_parallel_tabs"),
            DEFAULT_MAX_PARALLEL_TABS,
            ABSOLUTE_MAX_PARALLEL_TABS,
        ),
        max_attempts=_clamped_positive_int(
            "max_attempts",
            raw.get("max_attempts"),
            DEFAULT_MAX_ATTEMPTS,
            ABSOLUTE_MAX_ATTEMPTS,
        ),
        result_retention=_positive_int(
            "result_retention", raw.get("result_retention"), DEFAULT_RESULT_RETENTION
        ),
        job_timeout_seconds=_non_negative_float(
            "job_timeout_seconds", raw.get("job_timeout_seconds"), 0.0
        ),
        max_prompt_chars=_clamped_positive_int(
            "max_prompt_chars",
            raw.get("max_prompt_chars"),
            DEFAULT_MAX_PROMPT_CHARS,
            ABSOLUTE_MAX_PROMPT_CHARS,
        ),
        max_file_chars=_clamped_positive_int(
            "max_file_chars",
            raw.get("max_file_chars"),
            DEFAULT_MAX_FILE_CHARS,
            ABSOLUTE_MAX_FILE_CHARS,
        ),
        max_files=_clamped_positive_int(
            "max_files",
            raw.get("max_files"),
            DEFAULT_MAX_FILES,
            ABSOLUTE_MAX_FILES,
        ),
        max_read_bytes=_clamped_positive_int(
            "max_read_bytes",
            raw.get("max_read_bytes"),
            DEFAULT_MAX_READ_BYTES,
            ABSOLUTE_MAX_READ_BYTES,
        ),
        backend=backend,
        local=local,
        gitlab=_load_gitlab(raw),
        **_redaction_settings(raw),
    )


# --------------------------------------------------------------------------- where the config lives

#: The config file to use when ``--config`` is not given.
ENV_CONFIG = "CRITIQUE_CONFIG"
CONFIG_FILE_NAME = "config.json"


def user_config_path() -> Path:
    """The person's own config: ``$XDG_CONFIG_HOME/critique-bot/config.json`` (default
    ``~/.config/...``) on Linux and macOS, ``%APPDATA%\\critique-bot\\config.json`` on Windows."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "critique-bot" / CONFIG_FILE_NAME


def read_config_dict(path: str | Path) -> dict:
    """The JSON object in ``path``; ``{}`` when the file does not exist."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must hold a JSON object")
    return raw


def write_config_file(path: str | Path, data: dict) -> Path:
    """Write ``data`` to ``path`` atomically, readable by its owner only (0600 on POSIX).

    A new folder, and always the user config's folder, is made owner-only (0700 on POSIX,
    an owner-only ACL on Windows). An existing project folder keeps its permissions.
    """
    from critique_bot.output import private_dir

    path = Path(path)
    parent = path.parent
    if not parent.exists() or path.resolve() == user_config_path().resolve():
        private_dir(parent)
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


_LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def clean_https_url(value: str, what: str = "address") -> str:
    """``value`` as an https URL (plain http only for this PC); ConfigError explains what is wrong."""
    from urllib.parse import urlsplit

    text = _clean(value)
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ConfigError(f"{text!r} is not a web address ({exc})") from exc
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise ConfigError(f"the {what} must be a full address starting with https://, not {text!r}")
    if parts.scheme == "http" and host not in _LOCAL_HTTP_HOSTS:
        raise ConfigError(f"the {what} must start with https:// (plain http would send your code unencrypted)")
    if parts.username or parts.password:
        raise ConfigError(f"the {what} must not contain a user name or password")
    return text


def clean_gitlab_url(value: str) -> str:
    """``gitlab.base_url``: the instance's https address (query, fragment, and trailing slash dropped)."""
    from urllib.parse import urlsplit

    text = clean_https_url(value, "GitLab address")
    parts = urlsplit(text)
    port = f":{parts.port}" if parts.port else ""
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{host}{port}{path}"


#: ``crit config --preset chatgpt``: kept equal to config.chatgpt.example.json (a test checks),
#: embedded because a packaged binary may not ship the example files.
CHATGPT_PRESET: dict = {
    "backend": "browser",
    "url": "https://chatgpt.com/",
    "egress": "strict",
    "allowed_hosts": [
        "oaistatic.com",
        "oaiusercontent.com",
        "auth.openai.com",
        "challenges.cloudflare.com",
    ],
    "selectors": {
        "model_dropdown": "",
        "model_dropdown_identifier": "model-switcher",
        "model_option": "[role='menuitemradio'], [role='menuitem'], [role='option']",
        "prompt_input": "#prompt-textarea, [data-testid='prompt-textarea'], #mobile-composer-prompt, textarea[data-mobile-composer-prompt]",
        "send_button": "button[data-testid='send-button'], button[data-composer-submit], button.wm-composer-submitButton",
        "stop_button": "button[data-testid='stop-button'], button[aria-label*='Stop' i]",
        "assistant_messages": "[data-message-author-role='assistant'] .markdown, [data-assistant-markdown], [data-message-role='assistant']",
    },
    "timeout_ms": 180000,
    "idle_ms": 6000,
}

#: Starter selectors for a chat page nobody has picked elements on yet. They match the
#: common shapes; ``crit setup`` replaces them with the page's own.
STARTER_SELECTORS: dict = {
    "model_dropdown": "",
    "model_dropdown_identifier": "",
    "model_option": "",
    "prompt_input": "textarea, [contenteditable='true'][role='textbox']",
    "send_button": "button[type='submit'], button[aria-label='Send' i]",
    "stop_button": "button[aria-label*='Stop' i]",
    "assistant_messages": "[data-message-author-role='assistant'], .assistant-message",
}

SELECTOR_KEYS = (
    "prompt_input",
    "assistant_messages",
    "send_button",
    "stop_button",
    "continue_button",
    "model_dropdown",
    "model_dropdown_identifier",
    "model_option",
)


def backend_kind(value: object) -> str:
    """``browser`` or ``local`` for the ``backend`` key; anything else is an error.

    ``browser`` (also ``web``, ``playwright``, ``ui``, ``chat``, or nothing) drives the chat
    page in Edge. ``local`` (also ``llama.cpp``, ``llama-server``, ``lmstudio``, ``vllm``,
    ``openai-compatible``) calls a model over the OpenAI-compatible API.
    """
    key = _clean(value).lower().replace("_", "-").replace(" ", "-")
    if key == "ollama":
        raise ConfigError(_NO_OLLAMA)
    if key in _BROWSER_ALIASES:
        return BACKEND_BROWSER
    if key in _LOCAL_ALIASES:
        return BACKEND_LOCAL
    raise ConfigError(
        f"backend {value!r} is not supported. Use browser (the chat page in Edge) or local "
        "(a model behind an OpenAI-compatible server such as llama.cpp's llama-server)."
    )


def _egress_mode(value: object) -> str:
    """config.json "egress": "strict" (the default) or "open"; CRITIQUE_EGRESS overrides it."""
    raw = os.environ.get("CRITIQUE_EGRESS") or value
    if raw is None or raw == "":
        return egress.STRICT
    mode = str(raw).strip().lower()
    if mode not in egress.MODES:
        raise ConfigError(f'egress must be "strict" or "open", not {raw!r}')
    return mode


def _bool_field(name: str, value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{name} must be true or false")


def _local_url(url: str, allowed_hosts: tuple[str, ...]) -> str:
    """The base URL of a local model server: http(s), on this PC unless the host is in allowed_hosts.

    Nothing leaves the PC but the conversation with the configured URL. A model on another
    machine is opted into by listing its host in ``allowed_hosts``, like an internal chat site.
    """
    from urllib.parse import urlsplit

    value = url.strip().rstrip("/")
    if "://" not in value:
        value = "http://" + value
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ConfigError(f"url {url!r} is not a valid URL: {exc}") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"url {url!r} must be the http(s) address of the local model server, e.g. {DEFAULT_LOCAL_URL}")
    if parts.username or parts.password:
        raise ConfigError("url must not carry a user name or password")
    host = parts.hostname.lower().strip("[]")
    if host in _LOOPBACK_HOSTS or host.endswith(".localhost"):
        return value
    if any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts):
        log.warn(f"backend local: the model at {host} is on another machine; the conversation goes there")
        return value
    raise ConfigError(
        f"backend local: {host} is not this PC. A local model is reached at localhost or 127.0.0.1 "
        f"(e.g. {DEFAULT_LOCAL_URL}); a model on another machine must be listed in allowed_hosts."
    )


def _load_local(raw: object, url: str) -> LocalConfig:
    """The ``local`` object. Only the OpenAI-compatible API is spoken."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("local must be a JSON object")
    api = _clean(raw.get("api")).lower()
    if api in ("openai-compatible", "openai_compatible", "chat-completions", "completions"):
        api = LOCAL_API_OPENAI
    if api == "ollama":
        raise ConfigError(_NO_OLLAMA)
    if api and api not in LOCAL_APIS:
        raise ConfigError(f"local.api {raw.get('api')!r} is not supported; use openai (or leave it out)")
    api = LOCAL_API_OPENAI
    temperature: float | None = None
    if raw.get("temperature") is not None and _clean(raw.get("temperature")) != "":
        temperature = _non_negative_float("local.temperature", raw.get("temperature"), 0.0)
    return LocalConfig(
        api=api,
        num_ctx=_positive_int("local.num_ctx", raw.get("num_ctx"), 0) if _clean(raw.get("num_ctx")) else 0,
        temperature=temperature,
    )


def dedicated_edge_user_data_dir() -> Path:
    """Bot-owned Edge profile, outside Chromium's default User Data directory.

    A sibling named ``User Data-critique-bot`` still string-prefix-matches
    Windows ``User Data``, so Chromium 136+ treats it as the daily profile and
    CDP returns HTTP 403.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local"
        )
        return Path(base) / "critique-bot" / "msedge-user-data"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "critique-bot"
            / "msedge-user-data"
        )
    return Path.home() / ".config" / "critique-bot" / "msedge-user-data"


def system_edge_user_data_dir() -> Path:
    """Microsoft Edge user-data directory for the signed-in desktop profile."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local"
        )
        return Path(base) / "Microsoft" / "Edge" / "User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Microsoft Edge"
    snap = (
        Path.home()
        / "snap"
        / "microsoft-edge"
        / "current"
        / ".config"
        / "microsoft-edge"
    )
    standard = Path.home() / ".config" / "microsoft-edge"
    if snap.is_dir() and not standard.is_dir():
        return snap
    return standard


def _resolve_queue_dir(value: str, config_path: Path) -> str:
    """Shared inbox for worker + submit. Relative paths are next to config.json."""
    raw = value.strip() if value else ""
    if not raw:
        resolved = (config_path.parent / DEFAULT_QUEUE_DIR_NAME).expanduser().resolve()
        log.debug(f"queue_dir default -> {resolved}")
        return str(resolved)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    resolved = path.resolve()
    log.debug(f"queue_dir {raw!r} -> {resolved}")
    return str(resolved)


def _resolve_user_data_dir(value: str, config_path: Path | None = None) -> str:
    """The browser profile folder. A relative path is next to config.json, never in the
    folder crit happens to run from: the profile holds the signed-in session's cookies,
    and a project folder is where they could be committed or shared."""
    raw = value or DEFAULT_USER_DATA_DIR
    if raw.lower() in {"system", "default"}:
        resolved = str(system_edge_user_data_dir())
        log.debug(f"user_data_dir {raw!r} -> system profile {resolved}")
        return resolved
    path = Path(raw).expanduser()
    if not path.is_absolute() and config_path is not None:
        path = Path(config_path).parent / path
    resolved = str(path.resolve())
    log.debug(f"user_data_dir {raw!r} -> {resolved}")
    return resolved


def _load_gitlab(raw: dict) -> GitLabConfig:
    nested = raw.get("gitlab") if isinstance(raw.get("gitlab"), dict) else {}
    base_url = (
        os.environ.get(ENV_GITLAB_URL)
        or _clean(nested.get("base_url"))
        or _clean(raw.get("gitlab_base_url"))
    )
    if os.environ.get(ENV_GITLAB_URL):
        log.debug(f"gitlab.base_url overridden by {ENV_GITLAB_URL}")
    return GitLabConfig(
        base_url=base_url,
    )


def compose_prompt(
    template: str,
    patch: str,
    mr_context: str = "",
    files: str = "",
) -> str:
    if "{patch}" not in template:
        raise ConfigError("prompt template must contain the {patch} placeholder")
    context = (mr_context or "").strip()
    file_body = (files or "").strip()
    if "{mr_context}" in template:
        template = template.replace(
            "{mr_context}",
            context or "No GitLab merge-request metadata was available.",
        )
    elif context:
        template = (
            "SYSTEM — merge request context from GitLab\n"
            f"{context}\n\n"
            + template
        )
    if "{files}" not in template and file_body:
        template = "FILE CONTEXT\n{files}\n\n" + template
    if "{files}" in template:
        template = template.replace(
            "{files}",
            file_body or "No full-file context was attached.",
        )
    return template.replace("{patch}", patch)


def format_attachments(
    attachments: list[tuple[str, str]],
    *,
    named: bool = True,
) -> str:
    """Join file contents for inclusion in a prompt.

    When named, each file is prefixed with ``--- file: <path> ---``.
    A single unnamed attachment is returned as raw text (review-template mode).
    """
    if not attachments:
        return ""
    if not named:
        return "\n\n".join(content for _, content in attachments)
    parts: list[str] = []
    for name, content in attachments:
        parts.append(f"--- file: {name} ---")
        parts.append(content.rstrip("\n"))
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def compose_prompt_from_args(
    prompt: str,
    attachments: list[tuple[str, str]] | None = None,
) -> str:
    """Build a prompt from CLI text plus optional file attachments.

    ``{files}`` is replaced with named file sections. ``{patch}`` is replaced
    with raw text for one file, or named sections for several. If neither
    placeholder is present, files are appended after the prompt.
    """
    attachments = list(attachments or [])
    if not prompt.strip():
        raise ConfigError("prompt is empty")
    has_files = "{files}" in prompt
    has_patch = "{patch}" in prompt
    if has_files or has_patch:
        if not attachments:
            raise ConfigError(
                "prompt contains {files} or {patch} but no files were provided"
            )
        named = format_attachments(attachments, named=True)
        raw = format_attachments(attachments, named=len(attachments) > 1)
        if has_files:
            prompt = prompt.replace("{files}", named)
        if has_patch:
            prompt = prompt.replace("{patch}", raw)
        return prompt
    if not attachments:
        return prompt
    return prompt.rstrip() + "\n\n" + format_attachments(attachments, named=True)


def _frozen_root() -> Path | None:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def default_prompt_template_path() -> Path:
    """Resolve the bundled review template (cwd, next to the binary, or package)."""
    package_dir = Path(__file__).resolve().parent
    candidates = [
        Path.cwd() / "prompts" / "review.txt",
        Path(sys.executable).resolve().parent / "prompts" / "review.txt",
        package_dir / "prompts" / "review.txt",
    ]
    frozen_root = _frozen_root()
    if frozen_root is not None:
        candidates.append(frozen_root / "prompts" / "review.txt")
        candidates.append(frozen_root / "critique_bot" / "prompts" / "review.txt")
    try:
        candidates.append(package_dir.parents[2] / "prompts" / "review.txt")
    except IndexError:
        pass

    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.is_file():
            return path
    raise ConfigError(
        "review prompt template not found. Place prompts/review.txt next to "
        "the binary or in the current directory, or pass --prompt-template."
    )


ENV_REDACT_TERMS = "CRIT_REDACT_TERMS"


def redact_term_list(value: object) -> tuple[str, ...]:
    """``redact_terms``: a list of names (company, customer, project), each 3-200 characters, without duplicates."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError('redact_terms must be a list of names, for example ["Acme Corp", "Globex"]')
    try:
        return redaction.clean_terms(value)
    except ValueError as exc:
        raise ConfigError(f"redact_terms: {exc}") from exc


def _redaction_settings(raw: dict) -> dict[str, object]:
    """``redact_terms`` (plus comma-separated ``CRIT_REDACT_TERMS``) and ``redact_pii``, registered with redaction."""
    terms = list(redact_term_list(raw.get("redact_terms")))
    from_env = [item for item in os.environ.get(ENV_REDACT_TERMS, "").split(",") if item.strip()]
    if from_env:
        log.debug(f"{len(from_env)} redact term(s) added from {ENV_REDACT_TERMS}")
    try:
        cleaned = redaction.clean_terms(terms + from_env)
    except ValueError as exc:
        raise ConfigError(f"{ENV_REDACT_TERMS}: {exc}") from exc
    pii = _bool_field("redact_pii", raw.get("redact_pii"), True)
    redaction.set_custom_terms(cleaned)
    redaction.set_pii_redaction(pii)
    return {"redact_terms": cleaned, "redact_pii": pii}
