"""What a reply means when it has no tool calls: a finish code, a refusal, a question, a plan.

Pure functions over the text, used by the turn loop to decide the next message.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from critique_bot import code_index
from critique_bot.engine.parse import (
    _BLOCK_RE,
    _OPEN_RE,
    _STATUS_OK,
    _STATUS_WORDS,
    ToolCall,
    _hide_tool_markup,
    parse_tool_calls,
)
from critique_bot.engine.text import _one_line
from critique_bot.model_api import COMPLETION_IDLE
from critique_bot.tool_api import canonical_tool


_PLAN_RE = re.compile(r"<plan>\s*(.*?)\s*</plan>", re.IGNORECASE | re.DOTALL)


_UNTAGGED_PLAN_RE = re.compile(r"(?is)\bfiles?\s*:.{8,}?\bchange\s*:.{8,}?\bcheck\s*:")


_EDIT_TASK_RE = re.compile(
    r"\b(add|create|update|fix|change|remove|delete|rename|write|implement|refactor|"
    r"replace|edit|modify|insert|append|move|convert|migrate|bump)\b",
    re.IGNORECASE,
)


_ANSWER_TASK_RE = re.compile(
    r"(?is)^(?:please\s+|can you\s+|could you\s+|would you\s+)?"
    r"(?:what|why|how|who|when|where|which|explain|describe|summarize|summarise|"
    r"tell me|define|compare|is|are|does|do|can|could|should|would|what's|whats)\b"
)


_PROMISE_START_RE = re.compile(
    r"(?i)^(i'll|i will|let me|next i|i'm going to|i am going to)\s+"
    r"(look|read|check|search|edit|update|fix|open|find|run|start|try|use|inspect|review)\b"
)


_FAIL_HEAD = re.compile(r"^(FAILED|BLOCKED)\b\s*[:\-—]?\s*(.*)$")


#: Error text the chat page shows in place of a reply.
_PROVIDER_ERROR_RE = re.compile(
    r"(?i)(something went wrong|network error|there was an error generating|"
    r"an error occurred|error in (?:the )?message stream|you'?ve reached (?:our|the|your)|"
    r"unusual activity|too many requests|rate limit|conversation not found|"
    r"request timed out|load failed|failed to fetch|please try again later|"
    r"hmm\.\.\.\s*something seems to have gone wrong)"
)


_PROVIDER_ERROR_MAX = 400


_PERMISSION_MARKERS = (
    "may i", "can i proceed", "should i proceed", "should i go ahead", "shall i proceed",
    "is it ok", "is that ok", "okay to", "ok to proceed", "permission", "confirm", "approve",
)


_TEST_MARKERS = (
    "tests", "test", "__tests__", "spec", "pytest.ini", "tox.ini", "package.json", "gradlew",
    "gradlew.bat", "build.gradle", "build.gradle.kts", "pom.xml", "Cargo.toml", "go.mod", "Makefile",
    "CMakeLists.txt", "setup.py", "pyproject.toml",
)


def _status_code(text: str) -> str | None:
    """Return a finish code when the reply is that word, starts with FAILED or BLOCKED, or ends with it."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    compact = re.sub(r"[^a-z]+", " ", text.lower()).strip()
    if compact in _STATUS_WORDS:
        return _STATUS_WORDS[compact]
    head = _FAIL_HEAD.match(lines[0])
    if head:
        return _STATUS_WORDS[head.group(1).lower()]
    if len(lines) > 4:
        return None
    last = re.sub(r"[^a-z]+", " ", lines[-1].lower()).strip()
    return _STATUS_WORDS.get(last)


