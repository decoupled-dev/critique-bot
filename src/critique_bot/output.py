from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.sync_api import Page


def write_review(
    output_dir: Path,
    body: str,
    payload: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "review.md").write_text(body, encoding="utf-8")
    (output_dir / "review.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(body, flush=True)


def save_failure(page: Page, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(output_dir / "screenshot.png"), full_page=True)
    except Exception:
        pass
    try:
        (output_dir / "page.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def isoformat(moment: datetime) -> str:
    return moment.astimezone().isoformat(timespec="seconds")
