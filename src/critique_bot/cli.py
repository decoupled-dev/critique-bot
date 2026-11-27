# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from critique_bot import config_prompts, env_names, log, model_api, privacy
from critique_bot.config import (
    BotConfig,
    ConfigError,
    compose_prompt_from_args,
    default_prompt_template_path,
    load_config,
)
from critique_bot.model_api import save_failure
from critique_bot.output import isoformat, write_output
from critique_bot.patch import (
    NOTE_RESERVE_CHARS,
    InputError,
    InputLimits,
    LoadedInput,
    cap_text,
    changed_file_paths,
    finalize_prompt,
    load_path,
    load_stdin,
    sanitize_attachments,
    sanitize_one,
)
from critique_bot.review_session import (
    PromptPayload,
    run_review_session,
    sanitize_context_files,
    split_review_payload,
)
from critique_bot.workspace import (
    DEFAULT_PATCH_NAME,
    EmptyDiff,
    WorkspaceError,
    load_changed_files,
    prepare_workspace_patch,
    should_prepare_workspace,
)

MODE_REVIEW = "review"
MODE_GENERAL = "general"
MODE_CHAT = "chat"
MODE_AGENT = "agent"
REVIEW_QUICK = "quick"
REVIEW_AGENT = "agent"
OUTPUT_STEM = {
    MODE_REVIEW: "review",
    MODE_GENERAL: "reply",
    MODE_CHAT: "chat",
    MODE_AGENT: "agent",
}
_CHAT_QUIT = {"exit", "quit", "/exit", "/quit", "/q"}


def _read_text_file(
    path: str | Path,
    limits: InputLimits,
    *,
    label: str = "file",
    allow_binary: bool = True,
) -> LoadedInput:
    file_path = Path(path)
    log.info(f"reading {label} from {file_path}")
    try:
        loaded = load_path(file_path, limits, name=str(path))
    except InputError as exc:
        raise ConfigError(f"{label} not found: {file_path}" if "not found" in str(exc) else str(exc)) from exc
    if loaded.binary and not allow_binary:
        raise ConfigError(f"{label} is binary, not text: {file_path}")
    if not loaded.text.strip():
        raise ConfigError(f"{label} is empty: {file_path}")
    extra = []
    if loaded.truncated_read:
        extra.append("truncated")
    if loaded.binary:
        extra.append("binary omitted")
    suffix = f", {', '.join(extra)}" if extra else ""
    log.info(
        f"{label} loaded ({len(loaded.text)} chars, "
        f"{loaded.text.count(chr(10)) + 1} lines{suffix})"
    )
    return loaded


def _read_stdin_patch(limits: InputLimits) -> LoadedInput:
    log.info("reading patch from stdin")
    if sys.stdin.isatty():
        raise ConfigError(
            "review mode needs a patch: pass --patch-file / FILE, "
            "or pipe a patch on stdin. For a one-shot prompt, use "
            "--mode general --prompt '...'. To talk interactively, use "
            "--mode chat"
        )
    loaded = load_stdin(limits)
    if not loaded.text.strip():
        raise ConfigError("patch is empty")
    extra = []
    if loaded.truncated_read:
        extra.append("truncated")
    if loaded.binary:
        extra.append("binary omitted")
    suffix = f", {', '.join(extra)}" if extra else ""
    log.info(
        f"patch loaded ({len(loaded.text)} chars, "
        f"{loaded.text.count(chr(10)) + 1} lines{suffix})"
    )
    return loaded


def _resolve_mode(args: argparse.Namespace) -> str:
    has_prompt = bool(args.prompt or args.prompt_file)
    mode = args.mode or (MODE_GENERAL if has_prompt else MODE_REVIEW)
    if mode == MODE_REVIEW and has_prompt:
        raise ConfigError(
            "--mode review uses the review template; "
            "use --mode general with --prompt / --prompt-file, "
            "or --mode chat to talk interactively"
        )
    if mode == MODE_GENERAL and not has_prompt:
        raise ConfigError("--mode general requires --prompt or --prompt-file")
    if mode != MODE_REVIEW and args.prompt_template:
        raise ConfigError("--prompt-template is only used in --mode review")
    if mode == MODE_AGENT and args.prompt and args.paths:
        raise ConfigError(
            "pass the task as a positional string or --prompt, not both"
        )
    return mode


def _collect_attachments(
    patch_file: str | None,
    files: list[str] | None,
    limits: InputLimits,
    *,
    allow_stdin: bool,
) -> list[LoadedInput]:
    paths: list[str] = []
    if patch_file:
        paths.append(patch_file)
    paths.extend(files or [])
    if paths:
        open_cap = max(limits.max_files * 3, 120)
        if len(paths) > open_cap:
            log.warn(
                f"{len(paths)} input files; reading the first {open_cap} "
                f"(increase max_files if you need more)"
            )
        per_file_limits = limits
        if len(paths) > 1:
            per_read = min(
                limits.max_read_bytes,
                max(limits.max_file_chars * 8, limits.max_prompt_chars * 2, 256_000),
            )
            per_file_limits = InputLimits(
                max_prompt_chars=limits.max_prompt_chars,
                max_file_chars=limits.max_file_chars,
                max_files=limits.max_files,
                max_read_bytes=per_read,
            )
        return [_read_text_file(path, per_file_limits) for path in paths[:open_cap]]
    if not allow_stdin:
        return []
    return [_read_stdin_patch(limits)]


def _load_template(path: Path, limits: InputLimits) -> str:
    log.info(f"loading prompt template {path}")
    return _read_text_file(
        path, limits, label="prompt template", allow_binary=False
    ).text


def _build_prompt(
    args: argparse.Namespace,
    mode: str,
    limits: InputLimits,
    config=None,
) -> str:
    return _build_prompt_payload(args, mode, limits, config).prompt


