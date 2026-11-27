# SPDX-License-Identifier: Apache-2.0
"""Compaction for model API sessions, driven by measured context pressure.

The budget is the context window ``B`` (``local.num_ctx``, else what the server reports).
Pressure is the last request's prompt and reply tokens (from the server's usage) plus an
estimate of the message about to go. At a turn boundary, at most once every
``every_turns`` replies:

* above ``mask_at`` (0.70 B): **mask** tool results older than the last ``keep_results``
  (10). Each becomes a pointer: what the call was (tool, path, range or command) and the
  blob in the session log that holds the original. Cheap, and it keeps failure signals in
  the recent turns (The Complexity Trap: masking matches summarizing at half the cost).
* above ``summarize_at`` (0.85 B), or still above it after masking: **summarize**. One
  helper request writes a structured summary (goal, decisions, files changed, open
  problems, failing tests, next steps) of everything but the last ``keep_turns`` (3)
  turns; the 5 files read most recently are re-attached fresh, and the todo list is
  recited at the end. Without a helper the summary is built from the task state.
* ``prefer_summary`` (a thinking-off model that loses more from masking): summarize
  instead of masking.

The first message (the cached prefix) is kept byte for byte; the summary is appended to
it. Turn-, time- and size-based rotation is the web chat's, and stays there.

Engine layer. The engine applies a :class:`Result` with the session's
``replace_history`` and records it as a ``compaction`` event.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from critique_bot import log

MASK_AT = 0.70
SUMMARIZE_AT = 0.85
KEEP_RESULTS = 10
KEEP_TURNS = 3
REATTACH_FILES = 5
REATTACH_CHARS = 8_000
EVERY_TURNS = 5
#: Characters per token when only characters are known (code and prose mixed).
CHARS_PER_TOKEN = 3.5

ELIDED = "[elided:"
SUMMARY_MARK = "CONTEXT SUMMARY ("
SUMMARY_PROMPT = (
    "Context is running out, so this conversation is about to be compacted. Write a summary that "
    "lets you carry on without it. Do not call any tool. Use exactly these headings, each with "
    "short bullet points (write 'none' when empty):\n"
    "GOAL\nDECISIONS (what you chose and why)\nFILES CHANGED (path: what changed)\n"
    "OPEN PROBLEMS\nFAILING TESTS (name and the error)\nNEXT STEPS\n"
    "Keep exact file paths, function names, commands and error messages."
)


@dataclass(frozen=True)
class Policy:
    window: int
    mask_at: float = MASK_AT
    summarize_at: float = SUMMARIZE_AT
    keep_results: int = KEEP_RESULTS
    keep_turns: int = KEEP_TURNS
    reattach_files: int = REATTACH_FILES
    reattach_chars: int = REATTACH_CHARS
    every_turns: int = EVERY_TURNS
    prefer_summary: bool = False

    @classmethod
    def from_settings(cls, window: int, settings: dict[str, Any] | None) -> "Policy":
        """``compaction_*`` keys in ``.crit/settings.json`` override the defaults."""
        options = settings if isinstance(settings, dict) else {}

        def number(key: str, default: float, low: float, high: float) -> float:
            raw = options.get(key)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return default
            return min(high, max(low, float(raw)))

        prefer = options.get("compaction_prefer_summary")
        if prefer is None:
            prefer = options.get("masking_penalty")
        return cls(
            window=int(window),
            mask_at=number("compaction_mask_at", MASK_AT, 0.2, 0.95),
            summarize_at=number("compaction_summarize_at", SUMMARIZE_AT, 0.3, 0.98),
            keep_results=int(number("compaction_keep_results", KEEP_RESULTS, 1, 100)),
            keep_turns=int(number("compaction_keep_turns", KEEP_TURNS, 1, 20)),
            every_turns=int(number("compaction_every_turns", EVERY_TURNS, 1, 100)),
            prefer_summary=prefer is True,
        )


@dataclass
class Context:
    """What the engine knows at a turn boundary."""

    #: The last request's prompt plus reply tokens, as the server reported them (0: unknown).
    used_tokens: int
    #: The size of the message about to be sent.
    pending_chars: int = 0
    #: Model replies so far in the session.
    turn: int = 0
    task: str = ""
    todos: list[dict[str, Any]] = field(default_factory=list)
    #: Files read in this task, oldest first (workspace-relative).
    recent_files: list[str] = field(default_factory=list)
    workspace: Path | None = None
    #: For a summary without a helper: files changed, last command and output, notes.
    facts: dict[str, str] = field(default_factory=dict)


@dataclass
class Result:
    strategy: str
    messages: list[dict[str, Any]]
    tokens_before: int
    tokens_after: int
    reason: str
    #: mask: [{"index", "text", "blob"}]; summarize: the range summarized and the summary text.
    masked: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    summarized: tuple[int, int] | None = None
    helper: bool = False


def message_chars(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        total += len(str(message.get("content") or ""))
        if message.get("tool_calls"):
            total += len(json.dumps(message["tool_calls"], ensure_ascii=False))
    return total


def _is_result(message: dict[str, Any]) -> bool:
    """A message that carries tool results: a native ``tool`` message, or a text-format result payload."""
    if message.get("role") == "tool":
        return True
    if message.get("role") != "user":
        return False
    head = str(message.get("content") or "").lstrip()[:200]
    return head.startswith('{"input"') or "<tool_result" in head


def _call_index(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id"):
                calls[str(call["id"])] = call
    return calls


def describe_call(call: dict[str, Any] | None) -> str:
    """``read_files src/x.py lines 1-100`` for a native tool call, from its name and arguments."""
    if not call:
        return "a tool result"
    function = call.get("function") or {}
    name = str(function.get("name") or "tool")
    raw = function.get("arguments")
    try:
        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    parts = [name]
    paths = args.get("paths") or args.get("path") or args.get("file")
    if isinstance(paths, list):
        shown = []
        for item in paths[:3]:
            if isinstance(item, dict):
                shown.append(str(item.get("path") or "") + _span(item))
            else:
                shown.append(str(item))
        parts.append(", ".join(shown) + (" ..." if len(paths) > 3 else ""))
    elif paths:
        parts.append(str(paths))
    span = _span(args)
    if span:
        parts.append(span.strip())
    for key in ("command", "pattern", "query", "url"):
        if args.get(key):
            parts.append(f"{key} {' '.join(str(args[key]).split())[:100]}")
            break
    return " ".join(parts)


def _span(args: dict[str, Any]) -> str:
    """`` lines 40-139`` from read_files' offset and limit, or ""."""
    offset, limit = args.get("offset"), args.get("limit")
    if not isinstance(offset, int) and not isinstance(limit, int):
        return ""
    start = offset if isinstance(offset, int) and offset > 0 else 1
    return f" lines {start}-{start + limit - 1}" if isinstance(limit, int) and limit > 0 else f" from line {start}"


