from __future__ import annotations

import sys
import traceback
from datetime import datetime
from typing import Any


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write(level: str, message: str) -> None:
    print(f"{_ts()} [{level:<5}] {message}", file=sys.stderr, flush=True)


def debug(message: str) -> None:
    _write("DEBUG", message)


def info(message: str) -> None:
    _write("INFO", message)


def warn(message: str) -> None:
    _write("WARN", message)


def error(message: str) -> None:
    _write("ERROR", message)


def exception(message: str) -> None:
    error(message)
    traceback.print_exc(file=sys.stderr)
    sys.stderr.flush()


def preview(text: str, limit: int = 120) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def kv(**fields: Any) -> str:
    parts = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={value!r}")
    return " ".join(parts)