def _build_prompt_payload(
    args: argparse.Namespace,
    mode: str,
    limits: InputLimits,
    config=None,
) -> PromptPayload:
    extra_files = list(args.files or []) + list(args.paths or [])
    if mode in (MODE_GENERAL, MODE_CHAT):
        loaded_attachments = _collect_attachments(
            args.patch_file,
            extra_files,
            limits,
            allow_stdin=False,
        )
        raw_attachments = [(item.name, item.text) for item in loaded_attachments]
        read_capped = any(item.truncated_read for item in loaded_attachments)
        has_input = bool(
            args.prompt
            or args.prompt_file
            or args.patch_file
            or args.files
            or args.paths
        )
        if mode == MODE_CHAT and not has_input:
            return PromptPayload(prompt="")
        if args.prompt_file:
            prompt_text = _read_text_file(
                args.prompt_file, limits, label="prompt file", allow_binary=False
            ).text
        else:
            prompt_text = args.prompt or ""
        prompt_text = cap_text(
            prompt_text,
            max(limits.max_prompt_chars // 2, 8_000),
            what="prompt text",
        )
        if not prompt_text.strip() and raw_attachments:
            prompt_text = "Please look at the attached file(s)."
        overhead = (
            len(prompt_text) - 7
            if ("{patch}" in prompt_text or "{files}" in prompt_text)
            else len(prompt_text)
        )
        attachments, stats = sanitize_attachments(
            raw_attachments,
            limits,
            extra_overhead=overhead + NOTE_RESERVE_CHARS,
        )
        stats.truncated_read = stats.truncated_read or read_capped
        prompt = finalize_prompt(
            compose_prompt_from_args(prompt_text, attachments),
            limits,
            stats,
        )
        log.info(
            f"composed {mode} prompt ({len(prompt)} chars, "
            f"{len(attachments)} file(s)"
            + (", sanitized" if stats.did_sanitize else "")
            + ")"
        )
        return PromptPayload(prompt=prompt)

    return _build_review_prompt(args, extra_files, limits, config)


def _build_review_prompt(
    args: argparse.Namespace,
    extra_files: list[str],
    limits: InputLimits,
    config=None,
) -> PromptPayload:
    repo_dir = Path(getattr(args, "repo_dir", None) or ".")
    patch_input, extra_loaded, mr_context = _load_review_inputs(args, extra_files, limits, config)

    context_inputs = list(extra_loaded)
    seen = {item.name for item in context_inputs}
    for item in load_changed_files(repo_dir, patch_input.text, limits):
        if item.name in seen:
            continue
        context_inputs.append(item)
        seen.add(item.name)

    template = _load_template(_review_template_path(args), limits)
    patch_body, patch_stats = _sanitized_patch(patch_input, limits)
    file_pairs = [(item.name, item.text) for item in context_inputs]
    file_attachments, file_stats = sanitize_context_files(file_pairs, limits)
    patch_stats.merge(file_stats)
    patch_stats.truncated_read = patch_stats.truncated_read or patch_input.truncated_read
    if any(item.truncated_read for item in context_inputs):
        patch_stats.truncated_read = True
    payload = split_review_payload(
        template,
        patch_body,
        mr_context,
        file_attachments,
        limits,
        patch_stats,
        changed_path_count=len(changed_file_paths(patch_input.text)),
    )
    log.info(
        f"composed review prompt ({len(payload.prompt)} chars"
        + (f", {len(file_attachments)} file(s)" if file_attachments else "")
        + (", staged" if payload.files or payload.patch else "")
        + (", sanitized" if patch_stats.did_sanitize else "")
        + (", gitlab mr context" if mr_context else "")
        + ")"
    )
    return payload


def _review_template_path(args: argparse.Namespace) -> Path:
    return Path(args.prompt_template) if args.prompt_template else default_prompt_template_path()


def _sanitized_patch(patch_input: LoadedInput, limits: InputLimits):
    patch_budget = max(limits.max_prompt_chars - NOTE_RESERVE_CHARS, 2_000)
    patch_body, patch_stats = sanitize_one(
        patch_input.name,
        patch_input.text,
        limits,
        remaining_chars=patch_budget,
        remaining_files=limits.max_files,
    )
    patch_stats.truncated_read = patch_input.truncated_read
    return patch_body, patch_stats


def _load_review_inputs(
    args: argparse.Namespace,
    extra_files: list[str],
    limits: InputLimits,
    config=None,
) -> tuple[LoadedInput, list[LoadedInput], str]:
    """The patch, any extra files named on the command line, and the GitLab MR context."""
    repo_dir = Path(getattr(args, "repo_dir", None) or ".")
    patch_file = args.patch_file
    patch_input: LoadedInput | None = None
    extra_loaded: list[LoadedInput] = []

    if should_prepare_workspace(patch_file=patch_file, extra_files=extra_files):
        write_to = Path(getattr(args, "write_patch", None) or DEFAULT_PATCH_NAME)
        try:
            text = prepare_workspace_patch(repo_dir, write_to)
        except EmptyDiff:
            raise
        except WorkspaceError as exc:
            text = _workspace_patch_from_gitlab(config, write_to, exc)
            if text is None:
                raise ConfigError(str(exc)) from exc
        patch_input = LoadedInput(name=str(write_to), text=text)
        patch_file = str(write_to)

    if patch_input is None:
        loaded = _collect_attachments(
            patch_file, extra_files, limits, allow_stdin=False
        )
        patch_input, extra_loaded = _split_patch_and_files(loaded, patch_file)
    elif extra_files:
        extra_loaded = _collect_attachments(
            None, extra_files, limits, allow_stdin=False
        )

    mr_context = ""
    if config is not None:
        fetched = _load_gitlab_mr_context(
            config, need_patch=patch_input is None
        )
        if fetched is not None:
            from critique_bot.gitlab import format_mr_context

            mr_context = format_mr_context(fetched)
            if patch_input is None and fetched.patch.strip():
                patch_input = LoadedInput(
                    name="merge_request.diff", text=fetched.patch
                )
                log.info(
                    f"using merge request diff from GitLab "
                    f"({len(fetched.patch)} chars)"
                )

    if patch_input is None:
        loaded = _collect_attachments(
            patch_file, extra_files, limits, allow_stdin=True
        )
        patch_input, extra_loaded = _split_patch_and_files(loaded, patch_file)

    if patch_input is None:
        raise ConfigError("review mode needs a patch")
    return patch_input, extra_loaded, mr_context


def _split_patch_and_files(
    loaded: list[LoadedInput],
    patch_file: str | None,
) -> tuple[LoadedInput | None, list[LoadedInput]]:
    """First attachment is the patch when ``--patch-file`` was set; else all files."""
    if not loaded:
        return None, []
    if patch_file:
        return loaded[0], loaded[1:]
    if len(loaded) == 1:
        return loaded[0], []
    return loaded[0], loaded[1:]


def _workspace_patch_from_gitlab(config, write_to: Path, exc: WorkspaceError) -> str | None:
    """Use GitLab's MR diffs when the job checkout cannot compute a merge-base."""
    log.warn(f"local git diff failed ({exc}); trying GitLab MR diffs")
    fetched = _load_gitlab_mr_context(config, need_patch=True)
    if fetched is None or not fetched.patch.strip():
        return None
    write_to = Path(write_to)
    write_to.parent.mkdir(parents=True, exist_ok=True)
    write_to.write_text(fetched.patch, encoding="utf-8")
    log.info(f"wrote {write_to} from GitLab API ({len(fetched.patch)} chars)")
    return fetched.patch


def _load_gitlab_mr_context(config, *, need_patch: bool):
    """Fetch MR title, tickets, description, commits, and diffs when targeting is set."""
    from critique_bot.gitlab import GitLabError, fetch_mr_context, resolve_target

    gitlab_cfg = getattr(config, "gitlab", None) if config is not None else None
    configured = (
        getattr(gitlab_cfg, "base_url", "")
        or env_names.get("CRIT_GITLAB_URL")
        or os.environ.get("CI_API_V4_URL")
        or os.environ.get("CI_SERVER_URL")
    )
    if not configured and os.environ.get("CI_MERGE_REQUEST_URL"):
        # A merge request is named but crit does not know the GitLab instance yet: ask (terminal only).
        asked = config_prompts.ensure_gitlab_url("")
        if asked:
            from critique_bot.config import GitLabConfig

            gitlab_cfg = GitLabConfig(base_url=asked)
    try:
        target = resolve_target(gitlab_cfg)
    except GitLabError as exc:
        log.warn(f"could not load GitLab MR context: {exc}")
        return None
    if not target.api_url or not target.project_id or not target.mr_iid:
        return None
    if not target.token:
        log.warn("skipping GitLab MR context; no CRIT_GITLAB_TOKEN")
        return None
    try:
        ctx = fetch_mr_context(target, include_patch=True)
    except GitLabError as exc:
        log.warn(f"could not load GitLab MR context: {exc}")
        return None
    log.info(
        "loaded GitLab MR context "
        + log.kv(
            project=target.project_id,
            mr=target.mr_iid,
            title=log.preview(ctx.title),
            tickets=",".join(ctx.tickets) or None,
            commits=len(ctx.commits),
        )
    )
    if need_patch and not ctx.patch.strip():
        log.warn("GitLab MR diffs were empty")
    return ctx


_CHAT_HELP = (
    "commands:\n"
    "  exit, quit, /q     leave the session\n"
    "  /help              show this help\n"
    "  /file PATH [text]  attach a file to this turn\n"
    "  end a line with \\  continue on the next line"
)


def _print_assistant(reply: str) -> None:
    log.print_safe(log.paint(reply, log.MODEL_COLOR), flush=True)
    log.print_safe(flush=True)


def _read_chat_message() -> str | None:
    chunks: list[str] = []
    while True:
        prefix = "You> " if not chunks else "... "
        try:
            line = input(prefix)
        except EOFError:
            print(flush=True)
            if chunks:
                return "\n".join(chunks).rstrip()
            return None
        except KeyboardInterrupt:
            print(flush=True)
            return None
        stripped = line.strip()
        if not chunks:
            if not stripped:
                continue
            if stripped.lower() in _CHAT_QUIT:
                return None
        if line.endswith("\\") and not line.endswith("\\\\"):
            chunks.append(line[:-1])
            continue
        chunks.append(line)
        return "\n".join(chunks).rstrip()


def _chat_file_turn(command: str, limits: InputLimits) -> str:
    parts = command.split(None, 2)
    if len(parts) < 2:
        raise ConfigError("/file needs a path, e.g. /file notes.txt explain this")
    path = parts[1]
    extra = parts[2].strip() if len(parts) > 2 else ""
    loaded = _read_text_file(path, limits)
    prompt_text = extra or f"Please look at {loaded.name}."
    attachments, stats = sanitize_attachments(
        [(loaded.name, loaded.text)],
        limits,
        extra_overhead=len(prompt_text) + NOTE_RESERVE_CHARS,
    )
    return finalize_prompt(
        compose_prompt_from_args(prompt_text, attachments),
        limits,
        stats,
    )


def _format_chat_transcript(turns: list[dict[str, str]]) -> str:
    parts = ["# Conversation", ""]
    for turn in turns:
        heading = "You" if turn["role"] == "user" else "Assistant"
        parts.append(f"## {heading}")
        parts.append("")
        parts.append(turn["content"].rstrip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _run_chat_session(session, config, first_prompt: str) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []

    def send(payload: str) -> None:
        reply = session.send(payload)
        _print_assistant(reply)
        config_prompts.offer_blocked_hosts()
        if not reply.strip():
            log.warn("assistant returned an empty reply")
        turns.append({"role": "user", "content": payload})
        turns.append({"role": "assistant", "content": reply})

    if first_prompt.strip():
        send(first_prompt)

    print(
        "Session ready. Type a message, /help, or exit / Ctrl-D to quit.",
        file=sys.stderr,
    )
    while True:
        message = _read_chat_message()
        if message is None:
            break
        if message.strip().lower() == "/help":
            print(_CHAT_HELP, file=sys.stderr)
            continue
        if message.lower() == "/file" or message.lower().startswith("/file "):
            try:
                payload = _chat_file_turn(message, config.input_limits)
            except ConfigError as exc:
                log.notice(f"Problem: {exc}")
                continue
        else:
            payload = cap_text(message, config.max_prompt_chars, what="message")
        if not payload.strip():
            continue
        send(payload)
    return turns


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crit",
        usage="crit [MODE] [options] [TASK or FILE ...]",
        description=(
            "One command for everything crit does. The first word picks the mode; "
            "with no mode word crit runs the local coding agent. The model is a web page "
            "in Edge or a model on this PC (see crit config)."
        ),
        epilog=(
            "modes:\n"
            "  crit [TASK]             coding agent (default): crit \"fix the build\"\n"
            "  crit review [PATCH]     review a patch: stdin, --patch-file, or the CI checkout\n"
            "  crit ask --prompt TEXT  one-shot prompt with optional files (alias: general)\n"
            "  crit chat               interactive conversation in this terminal\n"
            "\n"
            "service commands:\n"
            "  crit config             see or change the settings (crit config --help)\n"
            "  crit setup              web UI to pick the page's selectors by clicking\n"
            "  crit worker             queue worker on the runner PC (owns the signed-in Edge)\n"
            "  crit submit             enqueue a review for the worker and wait for it\n"
            "  crit queue-status       worker liveness, queued jobs, recent results\n"
            "  crit gitlab-post        post review.md on a GitLab merge request\n"
            "\n"
            "agent extras:\n"
            "  crit init               rebuild the .crit index\n"
            "  crit undo               restore the files the last task changed\n"
            "  crit --yes TASK         auto mode: no approval prompts\n"
            "  crit --plan TASK        plan first, approve, then build\n"
            "  crit --acp              Agent Client Protocol agent for JetBrains IDEs and Zed\n"
            "\n"
            "Settings come from --config, CRIT_CONFIG, the project's own config, or your\n"
            "user settings (crit config --path); the first run asks for them.\n"
            "Quote a task that starts with a mode word:\n"
            "  crit \"review the auth code\"\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        help=(
            "settings file to use instead of the default (crit config shows which one that is; "
            "the first run creates it)"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=(MODE_REVIEW, MODE_GENERAL, MODE_CHAT, MODE_AGENT),
        help=(
            "same as naming the mode as the first word (crit review). "
            f"{MODE_REVIEW}: code-review template + patch. "
            f"{MODE_GENERAL}: send --prompt and optional files as-is. "
            f"{MODE_CHAT}: interactive conversation in this terminal "
            f"(--prompt is optional as the first message). "
            f"{MODE_AGENT} (crit's default): local coding harness"
        ),
    )
    prompt_src = parser.add_mutually_exclusive_group()
    prompt_src.add_argument(
        "--prompt",
        help="prompt text to send (--mode general, or first message in --mode chat)",
    )
    prompt_src.add_argument(
        "--prompt-file",
        help="read prompt text from a file (--mode general, or first message in --mode chat)",
    )
    parser.add_argument(
        "--file",
        dest="files",
        action="append",
        metavar="PATH",
        help="file to include in the prompt (repeatable; patch, source, or any text file)",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="FILE",
        help=(
            "files to include in the prompt (same as --file). "
            "In agent mode this is the task text"
        ),
    )
    parser.add_argument(
        "--patch-file",
        help=(
            "patch/diff to include; in review mode, omit in GitLab CI to build "
            "it from the job checkout, or pipe a patch on stdin locally"
        ),
    )
    parser.add_argument(
        "--include-changed-files",
        action="store_true",
        help=(
            "load HEAD contents of paths in the patch from --repo-dir. "
            "Review mode always does this; the flag is kept for older scripts"
        ),
    )
    parser.add_argument(
        "--repo-dir",
        default=".",
        help="git checkout to read changed files from (default: current directory)",
    )
    parser.add_argument(
        "--write-patch",
        metavar="PATH",
        help=(
            f"where to write a generated git diff (default: {DEFAULT_PATCH_NAME} "
            "when building from the CI checkout)"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="out",
        help="directory for the reply (review.md, reply.md, or chat.md), JSON, and failure screenshots",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window (a missing Edge profile opens the window on its own)",
    )
    parser.add_argument(
        "--cdp-url",
        help="attach to a running Edge (e.g. http://127.0.0.1:9222) instead of launching a new window",
    )
    parser.add_argument(
        "--model",
        help="override config/env model name (visible dropdown label)",
    )
    parser.add_argument(
        "--prompt-template",
        help="template file containing a {patch} placeholder (review mode)",
    )
    engine = parser.add_mutually_exclusive_group()
    engine.add_argument(
        "--quick",
        dest="review_engine",
        action="store_const",
        const=REVIEW_QUICK,
        help=(
            "review mode: one message with the patch and the changed files, no tools "
            "(what the CI worker runs)"
        ),
    )
    engine.add_argument(
        "--deep",
        dest="review_engine",
        action="store_const",
        const=REVIEW_AGENT,
        help=(
            "review mode: the Code Reviewer reads the project with read-only tools before it reports "
            "(crit's default)"
        ),
    )
    parser.add_argument("--crit-defaults", dest="crit_defaults", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--resume",
        dest="resume_session",
        default=None,
        metavar="ID",
        help="agent mode: continue a logged session (an id from crit sessions, or last); same as crit resume",
    )
    parser.add_argument(
        "--no-memory",
        dest="no_memory",
        action="store_true",
        help='agent mode: no project memory for this run: no hunch at task start, nothing recorded ("memory": "off")',
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help="optional cap on agent tool rounds; omit to run until the model stops",
    )
    parser.add_argument(
        "--yes",
        "--auto",
        dest="auto_approve",
        action="store_true",
        help=(
            "agent mode: run edits, commands, and fetches without asking "
            '(same as "permissions": "auto" in .crit/settings.json)'
        ),
    )
    parser.add_argument(
        "--plan",
        dest="plan_mode",
        action="store_true",
        help="agent mode: start in plan mode (read and propose a plan; you approve before anything changes)",
    )
    parser.add_argument(
        "--permission-mode",
        dest="agent_mode",
        choices=("ask", "edits", "auto", "plan"),
        default=None,
        help="agent mode: ask (default), edits (auto-accept file edits), auto (no prompts except risky commands), plan",
    )
    parser.add_argument(
        "--role",
        default=None,
        help=(
            "agent mode: the role crit plays in this project, which shapes the instructions: "
            "code-reviewer, aosp-expert, expert-developer, ai-expert, test-engineer, android-app-engineer, or generic "
            '(stored as "role" in .crit/settings.json the first time; the first interactive run asks)'
        ),
    )
    parser.add_argument(
        "--tool-format",
        choices=("tags", "openai"),
        default=None,
        help=(
            "agent and review: how the model calls tools. openai (default): OpenAI Responses API items "
            '(function_call / function_call_output); tags: the older <tool_call> blocks ("tool_format" in .crit/settings.json)'
        ),
    )
    parser.add_argument(
        "--tool-set",
        choices=("full", "minimal"),
        default=None,
        help=(
            "agent mode: minimal gives the model the shell plus read_files, edit_file, and write_files; "
            'it searches, lists, and runs git through the shell ("tool_set" in .crit/settings.json)'
        ),
    )
    parser.add_argument(
        "--acp",
        action="store_true",
        help=(
            "agent mode: run as an Agent Client Protocol agent on stdin/stdout, for Android Studio, "
            "JetBrains IDEs, and Zed; the folder comes from the client's session (see docs/acp.md)"
        ),
    )
    parser.add_argument(
        "--logs",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="write diagnostic logs to stderr (default: off)",
    )
    parser.add_argument(
        "--label",
        help=(
            "short id for this job (shown in the queue filename). "
            "Default: GitLab MR IID, CI job id, or 'local'"
        ),
    )
    return parser


def _log_config(config) -> None:
    log.info(
        "config loaded "
        + log.kv(
            backend=config.backend,
            url=config.url or None,
            model=config.model or "(none)",
            timeout_ms=config.timeout_ms,
            idle_ms=config.idle_ms,
            max_prompt_chars=config.max_prompt_chars,
            max_file_chars=config.max_file_chars,
            max_files=config.max_files,
            max_read_bytes=config.max_read_bytes,
            user_data_dir=config.user_data_dir,
            cdp_url=config.cdp_url,
            queue_dir=config.queue_dir,
            min_interval_seconds=config.min_interval_seconds,
            interval_jitter_seconds=config.interval_jitter_seconds,
            turn_pause_seconds=config.turn_pause_seconds,
            max_parallel_tabs=config.max_parallel_tabs,
            storage_state=config.storage_state,
            prompt_input=config.selectors.prompt_input,
            send_button=config.selectors.send_button or "(Enter)",
            assistant_messages=config.selectors.assistant_messages,
            model_dropdown=config.selectors.model_dropdown or "(auto)",
            model_dropdown_identifier=(
                config.selectors.model_dropdown_identifier or "(none)"
            ),
            gitlab_base_url=config.gitlab.base_url or "(none)",
        )
    )


def _ci_meta() -> dict[str, str]:
    keys = (
        "CI_JOB_ID",
        "CI_JOB_NAME",
        "CI_PIPELINE_ID",
        "CI_PROJECT_PATH",
        "CI_PROJECT_ID",
        "CI_MERGE_REQUEST_IID",
        "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME",
        "CI_COMMIT_SHA",
        "CI_COMMIT_REF_NAME",
    )
    return {key: os.environ[key] for key in keys if os.environ.get(key)}


def _copy_job_results(src: Path, dest: Path, stem: str) -> None:
    """Copy the review and its status into the CI job's output folder.

    ``job.json`` (the whole prompt, source included) and a failure's ``screenshot.png`` and
    ``page.html`` (the signed-in chat page, with the list of other conversations) stay in the
    worker's queue folder on this PC: the output folder is uploaded as a CI artifact.
    """
    dest.mkdir(parents=True, exist_ok=True)
    names = (
        f"{stem}.md",
        f"{stem}.json",
        "status.json",
    )
    for name in names:
        item = src / name
        if item.is_file():
            shutil.copy2(item, dest / name)


def _config_error(exc: BaseException) -> int:
    log.error(f"config error: {exc}")
    log.notice(f"Setup problem: {exc}")
    return 1


def build_worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crit worker",
        description=(
            "Keep one worker process open and process review jobs from the "
            "on-disk queue. One signed-in Edge instance. GitLab jobs "
            "should call `crit submit`, not this command."
        ),
    )
    parser.add_argument("--config", help="settings file (default: the one crit config shows)")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window (a missing Edge profile opens the window on its own)",
    )
    parser.add_argument(
        "--cdp-url",
        help="attach to a running Edge (e.g. http://127.0.0.1:9222)",
    )
    parser.add_argument("--model", help="override config/env model name")
    parser.add_argument(
        "--logs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="write diagnostic logs to stderr (default: on for worker)",
    )
    return parser


def _add_submit_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=1800,
        metavar="SEC",
        help="seconds to wait for the worker to finish this job (default: 1800)",
    )


