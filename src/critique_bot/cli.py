from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from critique_bot import log
from critique_bot.config import (
    ConfigError,
    compose_prompt_from_args,
    default_prompt_template_path,
    load_config,
)
from critique_bot.output import isoformat, save_failure, write_output
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
OUTPUT_STEM = {
    MODE_REVIEW: "review",
    MODE_GENERAL: "reply",
    MODE_CHAT: "chat",
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

    context_inputs = list(extra_loaded)
    seen = {item.name for item in context_inputs}
    for item in load_changed_files(repo_dir, patch_input.text, limits):
        if item.name in seen:
            continue
        context_inputs.append(item)
        seen.add(item.name)

    template_path = (
        Path(args.prompt_template)
        if args.prompt_template
        else default_prompt_template_path()
    )
    template = _load_template(template_path, limits)
    overhead = max(len(template) - 7, 0) + len(mr_context)
    patch_budget = max(limits.max_prompt_chars - overhead - NOTE_RESERVE_CHARS, 2_000)

    patch_body, patch_stats = sanitize_one(
        patch_input.name,
        patch_input.text,
        limits,
        remaining_chars=patch_budget,
        remaining_files=limits.max_files,
    )
    patch_stats.truncated_read = patch_input.truncated_read
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
        + (", staged" if payload.files else "")
        + (", sanitized" if patch_stats.did_sanitize else "")
        + (", gitlab mr context" if mr_context else "")
        + ")"
    )
    return payload


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


