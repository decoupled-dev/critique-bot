# SPDX-License-Identifier: Apache-2.0
"""Task classes (plan Stage 7): bugfix, feature, refactor, question, ops.

Rules decide first (keyword scores); one short model call decides only when the rules are
unclear, and only when a classifier is given (``Classifier``: a callable from the task to
one of the class names). Tests script it; crit's sessions use :func:`model_classifier` on
a side chat when the model is behind an API.

The class picks the flow (:func:`flow_for`): bugfix gets the whole verified flow
(reproduce, fix, test, review); feature and refactor get test and review without the
reproduce step; question and ops run plain. ``--flow``, the agent file's ``flow:`` and the
``flow`` setting override it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from critique_bot import log
from critique_bot.engine.replies import _answer_only

KINDS = ("bugfix", "feature", "refactor", "question", "ops")
#: What a session can ask for: verified (always, for change tasks), plain (never), auto (by class).
FLOWS = ("verified", "plain", "auto")

#: A callable that returns one of KINDS for a task (or anything else for "unclear").
Classifier = Callable[[str], str]

_RULES: dict[str, re.Pattern[str]] = {
    "bugfix": re.compile(
        r"\b(fix(?:es|ed|ing)?|bugs?|crash(?:es|ed|ing)?|exceptions?|errors?|traceback|stack ?trace|fail(?:s|ed|ing|ure)?"
        r"|broken|regression|wrong|incorrect|doesn'?t work|does not work|not working|npe|nullpointer|leaks?|hangs?"
        r"|anr|off[- ]by[- ]one|returns? \w+ instead)\b",
        re.I,
    ),
    "feature": re.compile(
        r"\b(add|implement|support|introduce|create|new (?:option|flag|command|endpoint|field|feature)|feature"
        r"|allow|enable|let (?:users?|it)|expose)\b",
        re.I,
    ),
    "refactor": re.compile(
        r"\b(refactor(?:ing)?|rename|extract|restructure|reorgani[sz]e|clean ?up|split \w+ into|inline|deduplicate"
        r"|migrat(?:e|ion)|move \w+ (?:to|into))\b",
        re.I,
    ),
    "ops": re.compile(
        r"\b(build|compile|rebuild|gradle|soong|git (?:commit|push|rebase|merge|cherry-pick|bisect|log|blame)"
        r"|commit|rebase|cherry-pick|bisect|deploy|flash|logcat|tombstone|ci pipeline"
        r"|run the (?:tests?|build|suite))\b",
        re.I,
    ),
}

#: Words that make a "build"/"logs" task a fix after all ("fix the build" stays ops, below).
_OPS_FIX = re.compile(r"\b(build|compile|compil\w+)\b.*\b(fail|error|broken|break)", re.I)


@dataclass(frozen=True)
class TaskClass:
    kind: str
    #: "rules", "model", or "unclear" (neither decided; ``kind`` is then "feature").
    source: str
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "source": self.source, "reason": self.reason}


def rule_scores(task: str) -> dict[str, int]:
    """How many keyword hits each class gets."""
    text = " ".join(str(task or "").split())
    return {kind: len(pattern.findall(text)) for kind, pattern in _RULES.items()}


def classify(task: str, classifier: Classifier | None = None) -> TaskClass:
    """The class of ``task``: rules first, then ``classifier`` when the rules are unclear."""
    text = " ".join(str(task or "").split())
    if not text:
        return TaskClass("question", "rules", "empty task")
    if _answer_only(text):
        return TaskClass("question", "rules", "a question with no change asked for")
    scores = rule_scores(text)
    if _OPS_FIX.search(text):
        return TaskClass("ops", "rules", "a build that fails")
    ranked = sorted(scores.items(), key=lambda item: -item[1])
    best, top = ranked[0]
    runner_up = ranked[1][1]
    if top and top > runner_up:
        return TaskClass(best, "rules", f"{top} {best} keyword(s)")
    if top and best in {"bugfix", "refactor"} and runner_up == top:
        # "fix X and add Y" is a fix first; "refactor and add" is a refactor.
        tied = [kind for kind, score in ranked if score == top]
        for kind in ("bugfix", "refactor", "feature", "ops"):
            if kind in tied:
                return TaskClass(kind, "rules", f"tie broken toward {kind}")
    if classifier is not None:
        try:
            answer = str(classifier(text) or "").strip().lower()
        except Exception as exc:  # noqa: BLE001 - a broken classifier means "unclear", never a failed task
            log.debug(f"task classifier failed: {exc}")
            answer = ""
        found = _kind_in(answer)
        if found:
            return TaskClass(found, "model", "the model's answer")
    return TaskClass("feature", "unclear", "no rule decided")


def _kind_in(answer: str) -> str:
    for kind in KINDS:
        if re.search(rf"\b{kind}\b", answer):
            return kind
    return ""


def normalize_flow(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in {"verify", "verified", "on", "true"}:
        return "verified"
    if value in {"plain", "off", "false", "none"}:
        return "plain"
    if value == "auto":
        return "auto"
    return ""


@dataclass(frozen=True)
class FlowChoice:
    """What runs for one task: the flow (verified or plain), with or without the repro step."""

    flow: str
    repro: bool
    task_class: TaskClass
    #: Where the flow setting came from: flag, agent, settings, default.
    source: str

    @property
    def verified(self) -> bool:
        return self.flow == "verified"


def flow_for(task_class: TaskClass, setting: str, *, source: str = "settings") -> FlowChoice:
    """The flow for a class under a setting (verified, plain, auto)."""
    setting = normalize_flow(setting) or "plain"
    kind = task_class.kind
    if setting == "plain" or kind in {"question"}:
        return FlowChoice("plain", False, task_class, source)
    if setting == "auto":
        if kind in {"ops"} or task_class.source == "unclear":
            return FlowChoice("plain", False, task_class, source)
    return FlowChoice("verified", kind == "bugfix", task_class, source)


CLASSIFY_PROMPT = (
    "Classify this coding task with one word: bugfix (something behaves wrongly and must be fixed), feature "
    "(new behavior), refactor (same behavior, different code), question (an answer, no change), or ops (build, "
    "git, logs, deploy). Reply with the one word only.\n\nTask:\n{task}"
)


def model_classifier(open_session: Callable[[], Any]) -> Classifier:
    """A classifier that asks a fresh session on the same model one short question."""

    def ask(task: str) -> str:
        made = open_session()
        enter = getattr(made, "__enter__", None)
        session = enter() if callable(enter) else made
        try:
            return str(session.send(CLASSIFY_PROMPT.format(task=task[:2_000])) or "")[:200]
        finally:
            leave = getattr(made, "__exit__", None)
            if callable(leave):
                leave(None, None, None)

    return ask