SUBCOMMANDS = {
    "config": "see or change crit's settings (crit config --help)",
    "worker": "run the long-lived queue worker on the runner PC",
    "submit": "enqueue a review and wait for the worker to finish it",
    "setup": "open a local web UI to configure selectors by clicking them",
    "queue-status": "show worker liveness, queued jobs, and recent results",
    "gitlab-post": "post the review on a GitLab merge request",
    "eval": "measure crit (or opencode) on a suite of real tasks (crit eval --help)",
    "audit": "show what crit did: commands, file writes, network requests, permissions (crit audit --help)",
    "doctor": "check this machine: crit doctor model (the model server) or crit doctor sandbox",
    "sessions": "list, show, export, or prune this project's logged sessions (crit sessions --help)",
    "resume": "continue a logged session: crit resume [<id>|--last] [task or options]",
    "fork": "copy a logged session up to an event and continue the copy: crit fork <id> [--at SEQ]",
    "trust": "approve the project's extension files (tools, hooks, MCP servers) once per content",
    "tool": "list, show and test manifest tools (crit tool --help)",
    "mcp": "list MCP servers; approve a server's changed tool definitions",
    "memory": "the project memory: status, show, review proposed conventions, export, prune (crit memory --help)",
}


# A mode named as the first word, as in ``crit review`` or ``crit chat``, is the
# same as ``--mode``. ``ask`` is a shorter name for general.
MODE_WORDS = {
    MODE_REVIEW: MODE_REVIEW,
    MODE_GENERAL: MODE_GENERAL,
    "ask": MODE_GENERAL,
    MODE_CHAT: MODE_CHAT,
    MODE_AGENT: MODE_AGENT,
}


