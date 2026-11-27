# SPDX-License-Identifier: Apache-2.0
"""What a session is set up with: the tool format and set, extensions, the shell, the role, budgets."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from critique_bot import agent_shell, log, model_api, roles
from critique_bot import tool_api
from critique_bot.config import BotConfig
from critique_bot.engine.prompts import (
    DEFAULT_TOOL_FORMAT,
    TOOL_FORMAT_NATIVE,
    TOOL_FORMAT_OPENAI,
    TOOL_FORMAT_TAGS,
    _file_agent,
    tool_format_of,
)
from critique_bot.tool_api import ALLOWED_TOOLS


#: Characters per tool-result message unless "max_result_chars" says otherwise.
DEFAULT_RESULT_CHARS = 20_000


#: The "minimal" tool set: the shell does the searching, listing, and git; files are read and
#: edited with tools that number lines, check syntax, and can be undone.
MINIMAL_TOOLS = (
    "run_command",
    "command_output",
    "command_input",
    "kill_command",
    "read_files",
    "edit_file",
    "write_files",
    "todo",
    "ask_user",
)


@dataclass(frozen=True)
class _ToolSetup:
    tool_format: str
    #: The tools block for the first message ("" with the tag format, whose prompt lists the tools).
    tools_block: str
    #: The registry to install for the session, or None for the built-in one.
    registry: Any
    #: Native format: the tools in OpenAI chat format, handed to each session (use_tools).
    native_tools: list[dict[str, Any]] | None = None
    #: The tools the model is told about (for /tools); () when that is the registry's list.
    names: tuple[str, ...] = ()


def _tool_setup(
    settings: dict[str, Any],
    *,
    helpers: int,
    web: bool,
    only: Iterable[str] | None = None,
    native_ok: bool = False,
    extensions: Any = None,
) -> _ToolSetup:
    """The tool format, the tool set, and the tools block, from ``tool_format`` and ``tool_set``.

    ``native_ok``: the backend calls tools natively (a local model server). Then native is the
    default; ``"tool_format": "openai"`` or ``"tags"`` still picks a text format.
    """
    raw = str(settings.get("tool_format") or "").strip().lower()
    fmt = tool_format_of(settings, DEFAULT_TOOL_FORMAT)
    if native_ok and raw in {"", "auto"}:
        fmt = TOOL_FORMAT_NATIVE
    elif fmt == TOOL_FORMAT_NATIVE and not native_ok:
        fmt = TOOL_FORMAT_OPENAI  # the web chat has no native tool calls
    minimal = str(settings.get("tool_set") or "").strip().lower() == "minimal"
    names = list(only) if only is not None else list(MINIMAL_TOOLS if minimal else ALLOWED_TOOLS)
    if helpers < 1 and "delegate" in names:
        names.remove("delegate")
    if not web and "web_fetch" in names:
        names.remove("web_fetch")
    if only is None and not offers_patch(settings) and "apply_patch" in names:
        names.remove("apply_patch")  # only for patch-trained models (profile or setting edit_format)
    registry = tool_api.builtin_registry().only(names) if (minimal or only is not None) else None
    block = ""
    native: list[dict[str, Any]] | None = None
    source = registry or tool_api.builtin_registry().only(names)
    if extensions is not None and extensions.active():
        # Manifest and MCP tools, hooks, and the agents delegate can start: one merged registry
        # (extensions/session.py), decided now and kept for the whole session.
        source = extensions.registry(source)
        registry = extensions.registry(registry or tool_api.builtin_registry())
        if fmt == TOOL_FORMAT_TAGS:
            block = extensions.tools_text()
    if fmt == TOOL_FORMAT_OPENAI:
        from critique_bot import openai_format

        block = openai_format.tools_block(source.openai_tools())
    elif fmt == TOOL_FORMAT_NATIVE:
        # Sorted by name, so the request's tools are byte-identical turn after turn (prompt cache).
        native = sorted(source.openai_tools(), key=lambda tool: str((tool.get("function") or {}).get("name")))
    names_now = source.names() if extensions is not None and extensions.active() else ()
    return _ToolSetup(fmt, block, registry, native, names_now)


def _load_extensions(root: Path, settings: dict[str, Any]) -> Any:
    """This session's extension tools, MCP servers, hooks and agents (extensions.load_session); None on failure."""
    from critique_bot import extensions

    try:
        return extensions.load_session(root, settings, builtin_names=ALLOWED_TOOLS)
    except Exception as exc:  # extensions are optional: crit runs without them
        log.warn(f"extensions not loaded: {exc}")
        return None


def _session_start_hook(extensions: Any, setup: _ToolSetup) -> str:
    """Run the session_start hooks; the context they add goes into the project notes."""
    if extensions is None:
        return ""
    try:
        decision = extensions.run_hook("session_start", {"tools": list(setup.names or ())})
    except Exception as exc:
        log.warn(f"session_start hook failed: {exc}")
        return ""
    return decision.append_context


def _model_label(config: BotConfig) -> str:
    """What the header and /status call the model: the name, and where it runs when local."""
    return model_api.model_label(config)


#: Profile fields that become loop settings (a value in .crit/settings.json wins).
PROFILE_SETTINGS = ("tool_output_cap_tokens", "shell_timeout_s", "edit_format", "masking_penalty")
#: edit_format values that offer apply_patch (the profile word, and the plan's short one).
PATCH_FORMATS = frozenset({"apply_patch", "patch"})