class Compactor:
    """Decides when to compact and builds the new message list. It changes nothing itself."""

    def __init__(
        self,
        policy: Policy,
        *,
        summarize: Callable[[list[dict[str, Any]], str], str] | None = None,
        put_blob: Callable[[str], str] | None = None,
        redact: Callable[[str], str] | None = None,
    ) -> None:
        self.policy = policy
        self.summarize = summarize
        self.put_blob = put_blob or (lambda _text: "")
        self.redact = redact or (lambda text: text)
        #: The turn of the last compaction (or check that found nothing to do).
        self.last_turn: int | None = None

    def estimate(self, ctx: Context) -> int:
        return int(ctx.used_tokens + ctx.pending_chars / CHARS_PER_TOKEN)

    def due(self, ctx: Context) -> str:
        """"mask", "summarize", or "" (not now)."""
        if self.policy.window <= 0 or ctx.used_tokens <= 0:
            return ""
        if self.last_turn is not None and ctx.turn - self.last_turn < self.policy.every_turns:
            return ""
        share = self.estimate(ctx) / self.policy.window
        if share > self.policy.summarize_at:
            return "summarize"
        if share > self.policy.mask_at:
            return "summarize" if self.policy.prefer_summary else "mask"
        return ""

    def compact(self, messages: list[dict[str, Any]], ctx: Context) -> Result | None:
        """The compacted list, or None when nothing is due or nothing could be removed."""
        action = self.due(ctx)
        if not action:
            return None
        self.last_turn = ctx.turn
        before = self.estimate(ctx)
        chars_before = max(1, message_chars(messages))
        per_char = before / chars_before
        share = before / self.policy.window
        reason = f"context at {share:.0%} of {self.policy.window} tokens"
        result: Result | None = None
        current = messages
        if action == "mask":
            masked, entries = self.mask(messages)
            if entries:
                after = int(message_chars(masked) * per_char)
                result = Result("mask", masked, before, after, reason, masked=entries)
                current = masked
                if after <= self.policy.summarize_at * self.policy.window:
                    return result
            elif share <= self.policy.summarize_at:
                return None
        summarized = self.summarize_history(current, ctx)
        if summarized is None:
            return result
        new_messages, summary, span, helper = summarized
        after = int(message_chars(new_messages) * per_char)
        return Result(
            "summarize",
            new_messages,
            before,
            after,
            reason,
            masked=result.masked if result else [],
            summary=summary,
            summarized=span,
            helper=helper,
        )

    # ------------------------------------------------------------------ masking

    def mask(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Replace tool results older than the last ``keep_results`` with pointers to blobs."""
        results = [index for index, message in enumerate(messages) if index > 0 and _is_result(message)]
        old = results[: max(0, len(results) - self.policy.keep_results)]
        calls = _call_index(messages)
        out = [dict(message) for message in messages]
        entries: list[dict[str, Any]] = []
        for index in old:
            content = str(out[index].get("content") or "")
            if content.startswith(ELIDED) or len(content) < 200:
                continue
            blob = self.put_blob(content)
            if out[index].get("role") == "tool":
                what = describe_call(calls.get(str(out[index].get("tool_call_id") or "")))
            else:
                what = "tool results from an earlier turn"
            text = (
                f"{ELIDED} {what} -> blob {blob[:12] or '(not kept)'}, {len(content)} chars. "
                "Run the call again if you need it]"
            )
            out[index]["content"] = text
            entries.append({"index": index, "text": text, "blob": blob})
        return out, entries

    # ------------------------------------------------------------------ summarizing

    def summarize_history(
        self, messages: list[dict[str, Any]], ctx: Context
    ) -> tuple[list[dict[str, Any]], str, tuple[int, int], bool] | None:
        """(new list, summary, (first, last) index summarized, whether a helper wrote it), or None."""
        assistants = [index for index, message in enumerate(messages) if message.get("role") == "assistant"]
        if len(assistants) <= self.policy.keep_turns or not messages:
            return None
        tail_start = assistants[-self.policy.keep_turns]
        if tail_start <= 1:
            return None
        summary = ""
        helper = False
        if self.summarize is not None:
            try:
                summary = (self.summarize(messages[:tail_start], SUMMARY_PROMPT) or "").strip()
                helper = bool(summary)
            except Exception as exc:  # the mechanical summary still works
                log.warn(f"compaction: the summary request failed ({exc}); using the task state instead")
        if not summary:
            summary = self.state_summary(ctx)
        block = self.summary_block(summary, ctx)
        first = dict(messages[0])
        prefix = str(first.get("content") or "")
        cut = prefix.find("\n\n" + SUMMARY_MARK)
        if cut >= 0:
            prefix = prefix[:cut]
        first["content"] = prefix + "\n\n" + block
        new = [first] + [dict(message) for message in messages[tail_start:]]
        return new, summary, (1, tail_start - 1), helper

    def state_summary(self, ctx: Context) -> str:
        facts = ctx.facts
        todo_next = [str(item.get("content")) for item in ctx.todos if item.get("status") != "completed"]
        sections = [
            ("GOAL", ctx.task.strip()[:1_000] or "none"),
            ("DECISIONS", facts.get("notes") or "none"),
            ("FILES CHANGED", facts.get("changed") or "none"),
            ("OPEN PROBLEMS", facts.get("problems") or "none"),
            ("FAILING TESTS", facts.get("failing") or "none"),
            ("NEXT STEPS", "; ".join(todo_next[:8]) or "carry on with the goal"),
        ]
        return "\n".join(f"{name}\n{body}" for name, body in sections)

    def summary_block(self, summary: str, ctx: Context) -> str:
        parts = [
            f"{SUMMARY_MARK}crit compacted the earlier turns to save context; the last "
            f"{self.policy.keep_turns} turns follow unchanged)\n{summary.strip()}"
        ]
        files = self.reattach(ctx)
        if files:
            parts.append("FILES YOU READ MOST RECENTLY (fresh copies)\n" + files)
        if ctx.todos:
            parts.append(
                "TODO (the current list)\n"
                + "\n".join(f"- [{item.get('status')}] {item.get('content')}" for item in ctx.todos[:20])
            )
        return self.redact("\n\n".join(parts))

    def reattach(self, ctx: Context) -> str:
        """The newest ``reattach_files`` files read, read again now, each capped."""
        if ctx.workspace is None or self.policy.reattach_files <= 0:
            return ""
        from critique_bot.redaction import sensitive_file

        chosen: list[str] = []
        for rel in reversed(ctx.recent_files):
            if rel not in chosen:
                chosen.append(rel)
            if len(chosen) >= self.policy.reattach_files:
                break
        parts: list[str] = []
        root = Path(ctx.workspace).resolve()
        for rel in reversed(chosen):
            path = (root / rel).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if sensitive_file(path) or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(text) > self.policy.reattach_chars:
                text = text[: self.policy.reattach_chars] + f"\n[... cut at {self.policy.reattach_chars} chars; read the rest]"
            numbered = "\n".join(f"{number:>5}  {line}" for number, line in enumerate(text.splitlines(), 1))
            parts.append(f"=== {rel} ===\n{numbered}")
        return "\n\n".join(parts)


def reply_text(reply: str) -> str:
    """The words of a reply, also when it came as Responses items (a native session)."""
    text = (reply or "").strip()
    if not text.startswith('{"output"'):
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    words: list[str] = []
    for item in data.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("text"):
                    words.append(str(part["text"]))
    return "\n".join(words).strip()
