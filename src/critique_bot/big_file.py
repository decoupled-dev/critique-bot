# SPDX-License-Identifier: Apache-2.0
"""Reading windows of very large text files (logs of millions of lines) without loading them.

A sparse line index (the byte offset of every ``STEP``-th line) is built once per file
version with one pass over the file, and kept in memory. A window of lines then costs a
seek and a read of at most ``STEP`` extra lines, whatever the file size.

Long lines are clipped for display (:func:`clip_line`): a 100k-character line would
otherwise use a whole tool result by itself. The rest stays readable by column.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

#: Files above this size are read by window instead of whole.
LARGE_FILE_BYTES = 8 * 1024 * 1024
#: One checkpoint every STEP lines.
STEP = 4096
#: Longest line shown in full; longer lines show their start and how much is left.
MAX_LINE_CHARS = 2000
_CHUNK = 16 * 1024 * 1024
_CACHE_FILES = 16


@dataclass
class LineIndex:
    signature: tuple[int, int]  # (mtime_ns, size)
    offsets: list[int]  # byte offset of line 1, STEP+1, 2*STEP+1, ...
    total: int  # number of lines
    crlf: bool


_CACHE: dict[str, LineIndex] = {}
_LOCK = threading.Lock()


def _signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def line_index(path: Path) -> LineIndex:
    """The (cached) sparse line index of ``path``."""
    path = Path(path)
    key = str(path.resolve())
    signature = _signature(path)
    with _LOCK:
        found = _CACHE.get(key)
        if found is not None and found.signature == signature:
            return found
    offsets = [0]
    lines = 0
    position = 0
    last = b""
    crlf = False
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            if not crlf and b"\r\n" in chunk[:65536]:
                crlf = True
            start = 0
            while True:
                newline = chunk.find(b"\n", start)
                if newline < 0:
                    break
                lines += 1
                if lines % STEP == 0:
                    offsets.append(position + newline + 1)
                start = newline + 1
            position += len(chunk)
            last = chunk[-1:]
    total = lines + (1 if position and last != b"\n" else 0)
    if offsets and offsets[-1] >= position and len(offsets) > 1:
        offsets.pop()  # a checkpoint at the very end starts no line
    index = LineIndex(signature, offsets, total, crlf)
    with _LOCK:
        if len(_CACHE) >= _CACHE_FILES:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = index
    return index


def read_lines(path: Path, start: int, count: int) -> list[str]:
    """Lines ``start`` .. ``start + count - 1`` (1-based), decoded as UTF-8 with replacement."""
    if count <= 0 or start < 1:
        return []
    index = line_index(path)
    if start > index.total:
        return []
    checkpoint = min((start - 1) // STEP, len(index.offsets) - 1)
    line_no = checkpoint * STEP + 1
    out: list[str] = []
    with open(path, "rb") as handle:
        handle.seek(index.offsets[checkpoint])
        for raw in handle:
            if line_no >= start:
                text = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                out.append(text)
                if len(out) >= count:
                    break
            line_no += 1
    return out


def clip_line(text: str, *, limit: int = MAX_LINE_CHARS, column: int = 0) -> str:
    """``text`` from ``column`` (0-based), cut at ``limit`` characters with a note of what is left."""
    if column > 0:
        if column >= len(text):
            return f"[line has {len(text)} chars; col={column} is past its end]"
        text_from = text[column:]
        prefix = f"[from col={column}] "
    else:
        text_from = text
        prefix = ""
    if len(text_from) <= limit:
        return prefix + text_from
    rest = len(text_from) - limit
    return prefix + text_from[:limit] + f" …[+{rest} chars; read on with col={column + limit}]"


def is_large(path: Path) -> bool:
    try:
        return os.path.getsize(path) > LARGE_FILE_BYTES
    except OSError:
        return False
