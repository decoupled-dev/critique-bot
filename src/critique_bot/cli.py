from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

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
        if not path.is_file():
            raise ConfigError(f"patch file not found: {path}")
        text = path.read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            raise ConfigError("provide --patch-file or pipe a patch on stdin")
        text = sys.stdin.read()
    if not text.strip():
        raise ConfigError("patch is empty")
    return text


def _load_template(path: Path) -> str:
    if not path.is_file():
        raise ConfigError(f"prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


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
        help="show the browser window (for selector debugging)",
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

    try:
        config = load_config(args.config, model_override=args.model)
        patch = _read_patch(args.patch_file)
        template_path = (
            Path(args.prompt_template)
            if args.prompt_template
            else default_prompt_template_path()
        )
        prompt = compose_prompt(_load_template(template_path), patch)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    from critique_bot.browser import BrowserError, launch_edge
    from critique_bot.chat_client import ChatError, submit_review

    started = datetime.now(timezone.utc)
    try:
        with launch_edge(
            headed=args.headed,
            storage_state=config.storage_state,
        ) as page:
            try:
                response = submit_review(page, config, prompt)
            except Exception:
                save_failure(page, output_dir)
                raise
    except (BrowserError, ChatError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: unexpected failure: {exc}", file=sys.stderr)
        return 1

    finished = datetime.now(timezone.utc)
    if not response.strip():
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
    return 0
