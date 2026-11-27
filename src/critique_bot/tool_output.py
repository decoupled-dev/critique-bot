"""Tool results sized to the context: head and tail kept, the whole text saved, never empty.

Every built-in tool result passes through :func:`shape_output` (re-exported by
``tool_api``). A result whose text is over the budget keeps its head and its tail, and
the complete text is written to ``.crit/cache/out/<id>.txt`` with that path in the result,
so nothing is lost: the model reads the middle with read_files (offset, limit) or searches
it with search_code (path), Manus-style recoverable compression. Only the newest
:data:`KEEP_FILES` saved outputs are kept.

The budget is ``tool_output_cap_tokens`` from the settings (about 4 characters a token),
else the engine's per-result size (``ToolContext.max_chars``), and never more than
:data:`DEFAULT_CAP_TOKENS` unless the setting asks for it.

A successful result with no text says so instead of coming back empty.
"""

from __future__ import annotations

import itertools
import os
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_CAP_TOKENS = 10_000
CHARS_PER_TOKEN = 4
#: Saved full outputs kept in .crit/cache/out; older ones are removed.
KEEP_FILES = 200
#: Share of the budget given to the head; the tail (where errors and summaries are) gets the rest.
HEAD_SHARE = 0.4
#: The smallest budget honored (a tiny setting would leave nothing readable).
MIN_BUDGET_CHARS = 1_000

_LOCK = threading.Lock()
_COUNTER = itertools.count(1)


def budget_chars(max_chars: int | None, settings: dict[str, Any] | None = None) -> int:
    """The character budget for one tool result."""
    raw = (settings or {}).get("tool_output_cap_tokens")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return max(MIN_BUDGET_CHARS, int(raw * CHARS_PER_TOKEN))
    cap = DEFAULT_CAP_TOKENS * CHARS_PER_TOKEN
    if isinstance(max_chars, int) and max_chars > 0:
        cap = min(cap, max_chars)
    return max(MIN_BUDGET_CHARS, cap)


def out_dir(cache_dir: Path | str) -> Path:
    return Path(cache_dir) / "out"


def save_full(text: str, cache_dir: Path | str | None, workspace: Path | None = None) -> str:
    """Write ``text`` to ``.crit/cache/out/<id>.txt``; the path to show ("" when it could not be saved)."""
    if cache_dir is None:
        return ""
    folder = out_dir(cache_dir)
    try:
        with _LOCK:
            folder.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            while True:
                path = folder / f"{stamp}-{os.getpid()}-{next(_COUNTER)}.txt"
                if not path.exists():
                    break
            path.write_text(text, encoding="utf-8", errors="replace")
            _prune(folder)
    except OSError:
        return ""
    if workspace is not None:
        try:
            return path.resolve().relative_to(Path(workspace).resolve()).as_posix()
        except ValueError:
            pass
    return str(path)


def _prune(folder: Path) -> None:
    files = sorted(folder.glob("*.txt"), key=lambda item: (item.stat().st_mtime, item.name))
    for stale in files[:-KEEP_FILES]:
        try:
            stale.unlink()
        except OSError:
            pass


def head_tail(text: str, budget: int) -> tuple[str, int, int]:
    """``text`` cut to about ``budget`` characters on line boundaries: head, then tail.

    Returns the kept head plus tail joined by nothing (the caller adds the marker), and how
    many lines and characters were left out.
    """
    if len(text) <= budget:
        return text, 0, 0
    head_room = max(1, int(budget * HEAD_SHARE))
    tail_room = max(1, budget - head_room)
    head = text[:head_room]
    cut = head.rfind("\n")
    if cut > head_room // 2:
        head = head[: cut + 1]
    tail = text[-tail_room:]
    cut = tail.find("\n")
    if 0 <= cut < tail_room // 2:
        tail = tail[cut + 1 :]
    middle = text[len(head) : len(text) - len(tail)]
    return head + "\x00" + tail, middle.count("\n"), len(middle)


def shape_text(text: str, budget: int, *, cache_dir: Path | str | None = None,
               workspace: Path | None = None) -> tuple[str, str]:
    """``text`` within ``budget``, and the path of the saved full text ("" when nothing was cut)."""
    if len(text) <= budget:
        return text, ""
    saved = save_full(text, cache_dir, workspace)
    total_lines = text.count("\n") + 1
    where = (
        f"the full output ({total_lines:,} lines) is in {saved}: read the middle with read_files (path, offset, "
        "limit) or search it with search_code (path)"
        if saved else "narrow the call (a path, an offset and limit, a tighter pattern) to see the rest"
    )
    marker_room = 400
    kept, lines, chars = head_tail(text, max(200, budget - marker_room))
    head, tail = kept.split("\x00", 1)
    marker = f"\n... [{lines:,} lines ({chars:,} characters) left out here; {where}] ...\n"
    return head.rstrip("\n") + marker + tail, saved


def shape_output(result: dict[str, Any], budget: int, *, cache_dir: Path | str | None = None,
                 workspace: Path | None = None) -> dict[str, Any]:
    """One tool result within ``budget`` characters; never empty. Changes and returns ``result``."""
    output = result.get("output")
    if isinstance(output, str) and output:
        shaped, saved = shape_text(output, budget, cache_dir=cache_dir, workspace=workspace)
        if saved or shaped != output:
            result["output"] = shaped
            if saved:
                result["full_output"] = saved
    elif result.get("ok") and not (isinstance(output, str) and output.strip()):
        result["output"] = f"(no output; {result.get('tool') or 'the tool'} succeeded)"
    error = result.get("error")
    if isinstance(error, str) and len(error) > budget:
        result["error"], _ = shape_text(error, budget)
    if not result.get("ok") and not (isinstance(error, str) and error.strip()):
        result["error"] = f"{result.get('tool') or 'the tool'} failed without saying why"
    return result
