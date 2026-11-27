"""``crit review``: the review as a read-only agent task.

The one-shot review (``crit review --quick``, and the CI worker) pastes the patch and
the changed files into one message. Here the Code Reviewer gets the patch and the
review format, then reads what it needs (callers, tests, interfaces, manifests) with
read-only tools before it reports. The final reply is the same summary plus JSON
fence, so ``review.md`` / ``review.json`` and ``gitlab-post`` work unchanged.
"""

from __future__ import annotations

import re
from typing import Iterable

from critique_bot.engine import AnswerContract, hide_tool_markup
from critique_bot.config import compose_prompt
from critique_bot.review_comments import has_review_json

#: Tool rounds a review may use before it must report.
DEFAULT_MAX_ROUNDS = 20
#: Replies without the review that are sent back before the review fails.
MAX_NUDGES = 2

LEAD = """REVIEW TASK (read-only)
Review the change below in this project folder, as the reviewer described after this block.
Before you report a finding, read what you need to be sure of it: the changed files as they
are now, their callers, the tests, the interfaces and base classes they touch, manifests and
permission XML. Use read_files, search_code, find_files, list_files, git_log, git_show, and
git_diff. Ask for several reads in one reply. Edits, commands, web fetches, helper chats, and
paths outside the project are off for this task, so do not ask for them.
You have at most {rounds} tool rounds.
When you are done, reply with the review only: the summary and the ```json fence in the OUTPUT
format below, with no tool calls and no COMPLETED line. `line` numbers are line numbers in the
files as they are now (side "new"), which is what read_files shows.

Changed files in this patch:
{changed}
"""

FILES_NOTE = (
    "Not pasted here. Read the changed files listed at the top with read_files, "
    "and any other file the review needs."
)

NUDGE = (
    "That reply had no review in it. Reply now with the review only: the risk line, the short "
    "summary, and the ```json fence with \"risk\" and \"comments\", in the OUTPUT format of the "
    "task. No tool calls."
)

LAST_ROUND = (
    "This is the last round: no more tools after this reply. Reply with the review now, in the "
    "OUTPUT format of the task."
)

_STATUS_LINE_RE = re.compile(r"^\s*\**\s*(COMPLETED|FINISHED|DONE)\s*\**\s*\.?\s*$", re.M)


def build_task(
    template: str,
    patch_body: str,
    *,
    changed_paths: Iterable[str],
    mr_context: str = "",
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> str:
    """The first message of a review: the read-only lead, then the review template with the patch."""
    paths = [path for path in changed_paths if path]
    changed = "\n".join(f"- {path}" for path in paths) or "- (none found in the patch)"
    lead = LEAD.format(rounds=max_rounds, changed=changed)
    return lead + "\n" + compose_prompt(template, patch_body, mr_context, files=FILES_NOTE)


def is_review(reply: str) -> bool:
    """True when the reply holds the review JSON (``risk`` / ``comments``)."""
    return bool(reply and reply.strip()) and has_review_json(reply)


#: The engine's contract for a review: the task ends on a reply with the review JSON.
CONTRACT = AnswerContract(
    accepts=lambda reply: is_review(reply),
    nudge=NUDGE,
    last_round=LAST_ROUND,
    max_nudges=MAX_NUDGES,
    failure="the review never came back in the review format",
    note="Asking for the review in the review format.",
)


def review_text(turns: list[dict[str, str]]) -> str:
    """The review from a finished review task: the last assistant reply that holds the review JSON.

    Tool markup and a trailing finish word are dropped. "" when no reply held a review.
    """
    for turn in reversed(turns):
        if turn.get("role") != "assistant":
            continue
        content = str(turn.get("content") or "")
        if not is_review(content):
            continue
        cleaned = _STATUS_LINE_RE.sub("", hide_tool_markup(content))
        return cleaned.strip() + "\n"
    return ""
