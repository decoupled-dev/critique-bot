from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from critique_bot import log



def private_dir(path: Path) -> Path:
    """Create ``path`` (and its parents) readable by its owner only on POSIX; return it.

    For folders that hold code, prompts, or a signed-in browser: the browser profile,
    the CI queue, transcripts. An existing folder is narrowed too. On Windows the folder
    keeps the permissions of its parent (the user's profile folders are private already).
    """
    import os

    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            log.warn(f"could not make {path} private: {exc}")
    return path


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
