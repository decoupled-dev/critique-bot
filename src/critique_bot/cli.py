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
    compose_prompt,
    compose_prompt_from_args,
    default_prompt_template_path,
    format_attachments,
    load_config,
)
from critique_bot.output import isoformat, save_failure, write_output
from critique_bot.patch import (
    NOTE_RESERVE_CHARS,
    InputError,
    InputLimits,
    LoadedInput,
    cap_text,
    finalize_prompt,
    load_path,
    load_stdin,
    sanitize_attachments,
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


def _build_prompt(args: argparse.Namespace, mode: str, limits: InputLimits) -> str:
    loaded_attachments = _collect_attachments(
        args.patch_file,
        list(args.files or []) + list(args.paths or []),
        limits,
        allow_stdin=mode == MODE_REVIEW,
    )
    raw_attachments = [(item.name, item.text) for item in loaded_attachments]
    read_capped = any(item.truncated_read for item in loaded_attachments)

    if mode in (MODE_GENERAL, MODE_CHAT):
        has_input = bool(
            args.prompt
            or args.prompt_file
            or args.patch_file
            or args.files
            or args.paths
        )
        if mode == MODE_CHAT and not has_input:
            return ""
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
        return prompt

    template_path = (
        Path(args.prompt_template)
        if args.prompt_template
        else default_prompt_template_path()
    )
    template = _load_template(template_path, limits)
    overhead = max(len(template) - 7, 0)
    attachments, stats = sanitize_attachments(
        raw_attachments,
        limits,
        extra_overhead=overhead + NOTE_RESERVE_CHARS,
    )
    stats.truncated_read = stats.truncated_read or read_capped
    body = format_attachments(attachments, named=len(attachments) > 1)
    prompt = finalize_prompt(compose_prompt(template, body), limits, stats)
    log.info(
        f"composed review prompt ({len(prompt)} chars"
        + (", sanitized" if stats.did_sanitize else "")
        + ")"
    )
    return prompt


_CHAT_HELP = (
    "commands:\n"
    "  exit, quit, /q     leave the session\n"
    "  /help              show this help\n"
    "  /file PATH [text]  attach a file to this turn\n"
    "  end a line with \\  continue on the next line"
)


def _print_assistant(reply: str) -> None:
    print(reply, flush=True)
    print(flush=True)


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
            "General-purpose LLM bot. Default backend drives a web chat UI in "
            "headless Microsoft Edge; set backend to ollama, openai, or "
            "openai-compatible to call an HTTP API instead. "
            "Default mode is a specialized code reviewer; "
            "--mode general sends any prompt and optional files; "
            "--mode chat is an interactive terminal session."
        ),
        epilog=(
            "first run on a new machine:\n"
            "  critique-bot setup --config config.json\n"
            "  critique-bot doctor --config config.json --headed\n"
            "\n"
            "production (runner PC):\n"
            "  critique-bot worker --config config.json --logs\n"
            "  critique-bot queue-status --config config.json\n"
            "  critique-bot submit --config config.json --patch-file diff.patch\n"
            "  critique-bot gitlab-post --review-file out/review.md "
            "--patch-file diff.patch\n"
            "  critique-bot github-post --review-file out/review.md "
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
        help="patch/diff to include; in review mode, omit to read a patch from stdin",
    )
    parser.add_argument(
        "--output-dir",
        default="out",
        help="directory for the reply (review.md, reply.md, or chat.md), JSON, and failure screenshots",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window (browser backend: selector debugging / first login)",
    )
    parser.add_argument(
        "--cdp-url",
        help="attach to a running Edge (e.g. http://127.0.0.1:9222) instead of launching a new window",
    )
    parser.add_argument(
        "--model",
        help="override config/env model name (dropdown label, Ollama tag, or API model id)",
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
            "Default: GitLab MR IID, GitHub PR number, CI job id, or 'local'"
        ),
    )
    return parser


