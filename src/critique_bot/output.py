from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from critique_bot import log

if TYPE_CHECKING:
    from playwright.sync_api import Page


def write_review(
    output_dir: Path,
    body: str,
    payload: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "review.md"
    json_path = output_dir / "review.json"
    md_path.write_text(body, encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.info(
        "wrote review "
        + log.kv(
            markdown=str(md_path),
            json=str(json_path),
            chars=len(body),
        )
    )
    print(body, flush=True)


def save_failure(page: Page, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot = output_dir / "screenshot.png"
    html_path = output_dir / "page.html"
    try:
        from critique_bot.browser import describe_page

        log.error(f"saving failure artifacts for {describe_page(page)}")
    except Exception:
        log.error("saving failure artifacts")
    try:
        page.screenshot(path=str(screenshot), full_page=True)
        log.info(f"saved screenshot {screenshot}")
    except Exception as exc:
        log.warn(f"could not save screenshot: {exc}")
    try:
        html_path.write_text(page.content(), encoding="utf-8")
        log.info(f"saved page HTML {html_path} ({html_path.stat().st_size} bytes)")
    except Exception as exc:
        log.warn(f"could not save page HTML: {exc}")


def isoformat(moment: datetime) -> str:
    return moment.astimezone().isoformat(timespec="seconds")
