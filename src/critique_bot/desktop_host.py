"""``critique-bot desktop-host``: one per project in the crit desktop app.

It resolves the project's config.json and role (the same way ``crit`` does),
and for a web chat model starts one shared Edge window with remote debugging.
Every agent the app starts then runs with ``--cdp-url`` and
``CRITIQUE_CDP_OWN_TAB=1``, so each one gets its own tab in that window instead
of each launching (and closing) Edge on the same profile.

Output is JSON lines on stdout:

    {"type": "host", "workspace": ..., "config": ..., "role": ..., "local": bool, "cdp_url": str|null}
    {"type": "error", "message": ...}

The host stays up until stdin closes (the app quit or closed the project),
then closes the browser.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from critique_bot import log


def _emit(**fields: Any) -> None:
    sys.__stdout__.write(json.dumps(fields) + "\n")
    sys.__stdout__.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="critique-bot desktop-host",
        description="Shared browser and project settings for the crit desktop app (JSON lines on stdout).",
    )
    parser.add_argument("--repo-dir", default=".", help="project folder")
    parser.add_argument("--config", help="config.json (default: the project's .bot/settings.json entry)")
    parser.add_argument(
        "--headed",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show the shared Edge window (default: on)",
    )
    parser.add_argument("--logs", action=argparse.BooleanOptionalAction, default=False)
    return parser


def main(argv: list[str]) -> int:
    from critique_bot.bot_home import BotHomeError, find_bot_home, init_bot_home
    from critique_bot.config import ConfigError, load_config

    args = build_parser().parse_args(argv)
    log.configure(enabled=bool(args.logs))
    workspace = Path(args.repo_dir).resolve()
    # Library output (index stats, warnings) must not mix with the JSON lines.
    sys.stdout = sys.stderr
    try:
        home = find_bot_home(workspace) or init_bot_home(workspace)
    except BotHomeError as exc:
        _emit(type="error", message=str(exc))
        return 1
    config_path = Path(args.config).resolve() if args.config else home.config_file()
    if config_path is None or not config_path.is_file():
        _emit(
            type="error",
            message=f"no config.json for {home.root}. Place config.json in the project, or pick one in the app.",
        )
        return 1
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        _emit(type="error", message=str(exc))
        return 1
    role = home.settings.get("role") if isinstance(home.settings.get("role"), str) else None
    base = {"workspace": str(home.root), "config": str(config_path), "role": role, "local": config.is_local}
    if config.is_local or config.cdp_url:
        # A local model needs no browser; a configured cdp_url is the user's own shared browser.
        _emit(type="host", cdp_url=config.cdp_url, **base)
        return _wait_for_stdin_close()

    from critique_bot.browser import BrowserError, launch_edge, set_allowed_hosts

    set_allowed_hosts(config.allowed_hosts)
    cdp_out: dict[str, str] = {}
    try:
        with ExitStack() as stack:
            page = stack.enter_context(
                launch_edge(
                    headed=bool(args.headed),
                    storage_state=config.storage_state,
                    user_data_dir=config.user_data_dir,
                    start_url=config.url,
                    timeout_ms=config.timeout_ms,
                    cdp_out=cdp_out,
                )
            )
            if not cdp_out.get("url"):
                _emit(type="error", message="Edge started without remote debugging; agents cannot share it")
                return 1
            _emit(type="host", cdp_url=cdp_out["url"], **base)
            # The network filter runs in this process's Playwright, so keep its event loop turning
            # while waiting: a blocked loop would stall every agent tab's requests.
            closed = threading.Event()
            threading.Thread(target=lambda: (_wait_for_stdin_close(), closed.set()), daemon=True).start()
            while not closed.is_set():
                try:
                    page.wait_for_timeout(250)
                except Exception:
                    # The home tab was closed: keep the loop turning on the context.
                    try:
                        page = page.context.pages[0] if page.context.pages else page.context.new_page()
                    except Exception:
                        _emit(type="error", message="the shared browser closed")
                        return 1
            return 0
    except BrowserError as exc:
        _emit(type="error", message=str(exc))
        return 1


def _wait_for_stdin_close() -> int:
    try:
        while sys.stdin.readline():
            pass
    except (KeyboardInterrupt, OSError):
        pass
    return 0