def _profile_settings(config: BotConfig) -> dict[str, Any]:
    """The model profile's values for the loop (a local model; the web chat has no profile)."""
    if not getattr(config, "is_local", False):
        return {}
    try:
        profile = model_api.model_profile(config)
    except Exception as exc:  # a profile helps; it must never stop a session
        log.debug(f"model profile: {exc}")
        return {}
    values = {key: getattr(profile, key, None) for key in PROFILE_SETTINGS}
    return {key: value for key, value in values.items() if value is not None}


def offers_patch(settings: dict[str, Any]) -> bool:
    """apply_patch is offered only to models whose edit format is patches (profile or setting)."""
    return str(settings.get("edit_format") or "").strip().lower() in PATCH_FORMATS


def _loop_settings(config: BotConfig, settings: dict[str, Any]) -> dict[str, Any]:
    """The settings the loop reads: the model profile's values under the settings file's, and
    chat-size defaults that fit a local model's context window.

    The browser defaults (a 300000-character chat, 40000-character tool results) assume a
    hosted model with a large context. A local model has ``local.num_ctx`` tokens; a chat
    bigger than that is silently truncated, so a new chat starts well before that.
    """
    profile = _profile_settings(config)
    num_ctx = model_api.context_tokens(config)
    if num_ctx is None and not profile:
        return settings
    merged = {**profile, **dict(settings)}
    if num_ctx is not None:
        # about 3 characters per token of code; leave room for the reply and the instructions
        merged.setdefault("compact_after_chars", max(10_000, int(num_ctx * 2)))
        merged.setdefault("max_result_chars", max(4_000, min(DEFAULT_RESULT_CHARS, int(num_ctx / 2))))
    return merged


def _result_budget(config: BotConfig, settings: dict[str, Any]) -> int:
    """Characters per message sent back to the chat. Smaller keeps the task in view."""
    raw = _loop_settings(config, settings).get("max_result_chars")
    if isinstance(raw, int) and raw >= 4_000:
        return raw
    return min(DEFAULT_RESULT_CHARS, config.max_prompt_chars)


def _resolve_role(flag: str | None, settings: dict[str, Any], root: Path | None = None) -> str:
    """The role id for this run: ``--role``, then ``"role"`` in settings, then Generic. Unknown names warn.

    The name of a file-defined agent (``/agent``) counts too: its instructions become the role.
    """
    for source, raw in (("--role", flag), ('settings "role"', settings.get("role"))):
        if raw is None or str(raw).strip() == "":
            continue
        found = roles.normalize_role(raw)
        if found is not None:
            return found
        if root is not None and _file_agent(raw, root) is not None:
            return str(raw).strip()
        log.warn(f"{source}: {raw!r} is not one of {', '.join(roles.ROLE_IDS)}; using {roles.GENERIC}")
    return roles.GENERIC


_SHELL_SETTINGS = ("auto", "pwsh", "powershell", "cmd", "bash", "sh", "zsh", "gitbash", "git-bash")


def _shell_preference(settings: dict[str, Any]) -> str:
    """The ``"shell"`` setting (auto, pwsh, powershell, cmd, bash, sh, zsh); auto when unset or unknown."""
    value = str(settings.get("shell") or "auto").strip().lower()
    if value not in _SHELL_SETTINGS:
        log.warn(f'settings "shell": {value!r} is not one of {", ".join(_SHELL_SETTINGS[:7])}; using auto')
        return "auto"
    return value


def _prewarm_shell_environment() -> None:
    """Start reading the login-shell environment now, so the first command does not wait."""
    try:
        agent_shell.prewarm_login_environment()
    except Exception as exc:  # noqa: BLE001
        log.warn(f"login environment: {exc}")


def _available_shells() -> dict[str, agent_shell.Shell]:
    try:
        return agent_shell.available_shells()
    except Exception as exc:  # noqa: BLE001
        log.warn(f"listing shells failed: {exc}")
        return {}


def _open_shell_session(
    workspace: Path,
    shell: agent_shell.Shell,
    available: dict[str, agent_shell.Shell] | None = None,
    *,
    offline: bool = False,
) -> Any:
    """A persistent ShellSession when agent_shell provides one, else ``None``."""
    factory = getattr(agent_shell, "ShellSession", None)
    if factory is None:
        return None
    try:
        return factory(workspace, shell, available=available, offline=offline)
    except TypeError:
        return factory(workspace, shell, available=available)
    except Exception as exc:
        log.warn(f"shell session unavailable: {exc}")
        return None


def _remove_old_codegraph() -> None:
    """Delete the CodeGraph copy earlier versions unpacked (crit's own folder; nothing else uses it)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        folder = base / "critique-bot" / "codegraph"
    elif sys.platform == "darwin":
        folder = Path.home() / "Library" / "Application Support" / "critique-bot" / "codegraph"
    else:
        folder = Path.home() / ".config" / "critique-bot" / "codegraph"
    if not folder.is_dir():
        return
    try:
        shutil.rmtree(folder)
        log.info(f"removed the old CodeGraph copy at {folder}")
    except OSError as exc:
        log.debug(f"could not remove {folder}: {exc}")


def without_tool_lines(instructions: str, hidden: Iterable[str]) -> str:
    """The tag format's instructions without the lines that describe ``hidden`` tools."""
    names = tuple(f"{name}:" for name in hidden)
    if not names:
        return instructions
    return "\n".join(line for line in instructions.split("\n") if not line.startswith(names))
