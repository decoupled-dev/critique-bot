# SPDX-License-Identifier: Apache-2.0
"""Marking text that came from outside crit (an MCP server, a tool that reads the network).

The model sees it inside ``<untrusted source="...">...</untrusted>`` so instructions in it
read as data, and the tool result carries ``"taint": "<source>"`` so a later taint hook
(Stage 4) can see that the session has read untrusted input.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["TAINT_KEY", "mark", "wrap"]

TAINT_KEY = "taint"
_CLOSE_RE = re.compile(r"<\s*/\s*untrusted", re.IGNORECASE)
_OPEN_RE = re.compile(r"<\s*untrusted\b", re.IGNORECASE)
_SOURCE_RE = re.compile(r"[^\w:./@+-]")


def wrap(source: str, text: str) -> str:
    """``text`` inside an untrusted block; tags inside it cannot close or reopen the block."""
    safe_source = _SOURCE_RE.sub("_", source)[:120]
    body = _CLOSE_RE.sub("&lt;/untrusted", str(text))
    body = _OPEN_RE.sub("&lt;untrusted", body)
    return f'<untrusted source="{safe_source}">\n{body}\n</untrusted>'


def mark(result: dict[str, Any], source: str) -> dict[str, Any]:
    """Wrap a tool result's output and flag it for the taint hook."""
    for key in ("output", "error"):
        if isinstance(result.get(key), str) and result[key]:
            result[key] = wrap(source, result[key])
    result[TAINT_KEY] = source
    return result