def main(argv: list[str] | None = None) -> int:
    """Run crit. Any failure ends in a plain sentence, never a traceback (that goes to the log file)."""
    try:
        _migrate_legacy_files()
        return _main(argv)
    except KeyboardInterrupt:
        log.notice("Stopped.")
        return 130
    except PermissionError as exc:
        log.exception(f"refused: {exc}")
        log.notice(f"Blocked: {exc}")
        return 1
    except Exception as exc:  # the last line of defense: a readable message, details in the log file
        log.exception(f"unexpected error: {exc}")
        lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
        detail = (lines[0] if lines else type(exc).__name__)[:300].rstrip(".")
        log.notice(f"crit stopped: {detail}. {log.details_hint()}")
        return 1
    finally:
        config_prompts.disable()


def _migrate_legacy_files() -> None:
    """Move the files of crit before 1.1 into ~/.crit, once (see config.migrate_legacy_files)."""
    from critique_bot.config import migrate_legacy_files

    try:
        migrate_legacy_files()
    except Exception as exc:  # moving old files must never stop the command
        log.debug(f"moving the old settings failed: {exc}")


def _main_config(argv: list[str]) -> int:
    """``crit config``: show or change the settings (see config_cmd)."""
    from critique_bot.config_cmd import main as config_main

    return config_main(argv)


def _may_ask(args: argparse.Namespace | None = None) -> bool:
    """True when crit may ask questions: a terminal on both ends, not CI, not an ACP client."""
    from critique_bot.welcome import stdio_is_tty

    if args is not None and getattr(args, "acp", False):
        return False
    if os.environ.get("CI"):
        return False
    return stdio_is_tty()


def _find_config(explicit: str | None, repo_dir: str | Path = ".", *, ask: bool = True) -> Path | int:
    """The config file to use: found (see config_cmd.resolve_config_path), or set up now.

    No config anywhere: in a terminal the first-run questions write the user config and
    the command goes on; otherwise a sentence says which ``crit config`` command sets it up.
    An exit code when the command cannot go on.
    """
    from critique_bot.config_cmd import NOT_SET_UP, resolve_config_path

    try:
        location = resolve_config_path(explicit, repo_dir)
    except ConfigError as exc:
        return _config_error(exc)
    if location.exists:
        return location.path
    if location.source == "user":
        found = _use_found_config_file(location.path)
        if found is not None:
            return found
    if not (ask and _may_ask()):
        if location.source in ("flag", "env"):
            return _config_error(ConfigError(f"config file not found: {location.path}"))
        log.error("no config file found")
        log.notice(NOT_SET_UP)
        return 1
    return _first_run_wizard(location.path)


def _use_found_config_file(target: Path) -> Path | int | None:
    """A config file found without asking (CRIT_CONFIG_FILE, the system location): set up
    ``target`` with it and say so. None when there is none; an exit code when it is unusable."""
    from critique_bot.setup_wizard import use_found_config_file

    try:
        result = use_found_config_file(target, say=log.info)
    except ConfigError as exc:
        return _config_error(exc)
    return None if result is None else result.path