def _log_config(config) -> None:
    log.info(
        "config loaded "
        + log.kv(
            backend=config.backend,
            url=config.url or None,
            base_url=config.base_url or None,
            model=config.model or "(none)",
            timeout_ms=config.timeout_ms,
            idle_ms=config.idle_ms if config.uses_browser else None,
            max_prompt_chars=config.max_prompt_chars,
            max_file_chars=config.max_file_chars,
            max_files=config.max_files,
            max_read_bytes=config.max_read_bytes,
            user_data_dir=config.user_data_dir if config.uses_browser else None,
            cdp_url=config.cdp_url if config.uses_browser else None,
            queue_dir=config.queue_dir,
            min_interval_seconds=config.min_interval_seconds,
            interval_jitter_seconds=config.interval_jitter_seconds,
            max_parallel_tabs=config.max_parallel_tabs,
            storage_state=config.storage_state if config.uses_browser else None,
            has_api_key=bool(config.api_key) if not config.uses_browser else None,
            prompt_input=config.selectors.prompt_input if config.uses_browser else None,
            send_button=(
                (config.selectors.send_button or "(Enter)")
                if config.uses_browser
                else None
            ),
            assistant_messages=(
                config.selectors.assistant_messages if config.uses_browser else None
            ),
            model_dropdown=(
                (config.selectors.model_dropdown or "(auto)")
                if config.uses_browser
                else None
            ),
            model_dropdown_identifier=(
                (config.selectors.model_dropdown_identifier or "(none)")
                if config.uses_browser
                else None
            ),
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
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ID",
        "GITHUB_JOB",
        "GITHUB_SHA",
        "GITHUB_REF",
        "GITHUB_HEAD_REF",
        "GITHUB_BASE_REF",
        "GITHUB_EVENT_NAME",
        "GITHUB_PR_NUMBER",
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
            "on-disk queue. Browser backend: one signed-in Edge instance. "
            "Ollama/OpenAI backends: HTTP calls, no browser. GitLab jobs "
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
    "doctor": "check this machine: browser, login, selectors, live round trip",
    "setup": "open a local web UI to configure selectors by clicking them",
    "queue-status": "show worker liveness, queued jobs, and recent results",
    "gitlab-post": "post the review on a GitLab merge request",
    "github-post": "post the review on a GitHub pull request",
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUBCOMMANDS:
        command = argv[0]
        rest = argv[1:]
        handlers = {
            "worker": _main_worker,
            "submit": _main_submit,
            "doctor": _main_doctor,
            "setup": _main_setup,
            "queue-status": _main_queue_status,
            "gitlab-post": _main_gitlab_post,
            "github-post": _main_github_post,
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
        "The worker owns the LLM backend; this command does not launch a browser "
        "or call the model."
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
        prompt = _build_prompt(args, mode, config.input_limits)
    except ConfigError as exc:
        return _config_error(exc)
    if not prompt.strip():
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
            prompt=prompt,
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
        print(body_path.read_text(encoding="utf-8"), flush=True)
    log.info(
        f"job {job_id} copied to {output_dir} "
        f"({status.stem}.md)"
        + (f" in {status.elapsed_seconds:.1f}s" if status.elapsed_seconds else "")
    )
    return 0


def _main_doctor(argv: list[str]) -> int:
    from critique_bot import diagnostics

    parser = argparse.ArgumentParser(
        prog="critique-bot doctor",
        description=(
            "Check that this machine can actually run reviews: browser present, "
            "config valid, chat UI reachable and signed in, selectors matching, "
            "and a real prompt answered. Exits non-zero if any check fails."
        ),
    )
    parser.add_argument("--config", required=True, help="path to JSON config")
    parser.add_argument(
        "--live",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="open the backend and test it for real (default: on)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window (needed for the first login)",
    )
    parser.add_argument(
        "--round-trip",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="send a real prompt during live checks (default: on)",
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
        if args.json:
            print(
                json.dumps({"ok": False, "checks": [], "error": str(exc)}, indent=2),
                flush=True,
            )
        else:
            print(f"[FAIL] config  {exc}", file=sys.stderr)
        return 1

    report = diagnostics.static_checks(config, config_path=Path(args.config))
    if args.live:
        live = diagnostics.run_live_checks(
            config, headed=bool(args.headed), round_trip=bool(args.round_trip)
        )
        report.checks.extend(live.checks)

    if args.json:
        print(diagnostics.render_json(report), end="", flush=True)
    else:
        print(diagnostics.render_text(report), flush=True)
        warnings = len(report.warnings)
        if report.ok:
            summary = "All checks passed."
            if warnings:
                summary += f" {warnings} warning(s) worth a look."
            print(f"\n{summary}", flush=True)
        else:
            names = ", ".join(check.name for check in report.failures)
            print(f"\n{len(report.failures)} check(s) failed: {names}", flush=True)
            print(
                "Run `critique-bot setup --config "
                f"{args.config}` to fix selectors by clicking them.",
                flush=True,
            )
    return 0 if report.ok else 1


def _main_setup(argv: list[str]) -> int:
    from critique_bot.setup_ui import DEFAULT_PORT, run_setup

    parser = argparse.ArgumentParser(
        prog="critique-bot setup",
        description=(
            "Open a small web UI on 127.0.0.1 to check the install, pick the "
            "chat UI selectors by clicking them in a real browser window, and "
            "run a live test. Writes the result to your config file."
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


def _main_github_post(argv: list[str]) -> int:
    from critique_bot.github_post import GitHubPostError, post_review

    parser = argparse.ArgumentParser(
        prog="critique-bot github-post",
        description=(
            "Post the review as a GitHub pull request comment plus inline diff "
            "comments. Needs GITHUB_TOKEN with pull-requests: write (or "
            "CRITIQUE_GITHUB_TOKEN)."
        ),
    )
    parser.add_argument(
        "--review-file", required=True, help="path to review.md from submit"
    )
    parser.add_argument(
        "--patch-file",
        help="unified diff used to map comments onto changed lines",
    )
    parser.add_argument("--repo", help="owner/name (or GITHUB_REPOSITORY)")
    parser.add_argument("--pr", dest="pr_number", help="pull request number")
    parser.add_argument(
        "--api-url", help="GitHub API base URL (default: https://api.github.com)"
    )
    parser.add_argument(
        "--logs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="write diagnostic logs to stderr (default: on)",
    )
    args = parser.parse_args(argv)
    log.configure(enabled=bool(args.logs))
    try:
        return post_review(
            review_file=Path(args.review_file),
            patch_file=Path(args.patch_file) if args.patch_file else None,
            repo=args.repo,
            pr_number=args.pr_number,
            api_url=args.api_url,
        )
    except GitHubPostError as exc:
        log.error(str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _main_gitlab_post(argv: list[str]) -> int:
    from critique_bot.gitlab_post import GitLabPostError, post_review

    parser = argparse.ArgumentParser(
        prog="critique-bot gitlab-post",
        description=(
            "Post the review as a GitLab MR summary note and inline diff "
            "comments. Needs CRITIQUE_GITLAB_TOKEN (project access token, "
            "scope api). CI_JOB_TOKEN cannot create notes."
        ),
    )
    parser.add_argument(
        "--review-file",
        required=True,
        help="path to review.md from submit",
    )
    parser.add_argument(
        "--patch-file",
        help="unified diff used to map comments onto changed lines",
    )
    parser.add_argument("--project-id", help="GitLab project ID or path")
    parser.add_argument("--mr-iid", help="merge request IID")
    parser.add_argument(
        "--api-url",
        help="GitLab API v4 URL (or CI_API_V4_URL; required outside GitLab CI)",
    )
    parser.add_argument(
        "--logs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="write diagnostic logs to stderr (default: on)",
    )
    args = parser.parse_args(argv)
    log.configure(enabled=bool(args.logs))
    try:
        return post_review(
            review_file=Path(args.review_file),
            patch_file=Path(args.patch_file) if args.patch_file else None,
            project_id=args.project_id,
            mr_iid=args.mr_iid,
            api_url=args.api_url,
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
        prompt = _build_prompt(args, mode, config.input_limits)
    except ConfigError as exc:
        return _config_error(exc)

    from critique_bot.browser import BrowserError
    from critique_bot.llm import COMPLETION_IDLE, LLMError, open_provider

    started = datetime.now(timezone.utc)
    turns: list[dict[str, str]] = []
    response = ""
    completion: dict | None = None
    try:
        setup_msg = (
            "Starting browser..." if config.uses_browser else "Connecting to LLM..."
        )
        provider = open_provider(config, headed=headed)
        with ExitStack() as stack:
            with log.loading(setup_msg):
                stack.enter_context(provider)
            with provider.session() as session:
                try:
                    if mode == MODE_CHAT:
                        turns = _run_chat_session(session, config, prompt)
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
    except (BrowserError, LLMError) as exc:
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
        "url": config.url or config.base_url,
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
