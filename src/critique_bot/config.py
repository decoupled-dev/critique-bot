from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER_URL = "YOUR_CHAT_UI"
PLACEHOLDER_MODEL = "YOUR_MODEL_NAME"

ENV_CHAT_URL = "CRITIQUE_CHAT_URL"
ENV_MODEL = "CRITIQUE_MODEL"
ENV_STORAGE_STATE = "CRITIQUE_STORAGE_STATE"


class ConfigError(ValueError):
    """Invalid or incomplete bot configuration."""


@dataclass(frozen=True)
class Selectors:
    prompt_input: str
    assistant_messages: str
    model_dropdown: str = ""
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
) -> BotConfig:
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

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
        model_option=_clean(selectors_raw.get("model_option")),
        send_button=_clean(selectors_raw.get("send_button")),
    )
    if not selectors.prompt_input:
        raise ConfigError("selectors.prompt_input is required")
    if not selectors.assistant_messages:
        raise ConfigError("selectors.assistant_messages is required")

    url = os.environ.get(ENV_CHAT_URL) or _clean(raw.get("url"))
    model = (
        _clean(model_override)
        or os.environ.get(ENV_MODEL)
        or _clean(raw.get("model"))
    )
    storage_state = (
        os.environ.get(ENV_STORAGE_STATE)
        or _clean(raw.get("storage_state"))
        or None
    )
    if storage_state:
        state_path = Path(storage_state)
        if not state_path.is_file():
            raise ConfigError(f"storage_state file not found: {state_path}")
        storage_state = str(state_path)

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
    )


def compose_prompt(template: str, patch: str) -> str:
    if "{patch}" not in template:
        raise ConfigError("prompt template must contain the {patch} placeholder")
    return template.replace("{patch}", patch)


def default_prompt_template_path() -> Path:
    cwd_path = Path.cwd() / "prompts" / "review.txt"
    if cwd_path.is_file():
        return cwd_path
    return Path(__file__).resolve().parents[2] / "prompts" / "review.txt"