def _first_run_wizard(target: Path, *, reconfigure: bool = False) -> Path | int:
    """Ask the setup questions and write ``target``. The config path to go on with, or an exit code."""
    from critique_bot.setup_wizard import run_wizard

    if reconfigure and target.is_file():
        answer = input(f"Replace {target}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Kept the current settings.")
            return 0
    if not reconfigure:
        found = _use_found_config_file(target)
        if found is not None:
            return found
    try:
        result = run_wizard(target)
    except ConfigError as exc:
        return _config_error(exc)
    if result is None:
        return 1
    return result.path


def _agent_confirm(question: str) -> bool:
    """A yes/no question in the agent's terminal UI (default No)."""
    from critique_bot import agent

    asker = getattr(agent.agent_ui, "ask_question", None)
    if not callable(asker):
        return False
    answer = asker(question, ["No", "Yes, allow it"])
    return str(answer or "").strip().lower().startswith("y")


def _blocked_hosts() -> list[str]:
    """Sites the chat page was blocked from (only when the browser was used in this process)."""
    if "critique_bot.webchat" not in sys.modules:
        return []
    hooks = model_api.backend_hooks("browser")
    return hooks.drain_blocked_hosts() if hooks is not None else []


def _enable_prompts(args: argparse.Namespace, config_path: str | Path, *, agent_ui: bool = False) -> None:
    """Ask for settings as they are needed (blocked sites, GitLab address), in a terminal only."""
    if not _may_ask(args):
        return
    config_prompts.enable(
        config_path,
        confirm=_agent_confirm if agent_ui else None,
        drain=_blocked_hosts,
    )


def _main(argv: list[str] | None = None) -> int:
    # Before anything starts: telemetry and update checks off for crit, Playwright,
    # the browser, and every program crit runs (see privacy.py).
    privacy.apply_to_process()
    log.configure_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in MODE_WORDS:
        if "--mode" in argv[1:] or any(arg.startswith("--mode=") for arg in argv[1:]):
            return _config_error(ConfigError(f"{argv[0]} already names the mode; drop --mode"))
        return _main_run(["--mode", MODE_WORDS[argv[0]], *argv[1:]])
    if argv and argv[0] in SUBCOMMANDS:
        command = argv[0]
        rest = argv[1:]
        handlers = {
            "config": _main_config,
            "worker": _main_worker,
            "submit": _main_submit,
            "setup": _main_setup,
            "queue-status": _main_queue_status,
            "gitlab-post": _main_gitlab_post,
            "eval": _main_eval,
            "audit": _main_audit,
            "doctor": _main_doctor,
            "sessions": _main_sessions,
            "resume": _main_resume,
            "fork": _main_fork,
            "memory": _main_memory,
        }
        if command in {"trust", "tool", "mcp"}:
            return _main_extensions([command, *rest])
        return handlers[command](rest)
    return _main_run(argv)


def _doctor_model(argv: list[str]) -> int:
    from critique_bot import doctor

    def load(explicit: str | None) -> BotConfig | int:
        found = _find_config(explicit, ask=False)
        if isinstance(found, int):
            return found
        try:
            return load_config(found)
        except ConfigError as exc:
            return _config_error(exc)

    return doctor.main(["model", *argv], load)


def _main_audit(argv: list[str]) -> int:
    from critique_bot import audit_cmd

    return audit_cmd.main(argv)


def _doctor_sandbox(argv: list[str]) -> int:
    from critique_bot.sandbox_doctor import doctor_sandbox

    return doctor_sandbox(argv)


#: ``crit doctor <topic>``: each topic is one function taking the remaining arguments.
DOCTOR_TOPICS = {
    "model": ("the model server: type, context, profile, a 12-turn tool-call probe", _doctor_model),
    "sandbox": ("can commands run in the OS sandbox here, and how to fix it if not", _doctor_sandbox),
}


def _main_doctor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="crit doctor",
        description="Check this machine. Topics: "
        + "; ".join(f"{name}: {about}" for name, (about, _handler) in DOCTOR_TOPICS.items()),
    )
    parser.add_argument("topic", choices=sorted(DOCTOR_TOPICS), help="what to check")
    parser.add_argument("rest", nargs=argparse.REMAINDER, help="options for that check (crit doctor TOPIC --help)")
    if argv and argv[0] in DOCTOR_TOPICS:
        return DOCTOR_TOPICS[argv[0]][1](argv[1:])
    args = parser.parse_args(argv)
    return DOCTOR_TOPICS[args.topic][1](list(args.rest))
def _main_memory(argv: list[str]) -> int:
    from critique_bot.memory_cmd import main as memory_main

    return memory_main(argv)


def _main_sessions(argv: list[str]) -> int:
    from critique_bot.sessions_cmd import main_sessions

    return main_sessions(argv)


def _main_resume(argv: list[str]) -> int:
    """``crit resume [<id>|--last] ...``: the agent, continuing a logged session."""
    from critique_bot.sessions_cmd import USAGE, resume_argv, wants_help

    if wants_help(argv):
        print(USAGE["resume"])
        raise SystemExit(0)
    found = resume_argv(argv)
    if isinstance(found, str):
        log.notice(f"crit resume: {found}")
        return 1
    return _main_run(["--mode", MODE_AGENT, "--crit-defaults", *found])


def _main_fork(argv: list[str]) -> int:
    """``crit fork <id> [--at SEQ] ...``: copy a session up to an event, then continue the copy."""
    from critique_bot.sessions_cmd import USAGE, fork_argv, wants_help

    if wants_help(argv):
        print(USAGE["fork"])
        raise SystemExit(0)
    found = fork_argv(argv)
    if isinstance(found, str):
        log.notice(f"crit fork: {found}")
        return 1
    rest, line = found
    print(line, file=sys.stderr)
    if len(rest) == 2 and not _may_ask():
        return 0  # nothing to do now; crit resume <id> continues it later
    return _main_run(["--mode", MODE_AGENT, "--crit-defaults", *rest])
def _main_extensions(argv: list[str]) -> int:
    """``crit trust``, ``crit tool``, ``crit mcp`` (see extensions_cmd)."""
    from critique_bot import extensions_cmd

    command, rest = argv[0], argv[1:]
    handler = {"trust": extensions_cmd.main_trust, "tool": extensions_cmd.main_tool, "mcp": extensions_cmd.main_mcp}
    return handler[command](rest)


def _main_eval(argv: list[str]) -> int:
    from critique_bot import eval_harness

    return eval_harness.main(argv)


def _main_worker(argv: list[str]) -> int:
    parser = build_worker_parser()
    args = parser.parse_args(argv)
    log.configure(enabled=bool(args.logs))
    found = _find_config(args.config, ask=False)
    if isinstance(found, int):
        return found
    try:
        config = load_config(
            found,
            model_override=args.model,
            cdp_url_override=args.cdp_url,
        )
        _log_config(config)
    except ConfigError as exc:
        return _config_error(exc)
    from critique_bot.worker import run_worker

    return run_worker(config, headed=bool(args.headed))


def _main_submit(argv: list[str]) -> int:
    from critique_bot.queue import FileQueue, QueueError

    parser = build_parser()
    parser.prog = "crit submit"
    parser.description = (
        "Enqueue a review on the runner worker and wait for review.md. "
        "The worker owns the signed-in Edge; this command does not launch a browser."
    )
    _add_submit_args(parser)
    args = parser.parse_args(argv)
    log.configure(enabled=bool(args.logs))
    output_dir = Path(args.output_dir)
    try:
        mode = _resolve_mode(args)
    except ConfigError as exc:
        return _config_error(exc)
    if mode in (MODE_CHAT, MODE_AGENT):
        return _config_error(
            ConfigError(
                f"submit cannot run --mode {mode}; use review or general, "
                "or run the mode locally"
            )
        )
    if getattr(args, "review_engine", None) == REVIEW_AGENT:
        return _config_error(
            ConfigError("submit runs the one-shot review; the worker does not run --deep reviews yet")
        )
    if args.headed:
        log.warn("--headed is ignored on submit; the worker owns the browser")
    found = _find_config(args.config, args.repo_dir, ask=False)
    if isinstance(found, int):
        return found
    args.config = str(found)
    stem = OUTPUT_STEM[mode]
    log.info(
        "crit submit "
        + log.kv(
            config=args.config,
            mode=mode,
            label=args.label or None,
            patch_file=args.patch_file or ("(stdin)" if mode == MODE_REVIEW else None),
            output_dir=str(output_dir),
            wait_timeout=args.wait_timeout,
        )
    )
    try:
        config = load_config(
            args.config,
            model_override=args.model,
            cdp_url_override=args.cdp_url,
        )
        _log_config(config)
        payload = _build_prompt_payload(args, mode, config.input_limits, config)
    except EmptyDiff:
        print("empty diff; nothing to review", flush=True)
        return 0
    except ConfigError as exc:
        return _config_error(exc)
    if not payload.prompt.strip():
        return _config_error(ConfigError("prompt is empty"))

    queue = FileQueue(Path(config.queue_dir))
    if not queue.worker_alive():
        message = (
            "crit worker is not running "
            f"({queue.worker_hint()}). On the runner start: "
            f"crit worker --config {args.config} --logs"
        )
        log.error(message)
        log.notice(f"Problem: {message}")
        return 1
    try:
        meta = _ci_meta()
        job_id = queue.enqueue(
            mode=mode,
            stem=stem,
            prompt=payload.prompt,
            files=payload.files,
            patch=payload.patch or None,
            model=args.model,
            meta=meta,
            label=args.label,
        )
        print(f"queued job {job_id}; waiting for worker...", file=sys.stderr)
        status = queue.wait(job_id, timeout_sec=max(args.wait_timeout, 1))
    except QueueError as exc:
        log.error(str(exc))
        log.notice(f"Problem: {exc}")
        return 1

    result_dir = queue.result_dir(job_id)
    _copy_job_results(result_dir, output_dir, status.stem)
    body_path = output_dir / f"{status.stem}.md"
    if not status.ok:
        message = status.error or "worker reported failure"
        log.error(f"job {job_id} failed: {message}")
        log.notice(f"Problem: {message}")
        return 1
    if body_path.is_file():
        log.print_safe(log.paint(body_path.read_text(encoding="utf-8"), log.MODEL_COLOR), flush=True)
    log.info(
        f"job {job_id} copied to {output_dir} "
        f"({status.stem}.md)"
        + (f" in {status.elapsed_seconds:.1f}s" if status.elapsed_seconds else "")
    )
    return 0