def _failure_reason(text: str) -> str:
    """The sentences after FAILED or BLOCKED. Empty when the reply is only the word."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    kept: list[str] = []
    for line in lines:
        match = _FAIL_HEAD.match(line)
        if match:
            if match.group(2).strip():
                kept.append(match.group(2).strip())
            continue
        if re.sub(r"[^a-z]+", " ", line.lower()).strip() in _STATUS_WORDS:
            continue
        kept.append(line)
    return " ".join(kept).strip()


def _normalized(text: str) -> str:
    return text.lower().replace("\u2019", "'").replace("\u2018", "'")


def _no_edit_needed(text: str) -> bool:
    """True when the model says the task needs no further edit."""
    if _status_code(text) in _STATUS_OK:
        return True
    normalized = _normalized(text)
    if any(marker in normalized for marker in ("i'll ", "i will ", "let me ", "going to ", "next i")):
        return False
    markers = (
        "no edit needed", "no edits needed", "no edit is needed", "no further edit",
        "no further change", "no change needed", "no changes needed", "nothing to change",
        "nothing to edit", "already present", "already done", "already applied",
        "already contains", "already in the file", "no modification", "does not need an edit",
        "does not need to edit", "do not need to edit", "don't need to edit", "no additional change",
    )
    return any(marker in normalized for marker in markers)


_REFUSAL_MARKERS = (
    "i can't", "i cannot", "unable", "not able", "aren't available", "are not available",
    "not available", "isn't available", "not exposed", "not expose", "tool interface",
    "no tool", "no repository",
    "file-operation", "file operation", "don't have", "do not have", "i won't", "i will not",
)


_QUESTION_MARKERS = (
    "what would you like", "what should i", "would you like", "let me know", "for example",
    "shall i", "do you want", "what do you want",
)


_PROMISE_MARKERS = ("i'll ", "i will ", "let me ", "next i", "going to ")


#: The model handing the work back: "build it yourself", "run this in Android Studio".
_HAND_BACK_RE = re.compile(
    r"(?:\b(?:yourself|manually)\b"
    r"|\bon your (?:machine|computer|system|side|end|local)\b"
    r"|\b(?:please|you can|you could|you should|you(?:'ll| will)? need to|you may need to|you must|kindly)\s+"
    r"(?:\w+\s+){0,2}(?:run|build|execute|install|open|launch|rebuild|sync|try running)\b"
    r"|\b(?:open|build|run|sync)\b[^.\n]{0,40}\bin android studio\b"
    r"|\brun (?:the following|these|this) commands?\b"
    r"|\bi (?:can(?:no|')t|am unable to|'m unable to|do not have the ability to|don't have the ability to) (?:run|execute|build)\b)",
    re.I,
)


MAX_HAND_BACKS = 2


def _hands_back(text: str) -> bool:
    """True when the reply tells the user to run, build, or install something themselves."""
    return bool(_HAND_BACK_RE.search(_hide_tool_markup(text or "")))


def _refuses(text: str) -> bool:
    normalized = _normalized(text)
    return any(marker in normalized for marker in _REFUSAL_MARKERS)


def _stalls(text: str) -> bool:
    """True when the reply asks the user, refuses, or only promises work."""
    normalized = _normalized(text)
    if "?" in normalized:
        return True
    markers = _QUESTION_MARKERS + _REFUSAL_MARKERS + _PROMISE_MARKERS
    return any(marker in normalized for marker in markers)


def _asks_user(text: str) -> bool:
    normalized = _normalized(text)
    return any(marker in normalized for marker in _QUESTION_MARKERS)


def _only_promises(text: str) -> bool:
    """True when the reply only says it will go look, and has not answered yet."""
    return bool(_PROMISE_START_RE.match(_normalized(text).strip()))


def _answer_only(task: str) -> bool:
    """True when the user asked a question and did not ask for a file change.

    A question that also says to fix or add something is still a task: the
    answer and the tool calls belong in the same reply.
    """
    text = " ".join(task.split())
    if not text or _asks_for_change(text):
        return False
    if text.endswith("?"):
        return True
    return bool(_ANSWER_TASK_RE.match(text))


def _plan_text(reply: str) -> str | None:
    parts = [part.strip() for part in _PLAN_RE.findall(reply)]
    if parts:
        return "\n".join(part for part in parts if part)
    bare = _BLOCK_RE.sub("", reply).strip()
    if bare and _UNTAGGED_PLAN_RE.search(bare):
        return bare
    return None


def _idle_tool_reply(detail: Any, reply: str) -> bool:
    """True when an idle reply contains a tool tag that did not finish."""
    if not isinstance(detail, dict) or detail.get("completion") != COMPLETION_IDLE:
        return False
    if "<tool_call" not in reply.lower():
        return False
    calls, unclosed = parse_tool_calls(reply)
    return unclosed or not calls


def _asks_for_change(task: str) -> bool:
    return bool(_EDIT_TASK_RE.search(task))


def _provider_error(reply: str) -> str | None:
    """The chat page's own error text (or nothing) in place of a model reply."""
    text = (reply or "").strip()
    if not text:
        return "the chat page returned an empty reply"
    if len(text) > _PROVIDER_ERROR_MAX or _OPEN_RE.search(text):
        return None
    match = _PROVIDER_ERROR_RE.search(text)
    if match is None:
        return None
    return f"the chat page said: {_one_line(text, 120)}"


def _real_question(reply: str) -> str:
    """The question when a reply genuinely asks the user something, else "".

    Questions that only stall (what should I change, shall I proceed, may I)
    and refusals are not real questions: the task already says what to do.
    """
    text = _hide_tool_markup(reply).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[-1].endswith("?") or len(text) > 1_500:
        return ""
    normalized = _normalized(text)
    if _refuses(text) or any(marker in normalized for marker in _QUESTION_MARKERS + _PERMISSION_MARKERS):
        return ""
    return text


def _diffstat(diff: str) -> str:
    """``a.py +3 -1, b.py +10 -0`` from a unified diff."""
    counts: dict[str, list[int]] = {}
    current = ""
    old = ""
    lines = diff.splitlines()
    for number, line in enumerate(lines):
        header = number + 1 < len(lines) and lines[number + 1].startswith("+++ ")
        if line.startswith("--- ") and header:
            old = re.sub(r"^--- (?:a/)?", "", line).strip()
            current = ""
        elif line.startswith("+++ ") and number > 0 and lines[number - 1].startswith("--- "):
            current = re.sub(r"^\+\+\+ (?:b/)?", "", line).strip()
            if current == "/dev/null":
                current = old if old != "/dev/null" else ""
            if current:
                counts.setdefault(current, [0, 0])
        elif current and line.startswith("+"):
            counts[current][0] += 1
        elif current and line.startswith("-"):
            counts[current][1] += 1
    return ", ".join(f"{path} +{add} -{rem}" for path, (add, rem) in counts.items())


def _has_checks(workspace: Path) -> bool:
    """True when the workspace has tests or a build the model could run."""
    try:
        return any((workspace / marker).exists() for marker in _TEST_MARKERS)
    except OSError:
        return False


def _call_key(call: ToolCall) -> str:
    name = canonical_tool(call.tool) or call.tool
    return name + ":" + json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)


def _keywords(task: str) -> list[str]:
    words: list[str] = []
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.\-/]{2,}", task):
        words.append(token)
        words.extend(code_index.split_ident(Path(token).stem))
    stop = {"the", "and", "for", "with", "this", "that", "from", "into", "file", "files", "code", "test", "tests"}
    seen: list[str] = []
    for word in words:
        lowered = word.lower()
        if lowered in stop or len(lowered) < 3 or lowered in seen:
            continue
        seen.append(lowered)
    return seen[:20]
