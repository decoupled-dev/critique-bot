from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from critique_bot import log
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
ENV_BACKEND = "CRITIQUE_BACKEND"
ENV_BASE_URL = "CRITIQUE_BASE_URL"
ENV_API_KEY = "CRITIQUE_API_KEY"

BACKEND_BROWSER = "browser"
BACKEND_OLLAMA = "ollama"
BACKEND_OPENAI = "openai"
BACKEND_OPENAI_COMPAT = "openai-compatible"

DEFAULT_USER_DATA_DIR = ".edge-profile"
DEFAULT_QUEUE_DIR_NAME = ".critique-queue"
DEFAULT_MIN_INTERVAL_SECONDS = 30.0
DEFAULT_INTERVAL_JITTER_SECONDS = 5.0
DEFAULT_MAX_PARALLEL_TABS = 1
ABSOLUTE_MAX_PARALLEL_TABS = 8
DEFAULT_MAX_ATTEMPTS = 3
ABSOLUTE_MAX_ATTEMPTS = 20
DEFAULT_RESULT_RETENTION = 200
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

_BACKEND_ALIASES = {
    "browser": BACKEND_BROWSER,
    "web": BACKEND_BROWSER,
    "playwright": BACKEND_BROWSER,
    "ui": BACKEND_BROWSER,
    "ollama": BACKEND_OLLAMA,
    "local": BACKEND_OLLAMA,
    "openai": BACKEND_OPENAI,
    "openai-compatible": BACKEND_OPENAI_COMPAT,
    "openai_compatible": BACKEND_OPENAI_COMPAT,
    "compatible": BACKEND_OPENAI_COMPAT,
}


class ConfigError(ValueError):
    """Invalid or incomplete bot configuration."""


@dataclass(frozen=True)
class Selectors:
    prompt_input: str
    assistant_messages: str
    model_dropdown: str = ""
    model_dropdown_identifier: str = ""
    model_option: str = ""
    send_button: str = ""
    stop_button: str = ""


@dataclass(frozen=True)
class BotConfig:
    url: str
    selectors: Selectors
    model: str = ""
    timeout_ms: int = 180_000
    idle_ms: int = 4_000
    storage_state: str | None = None
    user_data_dir: str | None = None
    cdp_url: str | None = None
    queue_dir: str = ""
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS
    interval_jitter_seconds: float = DEFAULT_INTERVAL_JITTER_SECONDS
    max_parallel_tabs: int = DEFAULT_MAX_PARALLEL_TABS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    result_retention: int = DEFAULT_RESULT_RETENTION
    job_timeout_seconds: float = 0.0
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS
    max_file_chars: int = DEFAULT_MAX_FILE_CHARS
    max_files: int = DEFAULT_MAX_FILES
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES
    backend: str = BACKEND_BROWSER
    base_url: str = ""
    api_key: str = ""

    @property
    def input_limits(self) -> InputLimits:
        return InputLimits(
            max_prompt_chars=self.max_prompt_chars,
            max_file_chars=self.max_file_chars,
            max_files=self.max_files,
            max_read_bytes=self.max_read_bytes,
        )

    @property
    def uses_browser(self) -> bool:
        return self.backend == BACKEND_BROWSER

    @property
    def job_timeout_sec(self) -> float:
        """Wall-clock ceiling for one queued job.

        Defaults to twice the per-call timeout plus a minute of setup, which is
        generous for a healthy run but still unblocks a waiting CI job when the
        browser wedges below the Playwright timeout.
        """
        if self.job_timeout_seconds > 0:
            return self.job_timeout_seconds
        return (self.timeout_ms / 1000.0) * 2 + 60.0


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

    backend = _parse_backend(
        os.environ.get(ENV_BACKEND) or _clean(raw.get("backend"))
    )
    if os.environ.get(ENV_BACKEND):
        log.debug(f"backend overridden by {ENV_BACKEND}")

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
    user_data_dir = _resolve_user_data_dir(raw_user_data)
    queue_dir = _resolve_queue_dir(
        os.environ.get(ENV_QUEUE_DIR) or _clean(raw.get("queue_dir")),
        config_path,
    )
    if os.environ.get(ENV_QUEUE_DIR):
        log.debug(f"queue_dir overridden by {ENV_QUEUE_DIR}")
    raw_parallel = os.environ.get(ENV_MAX_PARALLEL_TABS)
    if raw_parallel:
        log.debug(f"max_parallel_tabs overridden by {ENV_MAX_PARALLEL_TABS}")

    base_url = _normalize_base_url(
        backend,
        os.environ.get(ENV_BASE_URL) or _clean(raw.get("base_url")),
    )
    if os.environ.get(ENV_BASE_URL):
        log.debug(f"base_url overridden by {ENV_BASE_URL}")
    api_key = _resolve_api_key(raw, backend)

    if model and PLACEHOLDER_MODEL in model:
        raise ConfigError(
            "model is still a placeholder. Set model in config.json "
            "or pass --model / CRITIQUE_MODEL."
        )
    if backend == BACKEND_BROWSER:
        if not selectors.prompt_input:
            raise ConfigError("selectors.prompt_input is required")
        if not selectors.assistant_messages:
            raise ConfigError("selectors.assistant_messages is required")
        if not url:
            raise ConfigError("url is required (config or CRITIQUE_CHAT_URL)")
        if PLACEHOLDER_URL in url:
            raise ConfigError(
                "url is still a placeholder. Copy config.example.json to config.json "
                "and set the real chat UI URL (or set CRITIQUE_CHAT_URL)."
            )
    else:
        if not model:
            raise ConfigError(
                f"model is required for the {backend} backend "
                "(config, --model, or CRITIQUE_MODEL)"
            )
        if backend == BACKEND_OPENAI_COMPAT and not base_url:
            raise ConfigError(
                "base_url is required for openai-compatible "
                "(config or CRITIQUE_BASE_URL)"
            )
        if backend == BACKEND_OPENAI and not api_key:
            raise ConfigError(
                "OpenAI backend needs an API key: set CRITIQUE_API_KEY or "
                "OPENAI_API_KEY (or api_key / api_key_env in config.json)"
            )

    return BotConfig(
        url=url,
        selectors=selectors,
        model=model,
        timeout_ms=_positive_int("timeout_ms", raw.get("timeout_ms"), 180_000),
        idle_ms=_positive_int("idle_ms", raw.get("idle_ms"), 4_000),
        storage_state=storage_state,
        user_data_dir=user_data_dir,
        cdp_url=cdp_url,
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
        base_url=base_url,
        api_key=api_key,
    )