def _main_setup(argv: list[str]) -> int:
    hooks = model_api.backend_hooks("browser")
    if hooks is None:
        print("crit setup configures the web UI backend, which is not installed in this crit. "
              "Install it with: pip install 'critique-bot[webchat]'", file=sys.stderr)
        return 2
    run_setup, DEFAULT_PORT = hooks.setup_command()

    parser = argparse.ArgumentParser(
        prog="crit setup",
        description=(
            "Open a small web UI on 127.0.0.1 to pick the page's selectors "
            "by clicking them in a real browser window, and run a live test. "
            "Writes the result to your config file."
        ),
    )
    parser.add_argument("--config", help="settings file (default: the one crit config shows)")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port to listen on (default: {DEFAULT_PORT}; 0 picks a free one)",
    )
    parser.add_argument(
        "--open",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="open the page in your default browser (default: on)",
    )
    parser.add_argument(
        "--logs",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="write diagnostic logs to stderr (default: off)",
    )
    args = parser.parse_args(argv)
    log.configure(enabled=bool(args.logs))
    if args.config:
        target = Path(args.config)
    else:
        found = _find_config(None)
        if isinstance(found, int):
            return found
        target = found
    return run_setup(target, port=args.port, open_page=bool(args.open))


def _main_queue_status(argv: list[str]) -> int:
    from critique_bot.queue import FileQueue

    parser = argparse.ArgumentParser(
        prog="crit queue-status",
        description=(
            "Show whether the worker is alive, what is waiting in the queue, "
            "and how recent jobs ended."
        ),
    )
    parser.add_argument("--config", help="settings file (default: the one crit config shows)")
    parser.add_argument(
        "--recent",
        type=int,
        default=10,
        metavar="N",
        help="how many finished jobs to list (default: 10)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument(
        "--logs",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="write diagnostic logs to stderr (default: off)",
    )
    args = parser.parse_args(argv)
    log.configure(enabled=bool(args.logs))
    found = _find_config(args.config, ask=False)
    if isinstance(found, int):
        return found
    args.config = str(found)
    try:
        config = load_config(found)
    except ConfigError as exc:
        return _config_error(exc)

    snapshot = FileQueue(Path(config.queue_dir)).snapshot(recent=max(args.recent, 0))
    if args.json:
        print(json.dumps(snapshot, indent=2, default=str), flush=True)
        return 0 if snapshot["worker_alive"] else 1

    print(f"queue      {snapshot['queue_dir']}", flush=True)
    state = "running" if snapshot["worker_alive"] else "NOT RUNNING"
    print(f"worker     {state} ({snapshot['worker_hint']})", flush=True)
    print(
        f"waiting    {len(snapshot['waiting'])} job(s); "
        f"in progress {len(snapshot['processing'])}",
        flush=True,
    )
    for job in snapshot["waiting"] + snapshot["processing"]:
        age = job.get("age_seconds")
        extra = f", {age:.0f}s old" if isinstance(age, (int, float)) else ""
        attempts = job.get("attempts") or 0
        retry = f", attempt {attempts + 1}" if attempts else ""
        print(f"  - {job['id']} ({job.get('label') or 'no label'}{extra}{retry})", flush=True)
    if snapshot["recent"]:
        print("recent", flush=True)
        for item in snapshot["recent"]:
            if "ok" not in item:
                print(f"  ? {item['id']}  {item.get('state', '')}", flush=True)
                continue
            mark = "ok  " if item["ok"] else "FAIL"
            elapsed = item.get("elapsed_seconds")
            timing = f" in {elapsed:.1f}s" if isinstance(elapsed, (int, float)) else ""
            note = "" if item["ok"] else f"  {item.get('error') or ''}"
            print(f"  {mark} {item['id']}{timing}{note}", flush=True)
    if not snapshot["worker_alive"]:
        print(
            f"\nStart the worker: crit worker --config {args.config} --logs",
            flush=True,
        )
        return 1
    return 0


def _main_gitlab_post(argv: list[str]) -> int:
    from critique_bot.gitlab_post import GitLabPostError, post_review

    parser = argparse.ArgumentParser(
        prog="crit gitlab-post",
        description=(
            "Post the review as GitLab MR discussion threads: a short risk "
            "summary plus inline diff threads. Findings that cannot be pinned "
            "to the diff stay on the summary. Needs "
            "CRIT_GITLAB_TOKEN (project access token, scope api). "
            "CI_JOB_TOKEN cannot create discussions."
        ),
    )
    parser.add_argument(
        "--review-file",
        required=True,
        help="path to review.md from submit",
    )
    parser.add_argument(
        "--patch-file",
        help="unified diff used to map comments onto changed lines "
        "(fetched from the MR if omitted)",
    )
    parser.add_argument(
        "--config",
        help="settings file with gitlab.base_url (default: the one crit config shows)",
    )
    parser.add_argument("--project-id", help="GitLab project ID or path")
    parser.add_argument("--mr-iid", help="merge request IID")
    parser.add_argument(
        "--mr-url",
        help="merge request web URL (fills host, project, and IID if omitted)",
    )
    parser.add_argument(
        "--api-url",
        help="GitLab API v4 URL (or gitlab.base_url/api/v4, or CI_API_V4_URL)",
    )
    parser.add_argument(
        "--logs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="write diagnostic logs to stderr (default: on)",
    )
    args = parser.parse_args(argv)
    log.configure(enabled=bool(args.logs))
    gitlab_cfg = None
    from critique_bot.config_cmd import resolve_config_path

    try:
        location = resolve_config_path(args.config, ".")
    except ConfigError as exc:
        return _config_error(exc)
    if location.source in ("flag", "env") and not location.exists:
        return _config_error(ConfigError(f"config file not found: {location.path}"))
    if location.exists:
        try:
            gitlab_cfg = load_config(location.path).gitlab
        except ConfigError as exc:
            return _config_error(exc)
        configured = (
            args.api_url
            or os.environ.get("CI_API_V4_URL")
            or env_names.get("CRIT_GITLAB_URL")
            or os.environ.get("CI_SERVER_URL")
            or gitlab_cfg.base_url
        )
        if not configured:
            _enable_prompts(args, location.path)
            asked = config_prompts.ensure_gitlab_url("")
            if asked:
                from critique_bot.config import GitLabConfig

                gitlab_cfg = GitLabConfig(base_url=asked)
    try:
        return post_review(
            review_file=Path(args.review_file),
            patch_file=Path(args.patch_file) if args.patch_file else None,
            project_id=args.project_id,
            mr_iid=args.mr_iid,
            api_url=args.api_url,
            mr_url=args.mr_url,
            gitlab=gitlab_cfg,
        )
    except GitLabPostError as exc:
        log.error(str(exc))
        log.notice(f"Could not post the review to GitLab: {exc}")
        return 1


