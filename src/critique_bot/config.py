from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from critique_bot import log

PLACEHOLDER_URL = "YOUR_CHAT_UI"
PLACEHOLDER_MODEL = "YOUR_MODEL_NAME"

ENV_CHAT_URL = "CRITIQUE_CHAT_URL"
ENV_MODEL = "CRITIQUE_MODEL"
ENV_STORAGE_STATE = "CRITIQUE_STORAGE_STATE"
ENV_USER_DATA_DIR = "CRITIQUE_USER_DATA_DIR"
ENV_CDP_URL = "CRITIQUE_CDP_URL"

DEFAULT_USER_DATA_DIR = ".edge-profile"


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
    )
    if not selectors.prompt_input:
        raise ConfigError("selectors.prompt_input is required")
    if not selectors.assistant_messages:
        raise ConfigError("selectors.assistant_messages is required")

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

    if not url:
        raise ConfigError("url is required (config or CRITIQUE_CHAT_URL)")
    if PLACEHOLDER_URL in url:
        raise ConfigError(
            "url is still a placeholder. Copy config.example.json to config.json "
            "and set the real chat UI URL (or set CRITIQUE_CHAT_URL)."
        )
    if model and PLACEHOLDER_MODEL in model:
        raise ConfigError(
            "model is still a placeholder. Set selectors + model in config.json "
            "or pass --model / CRITIQUE_MODEL."
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
    )


def dedicated_edge_user_data_dir() -> Path:
    """Persistent Edge profile the bot can debug (not the daily desktop profile)."""
    system = system_edge_user_data_dir()
    return system.parent / f"{system.name}-critique-bot"


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


def default_prompt_template_path() -> Path:
    cwd_path = Path.cwd() / "prompts" / "review.txt"
    if cwd_path.is_file():
        return cwd_path
    return Path(__file__).resolve().parents[2] / "prompts" / "review.txt"