def _parse_backend(value: str) -> str:
    key = value.strip().lower().replace("_", "-").replace(" ", "-")
    if not key:
        return BACKEND_BROWSER
    mapped = _BACKEND_ALIASES.get(key)
    if mapped is None:
        known = ", ".join(
            (BACKEND_BROWSER, BACKEND_OLLAMA, BACKEND_OPENAI, BACKEND_OPENAI_COMPAT)
        )
        raise ConfigError(f"unknown backend {value!r}. Use one of: {known}")
    return mapped


def _normalize_base_url(backend: str, value: str) -> str:
    cleaned = value.strip().rstrip("/")
    if not cleaned:
        if backend == BACKEND_OLLAMA:
            return DEFAULT_OLLAMA_BASE_URL
        if backend == BACKEND_OPENAI:
            return DEFAULT_OPENAI_BASE_URL
        return ""
    if backend == BACKEND_OLLAMA:
        if cleaned.endswith("/v1"):
            return cleaned
        if cleaned.endswith("/api"):
            return cleaned[: -len("/api")] + "/v1"
        return cleaned + "/v1"
    return cleaned


def _resolve_api_key(raw: dict, backend: str) -> str:
    env_name = _clean(raw.get("api_key_env"))
    if not env_name and backend == BACKEND_OPENAI:
        env_name = "OPENAI_API_KEY"
    if os.environ.get(ENV_API_KEY):
        log.debug(f"api_key overridden by {ENV_API_KEY}")
        return os.environ[ENV_API_KEY]
    if env_name and os.environ.get(env_name):
        log.debug(f"api_key taken from {env_name}")
        return os.environ[env_name]
    return _clean(raw.get("api_key"))


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


def _resolve_user_data_dir(value: str) -> str:
    raw = value or DEFAULT_USER_DATA_DIR
    if raw.lower() in {"system", "default"}:
        resolved = str(system_edge_user_data_dir())
        log.debug(f"user_data_dir {raw!r} -> system profile {resolved}")
        return resolved
    resolved = str(Path(raw).expanduser().resolve())
    log.debug(f"user_data_dir {raw!r} -> {resolved}")
    return resolved


def compose_prompt(template: str, patch: str) -> str:
    if "{patch}" not in template:
        raise ConfigError("prompt template must contain the {patch} placeholder")
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