def _main_agent(args: argparse.Namespace) -> int:
    """``crit`` / ``--mode agent``: set up the workspace on first run, then run a task."""
    from critique_bot.agent import run_agent
    from critique_bot.project_home import BotHomeError, find_bot_home, init_bot_home, update_settings
    from critique_bot.roles import ROLE_IDS, normalize_role
    from critique_bot.welcome import pick_theme, stdio_is_tty

    role_flag: str | None = None
    if getattr(args, "role", None):
        role_flag = normalize_role(args.role)
        if role_flag is None:
            return _config_error(ConfigError(f"--role {args.role!r} is not one of: {', '.join(ROLE_IDS)}"))

    workspace = Path(args.repo_dir).resolve()
    words = [part for part in (args.paths or []) if part]
    if words == ["init"]:
        if args.prompt or args.prompt_file:
            return _config_error(ConfigError("init does not take a prompt"))
        try:
            init_bot_home(workspace)
        except BotHomeError as exc:
            return _config_error(ConfigError(str(exc)))
        from critique_bot.config_cmd import NOT_SET_UP, resolve_config_path

        try:
            config_ready = resolve_config_path(args.config, workspace).exists
        except ConfigError as exc:
            return _config_error(exc)
        if not config_ready:
            print(f"Initialized. {NOT_SET_UP}", file=sys.stderr)
        return 0
    if words and words[0] == "init":
        return _config_error(ConfigError("init does not take extra arguments"))

    home = find_bot_home(workspace)
    if words == ["undo"]:
        if home is None:
            print("nothing to undo", file=sys.stderr)
            return 1
        from critique_bot.agent_edit import undo_last

        restored = undo_last(home.cache_dir, home.root)
        if not restored:
            print("nothing to undo", file=sys.stderr)
            return 1
        for path in restored:
            print(f"restored {path}")
        return 0

    just_created = home is None
    if home is None:
        try:
            home = init_bot_home(workspace)
        except BotHomeError as exc:
            return _config_error(ConfigError(str(exc)))
    if args.config and not home.settings.get("config"):
        update_settings(home, config=str(Path(args.config).resolve()))

    reconfigure = words == ["configure"]
    if reconfigure:
        words = []
    has_task = bool(args.prompt or args.prompt_file or words or args.files)
    from critique_bot.config_cmd import NOT_SET_UP, resolve_config_path

    try:
        location = resolve_config_path(args.config, workspace)
    except ConfigError as exc:
        return _config_error(exc)
    config_path = location.path
    missing_config = not location.exists
    if missing_config and not reconfigure and location.source == "user":
        found = _use_found_config_file(config_path)
        if isinstance(found, int):
            return found
        if found is not None:
            config_path, missing_config = found, False
    if (missing_config or reconfigure) and _may_ask(args):
        outcome = _first_run_wizard(config_path, reconfigure=reconfigure)
        if isinstance(outcome, int):
            return outcome
        config_path, missing_config = outcome, False
        if reconfigure and not has_task:
            return 0

    # The role is set once per project, from its files (no question). --role on a project
    # without one stores it; a later --role only applies to that run.
    if not home.settings.get("role"):
        if role_flag is not None:
            update_settings(home, role=role_flag)
        else:
            from critique_bot.roles import detect_role, role_label

            detected = detect_role(home.root)
            update_settings(home, role=detected)
            log.info(f"Role for this project: {role_label(detected)} (change with: crit --role <name>)")

    # The style and the sign-in welcome are once per user, not per project.
    from critique_bot.config import set_user_preferences, user_preferences

    prefs = user_preferences()
    welcome = stdio_is_tty() and not prefs.get("welcomed") and not home.settings.get("welcomed")
    if welcome and not prefs.get("theme") and not home.settings.get("theme"):
        chosen = pick_theme()
        theme_id, syntax = chosen if chosen is not None else ("dark", True)
        set_user_preferences(theme=theme_id, syntax_preview=syntax)

    if reconfigure and missing_config:
        return _config_error(ConfigError("crit configure needs a terminal to ask its questions; use crit config instead"))
    if missing_config:
        if location.source in ("flag", "env"):
            return _config_error(ConfigError(f"config file not found: {config_path}"))
        if not has_task and (just_created or welcome):
            print(f"Initialized. {NOT_SET_UP}", file=sys.stderr)
            return 0
        log.error("no config file found")
        log.notice(NOT_SET_UP)
        return 1
    try:
        config = load_config(
            config_path,
            model_override=args.model,
            cdp_url_override=args.cdp_url,
        )
        _log_config(config)
    except ConfigError as exc:
        return _config_error(exc)
    _enable_prompts(args, config_path, agent_ui=True)

    if welcome:
        # A local model has no sign-in: the first run goes straight to the task.
        if not config.is_local and not _welcome_login(config):
            return 0 if not has_task else 1
        set_user_preferences(welcomed=True)

    task = ""
    if args.prompt_file:
        try:
            task = _read_text_file(
                args.prompt_file,
                config.input_limits,
                label="prompt file",
                allow_binary=False,
            ).text
        except ConfigError as exc:
            return _config_error(exc)
    elif args.prompt:
        task = args.prompt
    elif words:
        task = " ".join(words)
    extra = list(args.files or [])
    if extra:
        listed = "\n".join(f"- {path}" for path in extra)
        task = (task + "\n\nPaths:\n" + listed).strip()
    rounds = args.max_rounds
    if rounds is not None and rounds < 1:
        return _config_error(ConfigError("--max-rounds must be at least 1"))
    output_dir = Path(args.output_dir) if args.output_dir else None
    _use_terminal_ui()
    _apply_tool_flags(args, home)
    code = run_agent(
        config,
        home,
        task,
        max_rounds=rounds,
        output_dir=output_dir,
        headed=bool(args.headed),
        approve_mode=_agent_mode(args),
        role=role_flag,
        **({"resume": args.resume_session} if getattr(args, "resume_session", None) else {}),
    )
    config_prompts.offer_blocked_hosts()
    return code


