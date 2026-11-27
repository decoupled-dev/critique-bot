# SPDX-License-Identifier: Apache-2.0
"""The verified flow's REVIEW: the review JSON contract, and the ``verifier`` agent in a fresh chat.

The reviewer sees the task, the final diff and the evidence (repro and tests, before and
after), not the author's reasoning, and answers in the review mode's contract: a ``Risk:``
line, a summary and a JSON fence with ``risk`` and ``comments`` (``path``, ``line``,
``severity``, ``body``). ``blocker``, ``must-fix`` and ``security`` findings are blocking.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from critique_bot.engine.text import _one_line
from critique_bot.tool_api import fill

#: Severities that send the change back to FIX (the review contract's must-fix kinds).
BLOCKING = frozenset({"blocker", "must-fix", "must_fix", "security", "high", "critical"})
#: Tool rounds the reviewer gets when its agent file sets no max_steps.
REVIEW_MAX_ROUNDS = 12


@dataclass
class Review:
    risk: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    skipped: str = ""
    text: str = ""

    @property
    def blocking(self) -> list[dict[str, Any]]:
        return [item for item in self.findings if str(item.get("severity") or "").lower() in BLOCKING]

    def as_dict(self) -> dict[str, Any]:
        return {"risk": self.risk or None, "findings": len(self.findings), "blocking": len(self.blocking),
                "skipped": self.skipped or None,
                "items": [{key: item.get(key) for key in ("path", "line", "severity", "body")} for item in self.findings[:20]]}


_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.S | re.I)
_RISK_RE = re.compile(r"^\W*risk\W*:\s*\**\s*(safe|moderate|risky|blocker)", re.I | re.M)


def parse_review(text: str) -> dict[str, Any] | None:
    """The review JSON (``risk`` / ``comments``) in a reply, as the review mode writes it; None if absent."""
    from critique_bot.engine.json_repair import _json_objects, _loads_lenient

    body = str(text or "")
    blobs = [match.group(1) for match in _FENCE_RE.finditer(body)] + _json_objects(body)
    found: dict[str, Any] | None = None
    for blob in blobs:
        try:
            value = json.loads(blob)
        except ValueError:
            try:
                value = _loads_lenient(blob)
            except ValueError:
                continue
        if isinstance(value, dict) and ("comments" in value or "risk" in value or "findings" in value):
            found = value
    if found is None:
        return None
    comments = found.get("comments", found.get("findings")) or []
    found["comments"] = [item for item in comments if isinstance(item, dict)] if isinstance(comments, list) else []
    return found


def review_from_text(text: str) -> Review:
    payload = parse_review(text)
    if payload is None:
        return Review(skipped="the reviewer's answer had no review JSON", text=text)
    risk = str(payload.get("risk") or "").strip().lower()
    if not risk:
        match = _RISK_RE.search(text)
        risk = match.group(1).lower() if match else ""
    findings = []
    for item in payload["comments"]:
        severity = str(item.get("severity") or item.get("sev") or "").strip().lower()
        if severity in {"nit", "nits", "praise", "lgtm", "style", "info", "note", "ok"}:
            continue
        findings.append({**item, "severity": severity})
    return Review(risk=risk, findings=findings, text=text)


@dataclass(frozen=True)
class ReviewRequest:
    """What the reviewer sees: the task, the final diff, the evidence. Not the author's reasoning."""

    task: str
    diff: str
    evidence: str
    workspace: Path


#: A reviewer: the request in, the reviewer's answer (summary + review JSON) out.
Reviewer = Callable[[ReviewRequest], str]


def review_in_chat(
request: ReviewRequest,
*,
open_session: Callable[[], Any],
run: Any,
seed: str,
text: dict[str, str],
budget: int | None,
) -> tuple[str, int]:
    """The ``verifier`` agent in a fresh read-only chat on ``open_session()``; its answer must carry
    the review JSON. Returns (the answer, the estimated tokens it used)."""
    from critique_bot.engine.chat import _Chat
    from critique_bot.engine.contracts import AnswerContract
    from critique_bot.engine.task import _TaskRun
    from critique_bot.extensions import agent_files
    from critique_bot.prompt_build import without_ready
    from critique_bot.tool_api import TaskState

    agent = agent_files.find_agent("verifier", request.workspace)
    made = open_session()
    enter = getattr(made, "__enter__", None)
    session = enter() if callable(enter) else made
    try:
        counted = _BudgetedSession(session, budget)
        chat = _Chat(counted, seed=seed, quiet=True)
        chat.prefix = without_ready(seed) if seed else ""
        instructions = (f"AGENT: {agent.name}. {agent.description}\n{agent.body}\n\n" if agent is not None else "")
        message = instructions + fill(text.get("REVIEW_TASK", ""), task=request.task.strip(),
                                               diff=request.diff or "(no diff)", evidence=request.evidence)
        contract = AnswerContract(accepts=lambda reply: parse_review(reply) is not None,
                                  nudge=text.get("REVIEW_NUDGE", ""), last_round=text.get("REVIEW_NUDGE", ""),
                                  failure="the reviewer never answered in the review format")
        turns: list[dict[str, str]] = []
        ctx = dataclasses.replace(run.ctx, state=TaskState(task="review"), checkpoints=None, delegate=None)
        review_run = _TaskRun(chat, message, ctx=ctx, sections=run.sections, turns=turns, show=lambda _t: None,
                              max_rounds=(agent.max_steps if agent is not None and agent.max_steps else REVIEW_MAX_ROUNDS),
                              max_result_chars=run.max_result_chars, check_command=None, approve_mode="plan",
                              hooks=_ReviewHooks(run.hooks), read_only=True, answer=contract)
        review_run.plan_done = True
        review_run.reviewed = True
        code = review_run.run(message)
        for turn in reversed(turns):
            if turn.get("role") == "assistant" and parse_review(turn.get("content") or "") is not None:
                return str(turn.get("content") or ""), counted.tokens
        raise RuntimeError(f"the reviewer ended with {code} and no review" +
                           (" (over its token budget)" if counted.over else ""))
    finally:
        leave = getattr(made, "__exit__", None)
        if callable(leave):
            leave(None, None, None)


class _ReviewHooks:
    """The reviewer's progress as one note per tool round; its words stay out of the main chat."""

    def __init__(self, outer: Any) -> None:
        self.outer = outer

    def ui(self, kind: str, message: str) -> None:
        if kind == "bad":
            self.outer.ui("note", "reviewer: " + message)

    def tool_start(self, call: Any) -> None:
        return None

    def tool_done(self, call: Any, result: dict[str, Any]) -> None:
        return None

    def status(self, code: str) -> None:
        return None


class _BudgetedSession:
    """A model session that refuses to send once its estimated tokens pass the budget."""

    def __init__(self, inner: Any, budget: int | None) -> None:
        self.inner = inner
        self.budget = budget
        self.tokens = 0
        self.over = False

    def send(self, prompt: str, *args: Any, **kwargs: Any) -> str:
        if self.budget is not None and self.tokens > self.budget:
            # Not an error (a chat error is retried with waits): a reply the contract refuses, so the review ends.
            self.over = True
            return "The review stopped: it used its token budget."
        reply = self.inner.send(prompt, *args, **kwargs)
        self.tokens += (len(prompt or "") + len(str(reply or ""))) // 4
        return reply

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def findings_text(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items[:10]:
        where = f"{item.get('path')}:{item.get('line')}" if item.get("path") else "(no file)"
        rows.append(f"- [{item.get('severity')}] {where}: {_one_line(str(item.get('body') or ''), 400)}")
    return "\n".join(rows)
