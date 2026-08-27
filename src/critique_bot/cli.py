from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from critique_bot import log
from critique_bot.config import (
    ConfigError,
    compose_prompt,
    default_prompt_template_path,
    load_config,
)
from critique_bot.output import isoformat, save_failure, write_review


def _read_patch(patch_file: str | None) -> str:
    if patch_file:
        path = Path(patch_file)
        log.info(f"reading patch from {path}")
        if not path.is_file():
            raise ConfigError(f"patch file not found: {path}")
        text = path.read_text(encoding="utf-8")
    else:
        log.info("reading patch from stdin")
        if sys.stdin.isatty():
            raise ConfigError("provide --patch-file or pipe a patch on stdin")
        text = sys.stdin.read()
    if not text.strip():
        raise ConfigError("patch is empty")
    log.info(f"patch loaded ({len(text)} chars, {text.count(chr(10)) + 1} lines)")
    return text


def _load_template(path: Path) -> str:
    log.info(f"loading prompt template {path}")
    if not path.is_file():
        raise ConfigError(f"prompt template not found: {path}")
    text = path.read_text(encoding="utf-8")
    log.debug(f"template loaded ({len(text)} chars)")
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="critique-bot",
        description=(
            "Open a web LLM chat in headless Microsoft Edge, paste a patch, "
            "and write the review to stdout plus review.md / review.json."
        ),
    )
    parser.add_argument(
        "--config",
        required=True,
        help="path to JSON config (see config.example.json)",
    )
    parser.add_argument(
        "--patch-file",
        help="patch/diff to review; omit to read from stdin",
    )
    parser.add_argument(
        "--output-dir",
        default="out",
        help="directory for review.md, review.json, and failure screenshots",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window (for selector debugging / first login)",
    )
    parser.add_argument(
        "--cdp-url",
        help="attach to a running Edge (e.g. http://127.0.0.1:9222) instead of launching a new window",
    )
    parser.add_argument(
        "--model",
        help="override config/env model name for the dropdown",
    )
    parser.add_argument(
        "--prompt-template",
        help="template file containing a {patch} placeholder",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output_dir = Path(args.output_dir)
    log.info(
        "critique-bot starting "
        + log.kv(
            config=args.config,
            patch_file=args.patch_file or "(stdin)",
            output_dir=str(output_dir),
            headed=args.headed,
            cdp_url=args.cdp_url,
            model_override=args.model,
            prompt_template=args.prompt_template,
        )
    )

    try:
        config = load_config(
            args.config,
            model_override=args.model,
            cdp_url_override=args.cdp_url,
        )
        log.info(
            "config loaded "
            + log.kv(
                url=config.url,
                model=config.model or "(none)",
                timeout_ms=config.timeout_ms,
                idle_ms=config.idle_ms,
                user_data_dir=config.user_data_dir,
                cdp_url=config.cdp_url,
                storage_state=config.storage_state,
                prompt_input=config.selectors.prompt_input,
                send_button=config.selectors.send_button or "(Enter)",
                assistant_messages=config.selectors.assistant_messages,
                model_dropdown=config.selectors.model_dropdown or "(auto)",
                model_dropdown_identifier=config.selectors.model_dropdown_identifier
                or "(none)",
            )
        )
        patch = _read_patch(args.patch_file)
        template_path = (
            Path(args.prompt_template)
            if args.prompt_template
            else default_prompt_template_path()
        )
        prompt = compose_prompt(_load_template(template_path), patch)
        log.info(f"composed prompt ({len(prompt)} chars)")
    except ConfigError as exc:
        log.error(f"config error: {exc}")
        print(f"error: {exc}", file=sys.stderr)
        return 1

    from critique_bot.browser import BrowserError, launch_edge
    from critique_bot.chat_client import ChatError, submit_review

    started = datetime.now(timezone.utc)
    try:
        with launch_edge(
            headed=args.headed,
            storage_state=config.storage_state,
            user_data_dir=config.user_data_dir,
            cdp_url=config.cdp_url,
            start_url=config.url,
            timeout_ms=config.timeout_ms,
        ) as page:
            try:
                response = submit_review(page, config, prompt)
            except Exception:
                log.exception("chat flow failed; capturing screenshot and HTML")
                save_failure(page, output_dir)
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
    log.info(f"chat flow finished in {elapsed:.1f}s ({len(response)} chars)")
    if not response.strip():
        log.error("assistant returned an empty review")
        print("error: assistant returned an empty review", file=sys.stderr)
        return 1

    write_review(
        output_dir,
        response,
        {
            "model": config.model,
            "url": config.url,
            "prompt_chars": len(prompt),
            "response": response,
            "started_at": isoformat(started),
            "finished_at": isoformat(finished),
        },
    )
    log.info("critique-bot finished successfully")
    return 0