class _StderrTail:
    """stderr that also keeps its last few KB, so a startup failure can be told to the ACP client."""

    def __init__(self, stream: Any, keep: int = 4000) -> None:
        self._stream = stream
        self._keep = keep
        self.text = ""

    def write(self, data: str) -> int:
        self.text = (self.text + data)[-self._keep :]
        return self._stream.write(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _main_agent_acp(args: argparse.Namespace) -> int:
    """``crit --acp``: the agent behind an ACP client. Waits for the first session/new to pick the folder."""
    from critique_bot import agent, agent_acp

    agent_acp.start()
    tail = _StderrTail(sys.stderr)
    sys.stderr = sys.stdout = tail
    cwd = agent_acp.wait_for_session()
    if cwd is None:
        return 0
    args.repo_dir = str(cwd)
    try:
        os.chdir(cwd)
    except OSError as exc:
        log.notice(f"Problem: cannot open {cwd}: {exc}")
    agent.use_ui(agent_acp)
    code = 1
    try:
        code = _main_agent(args)
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:
        log.notice(f"crit stopped: {exc}. {log.details_hint()}")
        raise
    finally:
        # Told to a client still waiting on session/new (no config.json, sign-in needed, ...).
        errors = [line for line in tail.text.splitlines() if line.lower().startswith("error")]
        lines = [line for line in tail.text.splitlines() if line.strip()]
        agent_acp.finish(code, errors[-1] if errors else (lines[-1] if lines else ""))
    return code


def _agent_mode(args: argparse.Namespace) -> str | None:
    """--permission-mode, --plan, or --yes; None reads "permissions" from .crit/settings.json."""
    if getattr(args, "agent_mode", None):
        return str(args.agent_mode)
    if getattr(args, "plan_mode", False):
        return "plan"
    if getattr(args, "auto_approve", False):
        return "auto"
    return None


def _welcome_login(config: BotConfig) -> bool:
    """Ask to sign in, then open Edge. False means stop before the session."""
    from critique_bot.welcome import ask_login

    hooks = model_api.backend_hooks("browser")
    if hooks is None:
        log.notice("Problem: the web UI backend is not installed. Install it with: pip install 'critique-bot[webchat]'")
        return False

    if not ask_login():
        print(
            "Sign in later by running crit again and opening the login window.",
            file=sys.stderr,
        )
        return False
    if hooks.needs_visible_login(config.user_data_dir, config.storage_state):
        try:
            hooks.login(config)
        except model_api.BackendError as exc:
            log.notice(f"Problem: {exc}")
            return False
        return True
    print("Already signed in.", file=sys.stderr)
    return True


def _apply_tool_flags(args: argparse.Namespace, home) -> None:
    """--tool-format / --tool-set / --no-memory win over .crit/settings.json for this run (not saved)."""
    if not isinstance(home.settings, dict):
        return
    if getattr(args, "tool_format", None):
        home.settings["tool_format"] = args.tool_format
    if getattr(args, "tool_set", None):
        home.settings["tool_set"] = args.tool_set
    if getattr(args, "no_memory", False):
        home.settings["memory"] = "off"


def _use_terminal_ui() -> None:
    """The terminal, unless a front end is installed already (crit --acp installs the IDE's)."""
    from critique_bot import agent, agent_ui, ui_api

    if isinstance(agent.agent_ui, ui_api.NullUI):
        agent.use_ui(agent_ui)


def _review_engine(args: argparse.Namespace) -> str:
    """--quick or --deep when given; otherwise the agent review under crit and the one-shot otherwise."""
    if getattr(args, "review_engine", None):
        return args.review_engine
    return REVIEW_AGENT if getattr(args, "crit_defaults", False) else REVIEW_QUICK


def _main_review_agent(args: argparse.Namespace, output_dir: Path) -> int:
    """``crit review``: the Code Reviewer reads the project with read-only tools, then reports."""
    from critique_bot import review_agent
    from critique_bot.agent import run_read_only_task
    from critique_bot.roles import CODE_REVIEWER
    from critique_bot.project_home import BotHomeError, find_bot_home, init_bot_home

    started = datetime.now(timezone.utc)
    workspace = Path(args.repo_dir).resolve()
    rounds = args.max_rounds or review_agent.DEFAULT_MAX_ROUNDS
    try:
        config = load_config(args.config, model_override=args.model, cdp_url_override=args.cdp_url)
        _log_config(config)
        _enable_prompts(args, args.config, agent_ui=True)
        limits = config.input_limits
        extra_files = list(args.files or []) + list(args.paths or [])
        patch_input, extra_loaded, mr_context = _load_review_inputs(args, extra_files, limits, config)
        template = _load_template(_review_template_path(args), limits)
        patch_body, _stats = _sanitized_patch(patch_input, limits)
    except EmptyDiff:
        print("empty diff; nothing to review", flush=True)
        return 0
    except ConfigError as exc:
        return _config_error(exc)
    changed = changed_file_paths(patch_input.text)
    changed += [item.name for item in extra_loaded if item.name not in changed]
    task = review_agent.build_task(
        template, patch_body, changed_paths=changed, mr_context=mr_context, max_rounds=rounds
    )
    try:
        home = find_bot_home(workspace) or init_bot_home(workspace)
    except BotHomeError as exc:
        return _config_error(ConfigError(str(exc)))
    _use_terminal_ui()
    _apply_tool_flags(args, home)
    code, turns = run_read_only_task(
        config,
        home,
        task,
        answer=review_agent.CONTRACT,
        role=CODE_REVIEWER,
        max_rounds=rounds,
        headed=bool(args.headed),
    )
    config_prompts.offer_blocked_hosts()
    review = review_agent.review_text(turns)
    if not review:
        message = "the agent review ended without a review; the transcript is in .crit/sessions"
        if code == 0:
            code = 1
        log.error(message)
        log.notice(f"Problem: {message}. Try crit review --quick.")
        return code
    if code != 0:
        log.warn("the review task did not end cleanly; writing the last review it sent")
    finished = datetime.now(timezone.utc)
    payload = {
        "mode": MODE_REVIEW,
        "engine": REVIEW_AGENT,
        "model": config.model,
        "backend": config.backend,
        "url": config.url,
        "prompt_chars": len(task),
        "response": review,
        "tool_rounds_max": rounds,
        "turns": len(turns),
        "started_at": isoformat(started),
        "finished_at": isoformat(finished),
    }
    write_output(output_dir, review, payload, stem=OUTPUT_STEM[MODE_REVIEW])
    return 0


def _main_run(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log.configure(enabled=bool(args.logs))
    output_dir = Path(args.output_dir)
    try:
        mode = _resolve_mode(args)
    except ConfigError as exc:
        return _config_error(exc)
    if mode == MODE_AGENT:
        if getattr(args, "acp", False):
            return _main_agent_acp(args)
        return _main_agent(args)
    found = _find_config(args.config, args.repo_dir)
    if isinstance(found, int):
        return found
    args.config = str(found)
    if args.review_engine and mode != MODE_REVIEW:
        return _config_error(ConfigError("--quick and --deep only apply to review mode"))
    if mode == MODE_REVIEW and _review_engine(args) == REVIEW_AGENT:
        return _main_review_agent(args, output_dir)
    stem = OUTPUT_STEM[mode]
    headed = args.headed
    log.info(
        "crit starting "
        + log.kv(
            config=args.config,
            mode=mode,
            prompt=log.preview(args.prompt) if args.prompt else None,
            prompt_file=args.prompt_file,
            patch_file=args.patch_file or ("(stdin)" if mode == MODE_REVIEW else None),
            files=",".join(list(args.files or []) + list(args.paths or [])) or None,
            output_dir=str(output_dir),
            headed=headed,
            cdp_url=args.cdp_url,
            model_override=args.model,
            prompt_template=args.prompt_template,
            logs=args.logs,
        )
    )

    try:
        config = load_config(
            args.config,
            model_override=args.model,
            cdp_url_override=args.cdp_url,
        )
        _log_config(config)
        _enable_prompts(args, args.config)
        payload = _build_prompt_payload(args, mode, config.input_limits, config)
    except EmptyDiff:
        print("empty diff; nothing to review", flush=True)
        return 0
    except ConfigError as exc:
        return _config_error(exc)

    prompt = payload.prompt
    from critique_bot.model_api import COMPLETION_IDLE, BackendError, ChatError, open_provider

    started = datetime.now(timezone.utc)
    turns: list[dict[str, str]] = []
    response = ""
    completion: dict | None = None
    try:
        setup_msg = "Starting..."
        provider = open_provider(config, headed=headed)
        with ExitStack() as stack:
            with log.loading(setup_msg):
                stack.enter_context(provider)
            with provider.session() as session:
                try:
                    if mode == MODE_CHAT:
                        turns = _run_chat_session(session, config, prompt)
                    elif mode == MODE_REVIEW:
                        response = run_review_session(
                            session,
                            prompt,
                            payload.files,
                            config.input_limits,
                            patch=payload.patch,
                            turn_pause_seconds=config.turn_pause_seconds,
                        )
                        completion = getattr(session, "last_detail", None)
                    else:
                        response = session.send(prompt)
                        completion = getattr(session, "last_detail", None)
                except Exception:
                    log.exception("conversation flow failed")
                    save_failure(session, output_dir)
                    raise
        config_prompts.offer_blocked_hosts()
    except (BackendError, ChatError) as exc:
        log.error(str(exc))
        log.notice(f"Problem: {exc}")
        config_prompts.offer_blocked_hosts()
        return 1
    except Exception as exc:
        log.exception(f"unexpected failure: {exc}")
        log.notice(f"crit stopped: {exc}. {log.details_hint()}")
        return 1

    finished = datetime.now(timezone.utc)
    elapsed = (finished - started).total_seconds()
    if mode == MODE_CHAT:
        if not turns:
            log.info(f"chat session ended with no turns ({elapsed:.1f}s)")
            return 0
        response = _format_chat_transcript(turns)
        log.info(
            f"chat session finished in {elapsed:.1f}s "
            f"({len(turns)} turn(s), {len(response)} chars)"
        )
    else:
        log.info(f"chat flow finished in {elapsed:.1f}s ({len(response)} chars)")
        if not response.strip():
            kind = "review" if mode == MODE_REVIEW else "reply"
            log.error(f"assistant returned an empty {kind}")
            log.notice(f"Problem: assistant returned an empty {kind}")
            return 1

    payload = {
        "mode": mode,
        "model": config.model,
        "backend": config.backend,
        "url": config.url,
        "prompt_chars": len(prompt),
        "response": response,
        "started_at": isoformat(started),
        "finished_at": isoformat(finished),
    }
    if turns:
        payload["turns"] = turns
    if completion:
        payload["completion"] = completion
        if completion.get("completion") == COMPLETION_IDLE:
            log.warn(
                "reply ended on an idle timeout, not a generation-finished "
                "signal; it may be truncated"
            )
    write_output(output_dir, response, payload, stem=stem)
    log.info("crit finished successfully")
    return 0
