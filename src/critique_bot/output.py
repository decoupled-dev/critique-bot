from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from critique_bot import log



def write_output(
    output_dir: Path,
    body: str,
    payload: dict[str, Any],
    *,
    stem: str = "review",
    print_body: bool = True,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / f"{stem}.md"
    json_path = output_dir / f"{stem}.json"
    md_path.write_text(body, encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.info(
        f"wrote {stem} "
        + log.kv(
            markdown=str(md_path),
            json=str(json_path),
            chars=len(body),
        )
    )
    if print_body:
        log.print_safe(log.paint(body, log.MODEL_COLOR), flush=True)


def write_review(
    output_dir: Path,
    body: str,
    payload: dict[str, Any],
) -> None:
    write_output(output_dir, body, payload, stem="review")


def isoformat(moment: datetime) -> str:
    return moment.astimezone().isoformat(timespec="seconds")