def _load_gitlab_mr_context(config, *, need_patch: bool):
    """Fetch MR title, tickets, description, commits, and diffs when targeting is set."""
    from critique_bot.gitlab import GitLabError, fetch_mr_context, resolve_target

    gitlab_cfg = getattr(config, "gitlab", None) if config is not None else None
    target = resolve_target(gitlab_cfg)
    if not target.api_url or not target.project_id or not target.mr_iid:
        return None
    if not target.token:
        log.warn("skipping GitLab MR context; no CRITIQUE_GITLAB_TOKEN")
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
    log.print_safe(reply, flush=True)
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
    parts = ["# Chat", ""]
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
        if not reply.strip():
            log.warn("assistant returned an empty reply")
        turns.append({"role": "user", "content": payload})
        turns.append({"role": "assistant", "content": reply})

    if first_prompt.strip():
        send(first_prompt)

    print(
        "Chat session ready. Type a message, /help, or exit / Ctrl-D to quit.",
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
                print(f"error: {exc}", file=sys.stderr)
                continue
        else:
            payload = cap_text(message, config.max_prompt_chars, what="chat message")
        if not payload.strip():
            continue
        send(payload)
    return turns


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="critique-bot",
        description=(
            "Browser chat automation bot. Drives a web chat UI in headless "
            "Microsoft Edge. Default mode is a specialized code reviewer; "
            "--mode general sends any prompt and optional files; "
            "--mode chat is an interactive terminal session."
        ),
        epilog=(
            "first run on a new machine:\n"
            "  critique-bot setup --config config.json\n"
            "\n"
            "production (runner PC):\n"
            "  critique-bot worker --config config.json --logs\n"
            "  critique-bot queue-status --config config.json\n"
            "  critique-bot submit --config config.json --patch-file diff.patch\n"
            "  critique-bot gitlab-post --review-file out/review.md "
            "--patch-file diff.patch\n"
            "\n"
            "one-shot (debug):\n"
            "  critique-bot --config config.json --patch-file diff.patch\n"
            "  critique-bot --config config.json --mode general "
            "--prompt 'Summarize this' notes.txt\n"
            "  critique-bot --config config.json --mode chat\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        required=True,
        help="path to JSON config (see config.example.json)",
    )
    parser.add_argument(
        "--mode",
        choices=(MODE_REVIEW, MODE_GENERAL, MODE_CHAT),
        help=(
            f"{MODE_REVIEW} (default): code-review template + patch. "
            f"{MODE_GENERAL}: send --prompt and optional files as-is. "
            f"{MODE_CHAT}: interactive conversation in this terminal "
            f"(--prompt is optional as the first message)"
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
        help="files to include in the prompt (same as --file)",
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
        help="show the browser window (selector debugging / first login)",
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
    dest.mkdir(parents=True, exist_ok=True)
    names = (
        f"{stem}.md",
        f"{stem}.json",
        "status.json",
        "job.json",
        "screenshot.png",
        "page.html",
    )
    for name in names:
        item = src / name
        if item.is_file():
            shutil.copy2(item, dest / name)


def _config_error(exc: BaseException) -> int:
    log.error(f"config error: {exc}")
    print(f"error: {exc}", file=sys.stderr)
    return 1


def build_worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="critique-bot worker",
        description=(
            "Keep one worker process open and process review jobs from the "
            "on-disk queue. One signed-in Edge instance. GitLab jobs "
            "should call `critique-bot submit`, not this command."
        ),
    )
    parser.add_argument("--config", required=True, help="path to JSON config")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window (first login / debugging)",
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
    "worker": "run the long-lived queue worker on the runner PC",
    "submit": "enqueue a review and wait for the worker to finish it",
    "setup": "open a local web UI to configure selectors by clicking them",
    "queue-status": "show worker liveness, queued jobs, and recent results",
    "gitlab-post": "post the review on a GitLab merge request",
}


def main(argv: list[str] | None = None) -> int:
    log.configure_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUBCOMMANDS:
        command = argv[0]
        rest = argv[1:]
        handlers = {
            "worker": _main_worker,
            "submit": _main_submit,
            "setup": _main_setup,
            "queue-status": _main_queue_status,
            "gitlab-post": _main_gitlab_post,
        }
        return handlers[command](rest)
    return _main_run(argv)


def _main_worker(argv: list[str]) -> int:
    parser = build_worker_parser()
    args = parser.parse_args(argv)
    log.configure(enabled=bool(args.logs))
    try:
        config = load_config(
            args.config,
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
    parser.prog = "critique-bot submit"
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
    if mode == MODE_CHAT:
        return _config_error(
            ConfigError(
                "submit cannot run --mode chat; use review or general, "
                "or run `critique-bot --mode chat` locally"
            )
        )
    if args.headed:
        log.warn("--headed is ignored on submit; the worker owns the browser")
    stem = OUTPUT_STEM[mode]
    log.info(
        "critique-bot submit "
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
            "critique-bot worker is not running "
            f"({queue.worker_hint()}). On the runner start: "
            f"critique-bot worker --config {args.config} --logs"
        )
        log.error(message)
        print(f"error: {message}", file=sys.stderr)
        return 1
    try:
        meta = _ci_meta()
        job_id = queue.enqueue(
            mode=mode,
            stem=stem,
            prompt=payload.prompt,
            files=payload.files,
            model=args.model,
            meta=meta,
            label=args.label,
        )
        print(f"queued job {job_id}; waiting for worker...", file=sys.stderr)
        status = queue.wait(job_id, timeout_sec=max(args.wait_timeout, 1))
    except QueueError as exc:
        log.error(str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1

    result_dir = queue.result_dir(job_id)
    _copy_job_results(result_dir, output_dir, status.stem)
    body_path = output_dir / f"{status.stem}.md"
    if not status.ok:
        message = status.error or "worker reported failure"
        log.error(f"job {job_id} failed: {message}")
        print(f"error: {message}", file=sys.stderr)
        return 1
    if body_path.is_file():
        log.print_safe(body_path.read_text(encoding="utf-8"), flush=True)
    log.info(
        f"job {job_id} copied to {output_dir} "
        f"({status.stem}.md)"
        + (f" in {status.elapsed_seconds:.1f}s" if status.elapsed_seconds else "")
    )
    return 0


def _main_setup(argv: list[str]) -> int:
    from critique_bot.setup_ui import DEFAULT_PORT, run_setup

    parser = argparse.ArgumentParser(
        prog="critique-bot setup",
        description=(
            "Open a small web UI on 127.0.0.1 to pick the chat UI selectors "
            "by clicking them in a real browser window, and run a live test. "
            "Writes the result to your config file."
        ),
    )
    parser.add_argument("--config", required=True, help="path to JSON config")
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
    return run_setup(Path(args.config), port=args.port, open_page=bool(args.open))


def _main_queue_status(argv: list[str]) -> int:
    from critique_bot.queue import FileQueue

    parser = argparse.ArgumentParser(
        prog="critique-bot queue-status",
        description=(
            "Show whether the worker is alive, what is waiting in the queue, "
            "and how recent jobs ended."
        ),
    )
    parser.add_argument("--config", required=True, help="path to JSON config")
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
    try:
        config = load_config(args.config)
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
            f"\nStart the worker: critique-bot worker --config {args.config} --logs",
            flush=True,
        )
        return 1
    return 0


def _main_gitlab_post(argv: list[str]) -> int:
    from critique_bot.gitlab_post import GitLabPostError, post_review

    parser = argparse.ArgumentParser(
        prog="critique-bot gitlab-post",
        description=(
            "Post the review as GitLab MR discussion threads: a summary "
            "thread plus inline diff threads (replyable). Needs "
            "CRITIQUE_GITLAB_TOKEN (project access token, scope api). "
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
        help="config.json with gitlab.base_url (or CRITIQUE_CONFIG)",
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
    config_path = args.config or os.environ.get("CRITIQUE_CONFIG") or ""
    if config_path:
        try:
            gitlab_cfg = load_config(config_path).gitlab
        except ConfigError as exc:
            return _config_error(exc)
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
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _main_run(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log.configure(enabled=bool(args.logs))
    output_dir = Path(args.output_dir)
    try:
        mode = _resolve_mode(args)
    except ConfigError as exc:
        return _config_error(exc)
    stem = OUTPUT_STEM[mode]
    headed = args.headed
    log.info(
        "critique-bot starting "
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
        payload = _build_prompt_payload(args, mode, config.input_limits, config)
    except EmptyDiff:
        print("empty diff; nothing to review", flush=True)
        return 0
    except ConfigError as exc:
        return _config_error(exc)

    prompt = payload.prompt
    from critique_bot.browser import BrowserError
    from critique_bot.chat_client import COMPLETION_IDLE, ChatError
    from critique_bot.provider import open_provider

    started = datetime.now(timezone.utc)
    turns: list[dict[str, str]] = []
    response = ""
    completion: dict | None = None
    try:
        setup_msg = "Starting browser..."
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
                            turn_pause_seconds=config.turn_pause_seconds,
                        )
                        completion = getattr(session, "last_detail", None)
                    else:
                        response = session.send(prompt)
                        completion = getattr(session, "last_detail", None)
                except Exception:
                    page = getattr(session, "page", None)
                    if page is not None:
                        log.exception(
                            "chat flow failed; capturing screenshot and HTML"
                        )
                        save_failure(page, output_dir)
                    else:
                        log.exception("chat flow failed")
                    raise
    except (BrowserError, ChatError) as exc:
        log.error(str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        log.exception(f"unexpected failure: {exc}")
        print(f"error: unexpected failure: {exc}", file=sys.stderr)
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
            print(f"error: assistant returned an empty {kind}", file=sys.stderr)
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
    log.info("critique-bot finished successfully")
    return 0
