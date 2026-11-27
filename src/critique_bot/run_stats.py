# SPDX-License-Identifier: Apache-2.0
"""Counters for one crit run, saved with the transcript so ``crit eval`` can measure the harness.

The engine and the model backends report here; nothing reads the counters while the run
is going. One run per process: :func:`reset` starts over, :func:`snapshot` is what gets
saved (``"stats"`` in ``agent.json``).

Counted:

* ``model_requests`` and token usage (``prompt_tokens``, ``completion_tokens``,
  ``cached_tokens``) as the server reports them; requests without usage are counted in
  ``requests_without_usage``;
* ``tool_calls`` by tool name, ``tool_errors`` (results with ``ok: false`` that were not
  a denial or a plan-mode block), ``malformed_calls`` (tool markup that did not parse);
* ``edits`` per tool: calls, ok, seconds, and an estimate of the output tokens the model
  spent on the arguments (characters / 4);
* ``repeat_breaks`` (identical calls refused as a loop), ``compactions`` (the chat moved
  to a new conversation), ``stale_edit_refusals`` (an edit refused because the file changed
  on disk since the model read it), ``unread_edit_refusals`` (an edit of a file never read);
* ``edits[tool]["steps"]``: how each edit matched (the match-ladder step, or ambiguous /
  refused / guard_rejected), recorded by the tools themselves;
* ``exit_reason``: the last status the task reported (COMPLETED, FAILED, ...).
"""

from __future__ import annotations

import threading
from collections import Counter
from typing import Any

#: Tools whose calls count as edits.
EDIT_TOOLS = frozenset({"edit_file", "write_files", "apply_patch", "delete_file", "move_file"})

_lock = threading.Lock()
_counts: Counter[str] = Counter()
_tools: Counter[str] = Counter()
_edits: dict[str, dict[str, float]] = {}
_steps: dict[str, Counter[str]] = {}
_values: dict[str, Any] = {}


def reset() -> None:
    with _lock:
        _counts.clear()
        _tools.clear()
        _edits.clear()
        _steps.clear()
        _values.clear()


def count(name: str, amount: int = 1) -> None:
    with _lock:
        _counts[name] += amount


def set_value(name: str, value: Any) -> None:
    with _lock:
        _values[name] = value


def add_usage(prompt: Any = None, completion: Any = None, cached: Any = None) -> None:
    """One model request and what it cost, as far as the server said."""
    with _lock:
        _counts["model_requests"] += 1
        known = False
        for key, value in (("prompt_tokens", prompt), ("completion_tokens", completion), ("cached_tokens", cached)):
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                _counts[key] += value
                known = known or key != "cached_tokens"
        if not known:
            _counts["requests_without_usage"] += 1


def tool_result(name: str, result: dict[str, Any] | None, *, malformed: bool = False) -> None:
    """One tool call as the model sent it and the result it got."""
    with _lock:
        if malformed:
            _counts["malformed_calls"] += 1
            return
        _tools[name or "?"] += 1
        result = result or {}
        if not result.get("ok") and not result.get("denied") and not result.get("planned") and not result.get("repeat"):
            _counts["tool_errors"] += 1
            if name in EDIT_TOOLS:
                _counts["edit_failures"] += 1


def edit(name: str, *, ok: bool, seconds: float, argument_chars: int) -> None:
    """One edit call that ran: how long it took and roughly what the model wrote for it."""
    with _lock:
        row = _edits.setdefault(name, {"calls": 0, "ok": 0, "seconds": 0.0, "out_tokens": 0})
        row["calls"] += 1
        row["ok"] += 1 if ok else 0
        row["seconds"] = round(row["seconds"] + max(0.0, seconds), 3)
        row["out_tokens"] += max(0, argument_chars) // 4


def edit_step(name: str, step: str) -> None:
    """How one edit (or one patch hunk) matched: a match-ladder step (exact, whitespace, indent,
    punctuation, fuzzy) or why it did not (ambiguous, refused, guard_rejected)."""
    with _lock:
        _steps.setdefault(name, Counter())[step or "?"] += 1


def snapshot() -> dict[str, Any]:
    with _lock:
        out: dict[str, Any] = {
            key: _counts.get(key, 0)
            for key in (
                "model_requests",
                "prompt_tokens",
                "completion_tokens",
                "cached_tokens",
                "requests_without_usage",
                "malformed_calls",
                "tool_errors",
                "edit_failures",
                "repeat_breaks",
                "compactions",
                "stale_edit_refusals",
            )
        }
        out["tool_calls"] = dict(_tools)
        out["edits"] = {name: dict(row) for name, row in _edits.items()}
        for name, steps in _steps.items():
            out["edits"].setdefault(name, {})["steps"] = dict(steps)
        out.update(_values)
        for key, value in _counts.items():
            out.setdefault(key, value)
        return out
