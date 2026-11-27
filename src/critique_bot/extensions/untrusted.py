# SPDX-License-Identifier: Apache-2.0
"""Marking text that came from outside crit (an MCP server, a tool that reads the network).

The model sees it inside ``<untrusted source="...">...</untrusted>`` so instructions in it
read as data, and the tool result carries ``"taint": "<source>"`` so a later taint hook
(Stage 4) can see that the session has read untrusted input.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

__all__ = ["TAINT_KEY", "mark", "wrap"]

TAINT_KEY = "taint"
_SOURCE_RE = re.compile(r"[^\w:./@+-]")
#: ``<`` and what a reader takes for it: fullwidth, small form, angle brackets.
_LESS_THAN = frozenset("<\uff1c\ufe64\u2039\u2329\u3008\u27e8")
_TAG_RE = re.compile(r"\s*/?\s*untrusted", re.IGNORECASE)
_LOOKAHEAD = 64


def _plain(text: str) -> str:
    """``text`` as a reader sees it: no format characters (zero-width, soft hyphen), NFKC-folded."""
    return unicodedata.normalize("NFKC", "".join(ch for ch in text if unicodedata.category(ch) != "Cf"))


def _neutralize(text: str) -> str:
    """Escape each ``<`` (or lookalike) that starts an ``untrusted`` tag, however it is disguised."""
    if not any(ch in _LESS_THAN for ch in text):
        return text
    out: list[str] = []
    for index, ch in enumerate(text):
        if ch in _LESS_THAN and _TAG_RE.match(_plain(text[index + 1:index + 1 + _LOOKAHEAD])):
            out.append("&lt;")
        else:
            out.append(ch)
    return "".join(out)


def wrap(source: str, text: str) -> str:
    """``text`` inside an untrusted block; tags inside it cannot close or reopen the block.

    Zero-width and other format characters, fullwidth forms and bracket lookalikes are seen
    through first (SF-9), so ``<\u200b/untrusted>`` or ``\uff1c/untrusted\uff1e`` is escaped too.
    """
    safe_source = _SOURCE_RE.sub("_", source)[:120]
    body = _neutralize(str(text))
    return f'<untrusted source="{safe_source}">\n{body}\n</untrusted>'


def mark(result: dict[str, Any], source: str) -> dict[str, Any]:
    """Wrap a tool result's output and flag it for the taint hook."""
    for key in ("output", "error"):
        if isinstance(result.get(key), str) and result[key]:
            result[key] = wrap(source, result[key])
    result[TAINT_KEY] = source
    return result
