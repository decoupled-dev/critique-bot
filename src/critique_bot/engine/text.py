# SPDX-License-Identifier: Apache-2.0
"""Small text and settings helpers shared by the engine modules."""

from __future__ import annotations

from typing import Any


def _one_line(text: str, limit: int = 120) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _cap(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    note = "\n...[truncated; narrow with path, offset, or a tighter pattern]"
    return text[: max(0, limit - len(note))] + note


def _int_setting(settings: dict[str, Any], key: str, default: int, minimum: int) -> int:
    raw = settings.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < minimum:
        return default
    return int(raw)
