from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from critique_bot import egress, env_names, integrations, log
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

ENV_CHAT_URL = "CRIT_URL"
ENV_MODEL = "CRIT_MODEL"
ENV_STORAGE_STATE = "CRIT_STORAGE_STATE"
ENV_USER_DATA_DIR = "CRIT_USER_DATA_DIR"
ENV_CDP_URL = "CRIT_CDP_URL"
ENV_QUEUE_DIR = "CRIT_QUEUE_DIR"
ENV_MAX_PARALLEL_TABS = "CRIT_MAX_PARALLEL_TABS"
ENV_GITLAB_URL = "CRIT_GITLAB_URL"

BACKEND_BROWSER = "browser"
#: A model served over the OpenAI-compatible API (llama.cpp, LM Studio, vLLM, ...).
BACKEND_LOCAL = "local"

#: Where llama.cpp's llama-server listens by default; the ``url`` of a local backend when none is given.
DEFAULT_LOCAL_URL = "http://127.0.0.1:8080"
#: ``local.api``: the OpenAI-compatible chat completions API, the only one crit speaks.
LOCAL_API_OPENAI = "openai"
LOCAL_APIS = (LOCAL_API_OPENAI,)

DEFAULT_USER_DATA_DIR = ".edge-profile"
#: crit's own folder in the home folder (see crit_home), and its override.
CRIT_HOME_NAME = ".crit"
ENV_CRIT_HOME = "CRIT_HOME"
BROWSER_PROFILE_NAME = "browser-profile"
DEDICATED_PROFILE_NAME = "msedge-user-data"
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
    """Invalid or incomplete crit configuration."""


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
    the current merge request. crit builds
    ``{base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}``.

    ``token`` is the person's GitLab personal access token (``crit config --gitlab-token``,
    or ``CRIT_GITLAB_TOKEN``). The projects it can reach are the ones crit may use.
    """

    base_url: str = ""
    token: str = field(default="", repr=False)


@dataclass(frozen=True)
class SiteLoginConfig:
    """The chat site's HTTP or Windows sign-in (Negotiate/NTLM), for machines without SSO.

    The browser gives ``username`` and ``password`` only to ``host`` (the chat url's host
    unless set). Both come only from the person's own config or the environment.
    """

    username: str = ""
    password: str = field(default="", repr=False)
    host: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.username and self.password and self.host)


@dataclass(frozen=True)
class JiraConfig:
    """A Jira instance crit reads issues from (never writes).

    ``base_url`` is the instance (``https://jira.example.com`` or ``https://x.atlassian.net``).
    ``token``: a personal access token (Jira Server/Data Center, sent as Bearer), or an API
    token with ``email`` (Jira Cloud, sent as Basic). Tokens come only from the person's own
    config or the environment.
    """

    base_url: str = ""
    email: str = ""
    token: str = field(default="", repr=False)


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
    #: The server's API key (``llama-server --api-key``, vLLM, a gateway): only from the user's own
    #: config or ``CRIT_API_KEY``, never from a shared config file. Sent only to this server.
    api_key: str = field(default="", repr=False)


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
    #: "open" (the default): the chat page loads what it needs, as for a person (its CDNs and
    #: sign-in hosts resolve); crit's own traffic still follows ``egress``. "strict": under
    #: strict egress the page reaches only the chat host and allowed_hosts too.
    page_network: str = "open"
    #: The company settings file in use ("" when none). See load_company_settings.
    company_config: str = ""
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
    jira: JiraConfig = JiraConfig()
    site_login: SiteLoginConfig = SiteLoginConfig()
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

    own_raw = raw
    company = find_company_settings(raw)
    locked: frozenset[str] = frozenset()
    shared_file = _is_shared_config_file(config_path)
    if company is not None:
        raw, locked = merge_company(company.data, raw)
        log.info(f"config file {company.path}" + (f" (saved copy of {company.cached_at})" if company.cached_at else ""))

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

    env_url = env_names.get(ENV_CHAT_URL) if "url" not in locked else ""
    url = env_url or _clean(raw.get("url"))
    if env_url:
        log.debug(f"url overridden by {ENV_CHAT_URL}")
    model = (
        _clean(model_override)
        or env_names.get(ENV_MODEL)
        or _clean(raw.get("model"))
    )
    if _clean(model_override):
        log.debug("model overridden by --model")
    elif env_names.get(ENV_MODEL):
        log.debug(f"model overridden by {ENV_MODEL}")
    storage_state = (
        env_names.get(ENV_STORAGE_STATE)
        or _clean(raw.get("storage_state"))
        or None
    )
    if env_names.get(ENV_STORAGE_STATE):
        log.debug(f"storage_state overridden by {ENV_STORAGE_STATE}")
    if storage_state:
        state_path = Path(storage_state)
        if not state_path.is_file():
            raise ConfigError(f"storage_state file not found: {state_path}")
        storage_state = str(state_path)

    if "cdp_url" in locked:
        if _clean(cdp_url_override) or env_names.get(ENV_CDP_URL):
            log.info("cdp_url is set in the config file; --cdp-url / CRIT_CDP_URL ignored")
        cdp_url_override = None
    cdp_url = (
        _clean(cdp_url_override)
        or (env_names.get(ENV_CDP_URL) if "cdp_url" not in locked else "")
        or _clean(raw.get("cdp_url"))
        or None
    )
    if _clean(cdp_url_override):
        log.debug("cdp_url overridden by --cdp-url")
    elif env_names.get(ENV_CDP_URL):
        log.debug(f"cdp_url overridden by {ENV_CDP_URL}")
    # The browser profile is personal: only the user's own config, CRIT_USER_DATA_DIR, or
    # the default; never a config file shared with others (nor relative to one).
    own_user_data = "" if shared_file else _clean(own_raw.get("user_data_dir"))
    if (company is not None and _clean(company.data.get("user_data_dir"))) or (
        shared_file and _clean(own_raw.get("user_data_dir"))
    ):
        log.info("user_data_dir in the config file is ignored: the browser profile is personal "
                 f"(set it in your own settings or {ENV_USER_DATA_DIR})")
    raw_user_data = env_names.get(ENV_USER_DATA_DIR) or own_user_data
    if env_names.get(ENV_USER_DATA_DIR):
        log.debug(f"user_data_dir overridden by {ENV_USER_DATA_DIR}")
    user_data_dir = _resolve_user_data_dir(raw_user_data, None if shared_file else config_path)
    queue_dir = _resolve_queue_dir(
        env_names.get(ENV_QUEUE_DIR) or _clean(raw.get("queue_dir")),
        config_path,
    )
    if env_names.get(ENV_QUEUE_DIR):
        log.debug(f"queue_dir overridden by {ENV_QUEUE_DIR}")
    raw_parallel = env_names.get(ENV_MAX_PARALLEL_TABS)
    if raw_parallel:
        log.debug(f"max_parallel_tabs overridden by {ENV_MAX_PARALLEL_TABS}")

    if model and PLACEHOLDER_MODEL in model:
        raise ConfigError(
            "model is still a placeholder. Set it with crit config --model NAME "
            "or pass --model / CRIT_MODEL."
        )
    local = LocalConfig()
    if backend == BACKEND_LOCAL:
        url = _local_url(url or DEFAULT_LOCAL_URL, _host_list(raw.get("allowed_hosts")))
        local = _load_local(raw.get("local"), url)
        api_key = _local_api_key(own_raw, company, shared_file=shared_file)
        if api_key:
            local = replace(local, api_key=api_key)
    else:
        if not selectors.prompt_input:
            raise ConfigError(
                "selectors.prompt_input is required: crit does not know the page's message box yet. "
                "Run crit setup to point at it (after crit config --url https://ai.company.com/)"
            )
        if not selectors.assistant_messages:
            raise ConfigError(
                "selectors.assistant_messages is required: crit does not know where the page shows replies yet. "
                "Run crit setup to point at them"
            )
        if not url:
            raise ConfigError(
                "no url is set. Run crit config --config-file /path/to/crit.json or crit config --url https://ai.company.com/ "
                "(or set CRIT_URL)"
            )
        if PLACEHOLDER_URL in url:
            raise ConfigError(
                "url is still a placeholder. Set it with "
                "crit config --url https://ai.company.com/."
            )

    egress_mode = _egress_mode(raw.get("egress"))
    if company is not None and "egress" in locked and _clean(company.data.get("egress")).lower() == egress.STRICT:
        egress_mode = egress.STRICT  # CRIT_EGRESS=open never loosens the config file's strict
    page_network = _page_network_mode(raw.get("page_network"))
    allowed_hosts = _host_list(raw.get("allowed_hosts"))
    if egress_mode == egress.STRICT and cdp_url:
        raise ConfigError(
            'egress is "strict", but cdp_url attaches to a browser crit did not start, whose own traffic '
            'crit cannot limit. Remove cdp_url (crit starts its own browser), or set "egress": "open".'
        )
    gitlab = _load_gitlab(raw, env="gitlab.base_url" not in locked)
    gitlab = replace(
        gitlab, token=_user_secret(own_raw, company, shared_file, "gitlab", "token", ENV_GITLAB_TOKENS, "--gitlab-token")
    )
    jira = _load_jira(raw, own_raw, company, shared_file, env="jira.base_url" not in locked)
    site_login = _load_site_login(own_raw, company, shared_file, url if backend != BACKEND_LOCAL else "")
    proxy_user = _clean(os.environ.get(ENV_PROXY_USER)) or _user_value(own_raw, shared_file, "proxy_login", "username")
    proxy_password = _user_secret(own_raw, company, shared_file, "proxy_login", "password", ENV_PROXY_PASSWORDS, "--proxy-password")
    egress.configure(
        egress_mode,
        url if backend != BACKEND_LOCAL else "",
        allowed_hosts,
        gitlab_url=gitlab.base_url,
        jira_url=jira.base_url,
        page_network=page_network,
    )
    integrations.configure(
        gitlab_url=gitlab.base_url, gitlab_token=gitlab.token,
        jira_url=jira.base_url, jira_email=jira.email, jira_token=jira.token,
        site_user=site_login.username, site_password=site_login.password, site_host=site_login.host,
        proxy_user=proxy_user, proxy_password=proxy_password,
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
        filter_chat_page=_bool_field("filter_chat_page", raw.get("filter_chat_page"), False)
        or (egress_mode == egress.STRICT and page_network == egress.STRICT),
        egress=egress_mode,
        page_network=page_network,
        company_config=company.path if company is not None else "",
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
        gitlab=gitlab,
        jira=jira,
        site_login=site_login,
        **_redaction_settings(raw),
    )


# --------------------------------------------------------------------------- where the config lives

#: The config file to use when ``--config`` is not given.
ENV_CONFIG = "CRIT_CONFIG"
CONFIG_FILE_NAME = "config.json"


def crit_home() -> Path:
    """crit's one folder for everything it keeps for this user (like ``~/.claude``): ``CRIT_HOME``,
    else ``%USERPROFILE%\\.crit`` on Windows and ``~/.crit`` on Linux and macOS.

    Inside: ``config.json`` (the user config), ``browser-profile/`` (the signed-in browser),
    ``msedge-user-data/`` (for ``user_data_dir: "system"``), ``cache/``, and the self-installed
    program in ``app/<version>/`` with its launchers in ``bin/``."""
    override = os.environ.get(ENV_CRIT_HOME, "").strip()
    if override:
        return Path(os.path.expanduser(override))
    if os.name == "nt":
        base = os.environ.get("USERPROFILE") or str(Path.home())
    else:
        base = os.environ.get("HOME") or str(Path.home())
    return Path(base) / CRIT_HOME_NAME


def ensure_crit_home() -> Path:
    """:func:`crit_home`, created owner-only (0700 on POSIX, an owner-only ACL on Windows)."""
    from critique_bot.output import private_dir

    return private_dir(crit_home())


def user_config_path() -> Path:
    """The person's own config: ``config.json`` in :func:`crit_home`."""
    return crit_home() / CONFIG_FILE_NAME


def default_browser_profile() -> Path:
    """The browser profile when ``user_data_dir`` is not set: ``browser-profile`` in :func:`crit_home`."""
    return crit_home() / BROWSER_PROFILE_NAME


def legacy_config_dir() -> Path:
    """Where versions before 1.1 kept the user config (``~/.config/critique-bot``,
    ``%APPDATA%\\critique-bot``); :func:`migrate_legacy_files` moves it into :func:`crit_home`."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "critique-bot"


def _legacy_dedicated_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "critique-bot" / DEDICATED_PROFILE_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "critique-bot" / DEDICATED_PROFILE_NAME
    return legacy_config_dir() / DEDICATED_PROFILE_NAME


_MIGRATED = False


def migrate_legacy_files(say: Callable[[str], None] | None = None) -> bool:
    """Once: move the user config, the saved config-file copy, and the browser profiles from the
    old folders into :func:`crit_home`. Nothing happens when ``config.json`` is already there or
    there is no old one. A profile that cannot be moved (the browser has it open) stays, the
    config is copied instead of moved, and a line says so. True when something moved."""
    global _MIGRATED
    if _MIGRATED:
        return False
    _MIGRATED = True
    say = say or log.info  # moving files is bookkeeping: the log file, not the screen
    new_config = user_config_path()
    old_dir = legacy_config_dir()
    old_config = old_dir / CONFIG_FILE_NAME
    try:
        if new_config.exists() or not old_config.is_file() or old_dir.resolve() == crit_home().resolve():
            return False
    except OSError:
        return False
    home = ensure_crit_home()
    profile_failed = ""
    for old, new in (
        (old_dir / DEFAULT_USER_DATA_DIR, default_browser_profile()),
        (_legacy_dedicated_dir(), home / DEDICATED_PROFILE_NAME),
    ):
        if not old.is_dir() or new.exists():
            continue
        try:
            shutil.move(str(old), str(new))
        except OSError as exc:
            if new.exists() and old.exists():
                shutil.rmtree(new, ignore_errors=True)  # a half copy is worse than none
            profile_failed = profile_failed or (
                f"The browser profile {old} could not be moved ({getattr(exc, 'strerror', None) or exc}); "
                f"it stays there. Close the browser and move it to {new}, or sign in again."
            )
    cache = old_dir / COMPANY_CACHE_NAME
    try:
        if cache.is_file() and not company_cache_path().exists():
            company_cache_path().parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(cache), str(company_cache_path()))
        if profile_failed:
            shutil.copy2(old_config, new_config)
        else:
            shutil.move(str(old_config), str(new_config))
        if os.name != "nt":
            os.chmod(new_config, 0o600)
    except OSError as exc:
        say(f"crit could not move its settings from {old_dir} to {home}: {exc}")
        return False
    say(f"Moved crit's files to {home}.")
    if profile_failed:
        say(profile_failed)
    return True


#: Per-user preferences kept in the user config next to the settings (never in a config file
#: from the team): the terminal style, and whether the first-run welcome was shown.
USER_PREF_KEYS = ("theme", "syntax_preview", "welcomed")


def has_settings(data: dict) -> bool:
    """True when a config object sets crit up: an empty one (a config file is found elsewhere), or one
    with a setting. Only preferences (theme, welcomed) is not set up: the first-run questions still apply."""
    return not data or any(key not in USER_PREF_KEYS for key in data)


def user_preferences() -> dict:
    """theme / syntax_preview / welcomed from the user config ({} when unreadable)."""
    try:
        data = read_config_dict(user_config_path())
    except ConfigError:
        return {}
    return {key: data[key] for key in USER_PREF_KEYS if key in data}


def set_user_preferences(**values: object) -> None:
    """Store preferences in the user config, keeping everything else in it."""
    path = user_config_path()
    try:
        data = read_config_dict(path)
    except ConfigError:
        return
    data.update({key: value for key, value in values.items() if key in USER_PREF_KEYS})
    try:
        write_config_file(path, data)
    except OSError as exc:
        log.debug(f"could not save preferences in {path}: {exc}")


def effective_theme(project_settings: object) -> str:
    """The terminal style: the project's own when one was set there, else the user's, else dark."""
    if isinstance(project_settings, dict) and isinstance(project_settings.get("theme"), str) and project_settings["theme"]:
        return project_settings["theme"]
    theme = user_preferences().get("theme")
    return theme if isinstance(theme, str) and theme else "dark"


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
    home = crit_home()
    try:
        inside_home = path.resolve().is_relative_to(home.resolve())
    except OSError:
        inside_home = False
    if inside_home:
        private_dir(home)
    if not parent.exists() or inside_home:
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


# --------------------------------------------------------------------------- the config file
#
# The config file (internally "company" settings) is the shared file with url, selectors, and
# the rest, that a team hands out; the user config only names it (``config_file``).

#: User config key naming the config file (an absolute path, UNC kept as given).
COMPANY_CONFIG_KEY = "config_file"
#: Names the config file when the user config does not.
ENV_COMPANY_CONFIG = "CRIT_CONFIG_FILE"
COMPANY_FILE_NAME = "crit.json"
#: A copy of the last config file that loaded, next to the user config.
COMPANY_CACHE_NAME = "config-file.cache.json"

#: Keys the company file decides when it sets them; the user's own value is ignored.
#: ``selectors`` (key by key) and ``gitlab.base_url`` are locked the same way, ``egress``
#: and ``page_network`` may only be tightened, and ``redact_terms`` are added together.
COMPANY_LOCKED_KEYS = ("url", "backend", "allowed_hosts", "egress", "page_network", "redact_pii", "local", "cdp_url")
#: Every top-level key crit knows; others in a company file are reported (log.info) and ignored.
KNOWN_CONFIG_KEYS = frozenset(
    {
        "url", "backend", "selectors", "model", "model_dropdown_identifier", "timeout_ms", "idle_ms",
        "reply_start_ms", "reply_stall_ms", "thinking_max_ms", "reply_max_ms", "allowed_hosts",
        "filter_chat_page", "egress", "page_network", "storage_state", "user_data_dir", "cdp_url", "queue_dir",
        "min_interval_seconds", "interval_jitter_seconds", "turn_pause_seconds", "max_parallel_tabs",
        "max_attempts", "result_retention", "job_timeout_seconds", "max_prompt_chars", "max_file_chars",
        "max_files", "max_read_bytes", "local", "gitlab", "gitlab_base_url", "redact_terms", "redact_pii",
    }
)



@dataclass(frozen=True)
class CompanySettings:
    """A company settings file that loaded (or its saved copy)."""

    #: The file's path as configured (UNC paths as given).
    path: str
    data: dict
    #: "settings" (company_config in the user's config), "env" (CRIT_CONFIG_FILE), or "system".
    source: str
    #: When the saved copy was made, if the file itself could not be read ("" otherwise).
    cached_at: str = ""


def system_company_config_path() -> Path:
    """Where IT may place the config file for everyone on the PC: ``/etc/crit/crit.json``
    on Linux and macOS, ``%ProgramData%\\crit\\crit.json`` on Windows."""
    if os.name == "nt":
        base = os.environ.get("ProgramData") or os.environ.get("PROGRAMDATA") or "C:\\ProgramData"
        return Path(base) / "crit" / COMPANY_FILE_NAME
    return Path("/etc/crit") / COMPANY_FILE_NAME


def legacy_system_company_config_path() -> Path:
    """Where versions up to 1.0.4 looked for it (``/etc/critique-bot``, ``%ProgramData%\\critique-bot``):
    still read when the new location has no file."""
    if os.name == "nt":
        base = os.environ.get("ProgramData") or os.environ.get("PROGRAMDATA") or "C:\\ProgramData"
        return Path(base) / "critique-bot" / COMPANY_FILE_NAME
    return Path("/etc/critique-bot") / COMPANY_FILE_NAME


def system_company_config_paths() -> list[Path]:
    """The system locations, new first."""
    return [system_company_config_path(), legacy_system_company_config_path()]


def company_cache_path() -> Path:
    return crit_home() / "cache" / COMPANY_CACHE_NAME


def company_path_value(text: str) -> str:
    """The path to store in ``config_file``: absolute; a UNC path (``\\\\server\\share\\...``) as given."""
    value = _clean(text)
    while len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()  # a pasted "Copy as path" keeps its quotes
    if value.startswith(("\\\\", "//")):
        return value
    if os.name == "nt":
        import ntpath

        return ntpath.abspath(ntpath.expanduser(value))
    return os.path.abspath(os.path.expanduser(value))


def company_config_location(raw: dict | None = None) -> tuple[str, str] | None:
    """Which company file applies, and why: ``(path, source)``; None when there is none.

    ``company_config`` in the loaded config (``raw``), then in the user config, then
    ``CRIT_CONFIG_FILE``, then the system default when that file exists.
    """
    candidates = [raw or {}]
    try:
        candidates.append(read_config_dict(user_config_path()))
    except ConfigError as exc:
        log.debug(f"user config not read for company_config: {exc}")
    for data in candidates:
        value = _clean(data.get(COMPANY_CONFIG_KEY))
        if value and value.lower() != "none":
            return value, "settings"
    from_env = _clean(env_names.get(ENV_COMPANY_CONFIG))
    if from_env and from_env.lower() != "none":
        return from_env, "env"
    for system in system_company_config_paths():
        try:
            present = system.is_file()
        except OSError:
            present = False
        if present:
            if system != system_company_config_path():
                log.info(f"using the config file at its old location {system}")
            return str(system), "system"
    return None


def validate_company_data(data: object, path: str) -> dict:
    """Check a company file's object; ConfigError (naming the file) when crit cannot use it."""
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a JSON object.")
    unknown = sorted(str(key) for key in data if key not in KNOWN_CONFIG_KEYS and key != COMPANY_CONFIG_KEY)
    if unknown:
        log.info(f"config file {path}: keys this version of crit does not know, ignored: {', '.join(unknown)}")
    try:
        backend = backend_kind(data.get("backend"))
        if data.get("selectors") is not None and not isinstance(data.get("selectors"), dict):
            raise ConfigError("selectors must be a JSON object")
        _host_list(data.get("allowed_hosts"))
        redact_term_list(data.get("redact_terms"))
        if data.get("redact_pii") is not None:
            _bool_field("redact_pii", data.get("redact_pii"), True)
        for key in ("egress", "page_network"):
            value = data.get(key)
            if value is not None and str(value).strip().lower() not in egress.MODES:
                raise ConfigError(f'{key} must be "strict" or "open", not {value!r}')
        if data.get("local") is not None and not isinstance(data.get("local"), dict):
            raise ConfigError("local must be a JSON object")
        if data.get("gitlab") is not None and not isinstance(data.get("gitlab"), dict):
            raise ConfigError("gitlab must be a JSON object")
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    selectors = data.get("selectors") or {}
    missing = []
    if not _clean(data.get("url")):
        missing.append("url")
    if backend == BACKEND_BROWSER:
        for key in ("prompt_input", "assistant_messages"):
            if not _clean(selectors.get(key)):
                missing.append(f"selectors.{key}")
    if missing:
        raise ConfigError(f"{path} is missing: {', '.join(missing)}")
    return {key: value for key, value in data.items() if key != COMPANY_CONFIG_KEY}


def read_company_file(path: str) -> dict:
    """The company file's checked object. OSError when it cannot be read; ConfigError when it is wrong."""
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON ({exc}).") from exc
    return validate_company_data(data, path)


def check_company_file(text: str) -> CompanySettings:
    """A company file the person named (first run, ``crit config --config-file``), checked; no saved copy is used."""
    path = company_path_value(text)
    try:
        if not Path(path).is_file():
            raise ConfigError(f"{path} does not exist.")
        data = read_company_file(path)
    except OSError as exc:
        raise ConfigError(f"{path} cannot be read ({exc.strerror or exc}).") from exc
    _save_company_cache(path, data)
    return CompanySettings(path, data, "settings")


def _save_company_cache(path: str, data: dict) -> None:
    from datetime import datetime, timezone

    cache = company_cache_path()
    try:
        old = read_config_dict(cache)
    except ConfigError:
        old = {}
    if old.get("source") == path and old.get("config") == data:
        return
    record = {"source": path, "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "config": data}
    try:
        write_config_file(cache, record)
    except OSError as exc:
        log.debug(f"could not save the config file copy {cache}: {exc}")


def _cached_company(path: str) -> tuple[dict, str] | None:
    """The saved copy of the company file at ``path`` and when it was saved; None when there is none."""
    try:
        record = read_config_dict(company_cache_path())
    except ConfigError:
        return None
    if record.get("source") != path or not isinstance(record.get("config"), dict):
        return None
    try:
        data = validate_company_data(record["config"], path)
    except ConfigError:
        return None
    return data, str(record.get("saved_at") or "")


def _plain_time(iso: str) -> str:
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso or "an earlier run"


def load_company_settings(path: str, source: str) -> CompanySettings:
    """The company file at ``path``; its saved copy (with a notice) when the file cannot be read."""
    try:
        data = read_company_file(path)
    except OSError as exc:
        cached = _cached_company(path)
        if cached is None:
            raise ConfigError(
                f"{path} cannot be read ({exc.strerror or exc}) and there is no saved copy of it. "
                "Check the network or VPN."
            ) from exc
        data, saved_at = cached
        when = _plain_time(saved_at)
        log.info(f"{path} cannot be read; using its copy from {when}.")
        return CompanySettings(path, data, source, cached_at=when)
    _save_company_cache(path, data)
    return CompanySettings(path, data, source)


def find_company_settings(raw: dict | None = None) -> CompanySettings | None:
    """The company settings that apply to a config whose object is ``raw`` (None when there are none)."""
    found = company_config_location(raw)
    if found is None:
        return None
    return load_company_settings(*found)


def _tighter(company: object, user: object) -> object:
    """egress / page_network: the user may tighten the company's "open" to "strict", never loosen."""
    if _clean(company).lower() == egress.OPEN and _clean(user).lower() == egress.STRICT:
        return egress.STRICT
    return company


def merge_company(company: dict, user: dict) -> tuple[dict, frozenset[str]]:
    """Built-in defaults < ``company`` < ``user``, with the company's locked keys kept.

    Returns the merged object and the locked keys the company set (``selectors.<key>`` and
    ``gitlab.base_url`` for nested ones). ``redact_terms`` are both lists together.
    """
    merged = {key: value for key, value in company.items() if key != COMPANY_CONFIG_KEY}
    locked = {key for key in COMPANY_LOCKED_KEYS if key in company}
    company_selectors = company.get("selectors") if isinstance(company.get("selectors"), dict) else {}
    locked |= {f"selectors.{key}" for key in company_selectors}
    company_gitlab = company.get("gitlab") if isinstance(company.get("gitlab"), dict) else {}
    if _clean(company_gitlab.get("base_url")):
        locked.add("gitlab.base_url")
    company_jira = company.get("jira") if isinstance(company.get("jira"), dict) else {}
    if _clean(company_jira.get("base_url")):
        locked.add("jira.base_url")
    for key, value in user.items():
        if key in ("egress", "page_network") and key in company:
            merged[key] = _tighter(company[key], value)
        elif key in locked:
            if value != company.get(key):
                log.info(f"{key} is set in the config file; the value in your settings is not used")
        elif key == "selectors" and isinstance(value, dict):
            merged["selectors"] = {**value, **company_selectors}
        elif key == "model_dropdown_identifier" and "selectors.model_dropdown_identifier" in locked:
            continue
        elif key == "redact_terms" and "redact_terms" in company and isinstance(value, list):
            merged["redact_terms"] = list(redact_term_list(company.get("redact_terms"))) + value
        elif key == "gitlab" and isinstance(value, dict):
            merged["gitlab"] = {**value, **company_gitlab}
        elif key == "jira" and isinstance(value, dict):
            merged["jira"] = {**value, **{k: v for k, v in company_jira.items() if k == "base_url"}}
        elif key == "gitlab_base_url" and "gitlab.base_url" in locked:
            continue
        else:
            merged[key] = value
    return merged, frozenset(locked)


def company_summary(settings: CompanySettings) -> str:
    """``Using <path>``."""
    return f"Using {settings.path}"


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
        f"backend {value!r} is not supported. Use browser (a web page in Edge) or local "
        "(a model behind an OpenAI-compatible server such as llama.cpp's llama-server)."
    )


def _egress_mode(value: object) -> str:
    """config.json "egress": "strict" (the default) or "open"; CRIT_EGRESS overrides it."""
    raw = env_names.get("CRIT_EGRESS") or value
    if raw is None or raw == "":
        return egress.STRICT
    mode = str(raw).strip().lower()
    if mode not in egress.MODES:
        raise ConfigError(f'egress must be "strict" or "open", not {raw!r}')
    return mode


def _page_network_mode(value: object) -> str:
    """config.json "page_network": "open" (the default: the page loads its own sites) or "strict"."""
    if value is None or value == "":
        return egress.OPEN
    mode = str(value).strip().lower()
    if mode not in egress.MODES:
        raise ConfigError(f'page_network must be "open" or "strict", not {value!r}')
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


#: The local server's API key from the environment (wins over the user config).
ENV_LOCAL_KEY = "CRIT_API_KEY"


def user_api_key() -> str:
    """``local.api_key`` in the user's own config (``~/.crit/config.json``), or ""."""
    try:
        return _api_key_in(read_config_dict(user_config_path()))
    except (ConfigError, OSError):
        return ""


def _api_key_in(data: object) -> str:
    local = data.get("local") if isinstance(data, dict) else None
    return _clean(local.get("api_key")) if isinstance(local, dict) else ""


#: GitLab personal access token. CI_JOB_TOKEN stays gitlab.py's last resort in CI.
ENV_GITLAB_TOKENS = ("CRIT_GITLAB_TOKEN", "GITLAB_TOKEN")
ENV_JIRA_URL = "CRIT_JIRA_URL"
ENV_JIRA_EMAIL = "CRIT_JIRA_EMAIL"
ENV_JIRA_TOKENS = ("CRIT_JIRA_TOKEN", "JIRA_API_TOKEN")


def _secret_in(data: object, section: str, key: str) -> str:
    block = data.get(section) if isinstance(data, dict) else None
    return _clean(block.get(key)) if isinstance(block, dict) else ""


def _user_secret(
    own: dict, company: "CompanySettings | None", shared_file: bool, section: str, key: str,
    env: tuple[str, ...], flag: str,
) -> str:
    """A token: the environment first, else ``<section>.<key>`` in the person's own config.

    A company or shared config file never supplies one: it would hand everyone one
    identity, and the file is readable by others.
    """
    if company is not None and _secret_in(company.data, section, key):
        log.info(f"{section}.{key} in the config file {company.path} is ignored; set it with crit config {flag}")
    value = next((_clean(os.environ.get(name)) for name in env if _clean(os.environ.get(name))), "")
    if not value:
        if shared_file and _secret_in(own, section, key):
            log.info(f"{section}.{key} in a shared config file is ignored; set it with crit config {flag}")
        elif not shared_file:
            value = _secret_in(own, section, key)
    if not value:
        try:
            value = _secret_in(read_config_dict(user_config_path()), section, key)
        except (ConfigError, OSError):
            value = ""
    if value:
        redaction.add_secret_value(value)
    return value


def _load_jira(raw: dict, own: dict, company: "CompanySettings | None", shared_file: bool, *, env: bool = True) -> JiraConfig:
    nested = raw.get("jira") if isinstance(raw.get("jira"), dict) else {}
    base_url = (_clean(os.environ.get(ENV_JIRA_URL)) if env else "") or _clean(nested.get("base_url"))
    base_url = clean_gitlab_url(base_url) if base_url else ""
    email = _clean(os.environ.get(ENV_JIRA_EMAIL)) or _clean(nested.get("email"))
    token = _user_secret(own, company, shared_file, "jira", "token", ENV_JIRA_TOKENS, "--jira-token")
    return JiraConfig(base_url=base_url, email=email, token=token)


ENV_SITE_USER = "CRIT_SITE_USER"
ENV_SITE_PASSWORDS = ("CRIT_SITE_PASSWORD",)
ENV_PROXY_USER = "CRIT_PROXY_USER"
ENV_PROXY_PASSWORDS = ("CRIT_PROXY_PASSWORD",)


def _user_value(own: dict, shared_file: bool, section: str, key: str) -> str:
    """``<section>.<key>`` from the person's own config (never a shared one), not registered as a secret."""
    if not shared_file and _secret_in(own, section, key):
        return _secret_in(own, section, key)
    try:
        return _secret_in(read_config_dict(user_config_path()), section, key)
    except (ConfigError, OSError):
        return ""


def _load_site_login(own: dict, company: "CompanySettings | None", shared_file: bool, url: str) -> SiteLoginConfig:
    """``site_login``: the person's own username and password for the chat site's HTTP sign-in."""
    from urllib.parse import urlsplit

    username = _clean(os.environ.get(ENV_SITE_USER)) or _user_value(own, shared_file, "site_login", "username")
    password = _user_secret(own, company, shared_file, "site_login", "password", ENV_SITE_PASSWORDS, "--site-password")
    host = _user_value(own, shared_file, "site_login", "host").lower().rstrip(".")
    if not host and url:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return SiteLoginConfig(username=username, password=password, host=host)


def _local_api_key(own: dict, company: "CompanySettings | None", *, shared_file: bool) -> str:
    """``CRIT_API_KEY``, else ``local.api_key`` in the user's own config. A shared config file never
    supplies it (it would hand everyone's requests one key, and the file is readable by others).
    The key is registered with redaction so it never reaches a transcript or the log file."""
    if company is not None and _api_key_in(company.data):
        log.info(f"local.api_key in the config file {company.path} is ignored; set it with crit config --api-key")
    key = _clean(os.environ.get(ENV_LOCAL_KEY))
    if not key:
        if shared_file and _api_key_in(own):
            log.info("local.api_key in a shared config file is ignored; set it with crit config --api-key")
        elif not shared_file:
            key = _api_key_in(own)
    if not key:
        key = user_api_key()
    if key:
        redaction.add_secret_value(key)
    return key


def dedicated_edge_user_data_dir() -> Path:
    """crit's own Edge profile for ``user_data_dir: "system"``: ``msedge-user-data`` in crit's folder,
    outside Chromium's default User Data directory.

    A sibling named ``User Data-critique-bot`` still string-prefix-matches
    Windows ``User Data``, so Chromium 136+ treats it as the daily profile and
    CDP returns HTTP 403.
    """
    return crit_home() / DEDICATED_PROFILE_NAME


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


def _is_shared_config_file(config_path: Path) -> bool:
    """True when ``config_path`` is itself the config file shared with others (the system
    location or ``CRIT_CONFIG_FILE``), used directly as the config."""
    shared = list(system_company_config_paths())
    env = (env_names.get(ENV_COMPANY_CONFIG) or "").strip()
    if env:
        shared.append(Path(env).expanduser())
    try:
        here = Path(config_path).resolve()
        return any(here == Path(item).resolve() for item in shared)
    except OSError:
        return False


def _resolve_user_data_dir(value: str, config_path: Path | None = None) -> str:
    """The browser profile folder. A relative path is next to config.json, never in the
    folder crit happens to run from: the profile holds the signed-in session's cookies,
    and a project folder is where they could be committed or shared.

    Not set (or the old relative default ``.edge-profile``): :func:`default_browser_profile`,
    ``browser-profile`` in crit's folder, on every system."""
    raw = value or DEFAULT_USER_DATA_DIR
    if raw.lower() in {"system", "default"}:
        resolved = str(system_edge_user_data_dir())
        log.debug(f"user_data_dir {raw!r} -> system profile {resolved}")
        return resolved
    if raw == DEFAULT_USER_DATA_DIR:
        # Resolved like every other profile path (on Windows a short 8.3 home such as
        # RUNNER~1 becomes the long name, so the same profile is always the same string).
        resolved = str(default_browser_profile().resolve())
        log.debug(f"user_data_dir {raw!r} -> {resolved}")
        return resolved
    path = Path(raw).expanduser()
    if not path.is_absolute():
        base = Path(config_path).parent if config_path is not None else user_config_path().parent
        path = base / path
    resolved = str(path.resolve())
    log.debug(f"user_data_dir {raw!r} -> {resolved}")
    return resolved


def _load_gitlab(raw: dict, *, env: bool = True) -> GitLabConfig:
    nested = raw.get("gitlab") if isinstance(raw.get("gitlab"), dict) else {}
    base_url = (
        (env_names.get(ENV_GITLAB_URL) if env else "")
        or _clean(nested.get("base_url"))
        or _clean(raw.get("gitlab_base_url"))
    )
    if env and env_names.get(ENV_GITLAB_URL):
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
    """crit's own review template (the package, or the bundle's copy).

    Never one from the current folder: a project under review is untrusted input, and a
    prompts/review.txt in it must not replace crit's instructions (--prompt-template is
    the explicit way to use another template).
    """
    package_dir = Path(__file__).resolve().parent
    candidates = [package_dir / "prompts" / "review.txt"]
    frozen_root = _frozen_root()
    if frozen_root is not None:
        candidates.append(frozen_root / "critique_bot" / "prompts" / "review.txt")
        candidates.append(frozen_root / "prompts" / "review.txt")

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
