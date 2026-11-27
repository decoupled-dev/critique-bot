"""Local coding agent driven through a web chat page.

The chat model prints ``<tool_call>`` blocks. This module parses them, runs
the tool from :mod:`critique_bot.agent_tools`, and sends ``<tool_result>``
blocks back on the same chat, each followed by a short STATE block so the
task stays in view however long the conversation gets. Model-facing text
lives in ``prompts/agent.txt``.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import os
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from critique_bot import agent_build, agent_edit, agent_shell, code_index, config_prompts, egress, integrations, log, roles
from critique_bot import taint
from critique_bot import run_stats
from critique_bot import session_log as session_log_mod
from critique_bot import prompt_build
from critique_bot.prompt_build import READY_LINE, seed_message
from critique_bot.prompt_build import without_ready as _without_ready
from critique_bot.config import effective_theme
from critique_bot import tool_api
from critique_bot import tool_api as agent_tools
from critique_bot.tool_api import (
    ALLOWED_TOOLS,
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_TOOL_CHARS,
    MUTATING,
    TaskState,
    ToolContext,
    canonical_tool,
    fill,
    is_git_repo,
    normalize_args,
)
from critique_bot.tool_api import execute as _execute
from critique_bot.project_home import BotHome, resolve_check_command
from critique_bot import model_api, ui_api
from critique_bot.engine import runtime, ui
from critique_bot.model_api import COMPLETION_IDLE, ChatError, CloudflareBlocked, ReplyStalled
from critique_bot.config import BotConfig
from critique_bot.output import isoformat, write_output

from critique_bot.engine.parse import (  # noqa: F401
    ToolCall,
    _ARG_KEYS,
    _ATTR_RE,
    _BARE_RESULT_RE,
    _BLOCK_RE,
    _CALL_TAGS,
    _CHROME_LINES,
    _CLOSED_TOOL_MARKUP_RE,
    _FENCED_JSON_RE,
    _FENCE_RE,
    _INNER_ARGS_RE,
    _INNER_NAME_RE,
    _LIST_KEYS,
    _NAME_KEYS,
    _OPEN_RE,
    _QUOTED_CONTEXT_RE,
    _RESULT_OPEN_RE,
    _STATUS_OK,
    _STATUS_WORDS,
    _TAIL_PATH_RE,
    _TAIL_TOOL_RE,
    _TOOL_TAG_RE,
    _TRAILING_PROSE_RE,
    _UNCLOSED_TOOL_RE,
    _WRAPPER_TYPES,
    _WRITE_CALL_RE,
    _WRITE_JSON_HINT,
    _answer_text,
    _bare_json_calls,
    _bare_tool_blobs,
    _call_from_data,
    _calls_from_data,
    _cut_call_label,
    _cut_call_name,
    _cut_fabricated,
    _drop_trailing_prose,
    _hide_tool_markup,
    _json_error,
    _looks_quoted,
    _openai_reply,
    _parse_block,
    _parse_tagged,
    _strip_chrome,
    _whole_tail_calls,
    commentary,
    format_tool_result,
    hide_tool_markup,
    parse_tool_calls,
)
from critique_bot.engine.json_repair import (  # noqa: F401
    _BARE_TOOL_KEY_RE,
    _JSON_STRING_ESCAPES,
    _PATH_LEAD,
    _RAW_WIN_PATH_RE,
    _TRAILING_COMMA_RE,
    _closer_is_json,
    _json_objects,
    _json_value_at,
    _key_follows,
    _loads_lenient,
    _normalize_json_text,
    _raw_path_start,
    _read_json_string,
    _repair_json,
    _string_ends,
)
from critique_bot.engine.replies import (  # noqa: F401
    MAX_HAND_BACKS,
    _ANSWER_TASK_RE,
    _EDIT_TASK_RE,
    _FAIL_HEAD,
    _HAND_BACK_RE,
    _PERMISSION_MARKERS,
    _PLAN_RE,
    _PROMISE_MARKERS,
    _PROMISE_START_RE,
    _PROVIDER_ERROR_MAX,
    _PROVIDER_ERROR_RE,
    _QUESTION_MARKERS,
    _REFUSAL_MARKERS,
    _TEST_MARKERS,
    _UNTAGGED_PLAN_RE,
    _answer_only,
    _asks_for_change,
    _asks_user,
    _call_key,
    _diffstat,
    _failure_reason,
    _hands_back,
    _has_checks,
    _idle_tool_reply,
    _keywords,
    _no_edit_needed,
    _normalized,
    _only_promises,
    _plan_text,
    _provider_error,
    _real_question,
    _refuses,
    _stalls,
    _status_code,
)
from critique_bot.engine.text import (  # noqa: F401
    _cap,
    _one_line,
)

from critique_bot.engine.prompts import (  # noqa: F401
    DEFAULT_TOOL_FORMAT,
    MAX_SKILL_CHARS,
    TOOL_FORMATS,
    TOOL_FORMAT_NATIVE,
    TOOL_FORMAT_OPENAI,
    TOOL_FORMAT_TAGS,
    _FALLBACKS,
    _RESPONSES_WIRE,
    _SECTION_RE,
    _agent_prompt_path,
    _file_agent,
    _read_sections,
    _setting_list,
    _web_hosts,
    load_agent_instructions,
    load_prompt_sections,
    load_role_instructions,
    skills_block,
    task_message,
    tool_format_of,
)

from critique_bot.engine.ui import (  # noqa: F401
    _REVIEW_LINES,
    _activity,
    _approve_prompt,
    _ask_user,
    _call_paths,
    _emit,
    _friendly_error,
    _paint,
    _present_text,
    _print_status,
    _read_message,
    _review_plan,
    _set_active,
    _show_diff_in_ui,
    _tool_done,
    _tool_output,
    _tool_start,
    _ui,
    _ui_approve_mode,
    _ui_result,
    _waiting,
    current,
    use_thread_ui,
    use_ui,
)

__all__ = [
    "ALLOWED_TOOLS",
    "ToolCall",
    "command_argv",
    "execute_tool",
    "format_tool_result",
    "parse_tool_calls",
    "AnswerContract",
    "READ_ONLY_TOOLS",
    "hide_tool_markup",
    "run_agent",
    "run_agent_loop",
    "run_read_only_task",
]

_QUIT = {"exit", "quit", "/exit", "/quit", "/q"}
#: Helper chat tabs a task can be split across (the delegate tool). 0 turns it off.
DEFAULT_HELPER_SESSIONS = 2
MAX_HELPER_SESSIONS = 4
MAX_PLAN_REVISIONS = 5
MAX_REFUSALS = 3
MAX_FAILED_ROUNDS = 8
#: Replies in a row whose every call the user denied end the task as BLOCKED.
MAX_DENIED_ROUNDS = 3
MAX_TRUNCATIONS = 4
MAX_CHECK_CYCLES = 2
MAX_USER_QUESTIONS = 2
#: An identical read-only call at the same disk version is not run a third time.
MAX_SAME_CALL = 2
#: The same set of calls in this many replies in a row is a loop.
LOOP_REPLIES = 3
#: Protocol violations in a row that make the chat start over.
AMNESIA_VIOLATIONS = 2
#: A chat whose replies keep going wrong is left even when the violations are not in a row:
#: this many bad replies (cut off, made up, broken format, handed back, retried) among the
#: last HEALTH_WINDOW, and the next message goes to a new chat.
HEALTH_WINDOW = 6
HEALTH_BAD_REPLIES = 3
#: Cut-off replies in one chat before the next request for the rest goes to a new chat.
CUT_REPLIES_PER_CHAT = 2
DEFAULT_CHECK_TIMEOUT = 600
#: A web chat gets slow and forgetful well before its context is full. These are the
#: sizes at which crit moves to a new chat in the middle of a task; between tasks it
#: moves at half of each.
DEFAULT_COMPACT_AFTER_CHARS = 120_000
DEFAULT_COMPACT_AFTER_TURNS = 30
DEFAULT_COMPACT_AFTER_MINUTES = 60
#: Share of the sizes above at which a new task starts in a new chat (cheap: only the task list is needed).
BOUNDARY_ROTATION_SHARE = 0.5
#: Characters per tool-result message unless "max_result_chars" says otherwise.
DEFAULT_RESULT_CHARS = 20_000
#: After this many failed tries in one chat, the next try goes to a new chat. A reply that
#: started and then stalled leaves a half-written answer behind, so that one moves at once.
RETRIES_BEFORE_NEW_CHAT = 1
DEFAULT_REPLY_RETRIES = 3
#: Seconds between resends when the chat page shows an error instead of a reply.
RETRY_DELAYS = (5.0, 15.0, 45.0)
PARALLEL_WORKERS = 4
REPO_MAP_CHARS = 2_500
#: Read-only tools that may run side by side within one reply.
_PARALLEL_SAFE = frozenset(
    {"list_files", "find_files", "read_files", "search_code", "git_status", "git_diff", "git_log", "git_show"}
)
_READ_ONLY_FALLBACK = frozenset(
    _PARALLEL_SAFE | {"skill", "todo", "command_output", "ask_user", "web_fetch"}
)
#: Tools whose result changes over time, so repeating them is not a loop.
_POLLING = frozenset({"command_output", "ask_user"})

# Overridable in tests so a retry does not really sleep.
_sleep = time.sleep


def command_argv(command: str, *, platform_name: str | None = None, cwd: Path | None = None) -> list[str]:
    """Build the argv for ``run_command``. The command is one argument."""
    return agent_shell.command_argv(command, platform_name=platform_name, cwd=cwd)


def execute_tool(
    name: str,
    arguments: dict[str, Any] | None,
    *,
    workspace: Path,
    index_path: Path | None = None,
    cache_dir: Path | None = None,
    max_chars: int = DEFAULT_TOOL_CHARS,
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    runner: Callable[..., Any] | None = None,
    state: TaskState | None = None,
) -> dict[str, Any]:
    ctx = ToolContext(
        workspace=Path(workspace),
        index_path=index_path,
        cache_dir=cache_dir,
        max_chars=max_chars,
        command_timeout=command_timeout,
        runner=runner,
        state=state,
    )
    return _execute(name, arguments, ctx)


# --------------------------------------------------------------------------- parsing


# --------------------------------------------------------------------------- reply classification


# --------------------------------------------------------------------------- prompt file




# --------------------------------------------------------------------------- the loop


@dataclass
class _Perm:
    """Stand-in for agent_tools.Permission when that is not available."""

    kind: str = "read"
    summary: str = ""
    key: str = ""
    detail: str = ""


class _ProviderFailed(RuntimeError):
    """The chat page kept showing an error instead of a reply."""


def _tool_context(**values: Any) -> ToolContext:
    """A ToolContext with only the fields this version of agent_tools has."""
    names = {item.name for item in dataclasses.fields(ToolContext)}
    return ToolContext(**{key: value for key, value in values.items() if key in names})


def _int_setting(settings: dict[str, Any], key: str, default: int, minimum: int) -> int:
    raw = settings.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < minimum:
        return default
    return int(raw)


_MODES = frozenset({"ask", "edits", "auto", "plan"})
#: What a mode change note tells the model (prompt_build.change_note).
_MODE_NOTES = {
    "ask": "ask. Edits and commands wait for the user's approval.",
    "edits": "edits. File edits in the workspace run without asking; commands still ask.",
    "auto": "auto. Edits and commands run without asking; risky commands still ask.",
    "plan": "plan. Read only now, then reply with a plan; nothing changes until the user approves it.",
}


def _read_only_tools() -> frozenset[str]:
    return getattr(agent_tools, "READ_ONLY", None) or _READ_ONLY_FALLBACK


#: What a read-only task may run: reads in the project, and nothing that waits on a person or a command.
READ_ONLY_TOOLS = frozenset(_PARALLEL_SAFE | {"todo", "skill"})


def _read_only_allows(call: ToolCall, perm: Any) -> bool:
    """A read-only task reads the project and git history only. Input under review (a patch, an
    issue) must not be able to make crit edit, run, or fetch anything, or read outside the project."""
    kind = getattr(perm, "kind", "read") or "read"
    return kind == "read" and canonical_tool(call.tool) in READ_ONLY_TOOLS


@dataclass(frozen=True)
class AnswerContract:
    """What the final answer of a task must look like, for callers that parse it (a review).

    A reply without tool calls ends the task when ``accepts(reply)`` is true. Otherwise
    ``nudge`` is sent back, up to ``max_nudges`` times, and then the task fails with
    ``failure``. ``last_round`` (when set) goes with the last allowed tool round.
    """

    accepts: Callable[[str], bool]
    nudge: str
    last_round: str = ""
    max_nudges: int = 2
    failure: str = "the final answer never came in the required format"
    note: str = "Asking for the answer in the required format."


def _stop_reply(session: Any) -> None:
    """Ask the chat page to stop a reply it is still writing. Best effort."""
    stop = getattr(session, "stop_generation", None)
    if not callable(stop):
        return
    try:
        stop()
    except Exception as exc:  # the next send waits for (or stops) it again
        log.debug(f"stopping the reply failed: {exc}")


class _Chat:
    """The open conversation: sends with retries, tracks its size, starts over when asked.

    A web chat slows down and forgets the protocol as it grows. Past
    ``compact_after`` characters, or after the model breaks the protocol
    ``AMNESIA_VIOLATIONS`` times in a row, the next message goes to a new
    chat with the instructions and a summary in front of it.
    """

    def __init__(
        self,
        session: Any,
        *,
        seed: str = "",
        retries: int = DEFAULT_REPLY_RETRIES,
        compact_after: int = DEFAULT_COMPACT_AFTER_CHARS,
        quiet: bool = False,
        compact_turns: int = DEFAULT_COMPACT_AFTER_TURNS,
        compact_minutes: int = DEFAULT_COMPACT_AFTER_MINUTES,
    ) -> None:
        self.session = session
        native = runtime.native_tools()
        if native is not None and callable(getattr(session, "use_tools", None)):
            session.use_tools(native)
        #: A helper tab's chat: no spinner and no notes in the terminal.
        self.quiet = quiet
        self.seed = (seed or "").strip()
        self.retries = max(0, retries)
        #: Where a failed send saves the page's screenshot and HTML before the retry (None: nowhere).
        self.failure_dir: Path | None = None
        self.compact_after = compact_after
        self.chars = 0
        self.violations = 0
        #: The last HEALTH_WINDOW replies, True for a bad one; and cut-off replies in this chat.
        self.health: deque[bool] = deque(maxlen=HEALTH_WINDOW)
        self.cut_replies = 0
        self.compact_turns = max(1, int(compact_turns or DEFAULT_COMPACT_AFTER_TURNS))
        self.compact_seconds = max(60, int(compact_minutes or DEFAULT_COMPACT_AFTER_MINUTES) * 60)
        #: Messages answered in this chat, and when it started.
        self.sends = 0
        self.born = time.monotonic()
        #: Set by the running task: what a new chat needs to carry on (see _TaskRun._resume_text).
        self.resume: Callable[[], str] | None = None
        self.map_sent = False
        #: Skills already sent in this chat; a new chat sends them again.
        self.skills_sent: set[str] = set()
        #: Sent ahead of the next message: the instructions, in a fresh chat.
        self.prefix = ""
        self.can_restart = callable(getattr(session, "new_chat", None))
        #: "task -> outcome" for each task in this session, for the summary.
        self.history: list[str] = []
        #: The session's event log (session_log.SessionLog); NullLog when there is none.
        self.log: Any = session_log_mod.NullLog()
        #: Model API sessions: compaction by context pressure (compaction.Compactor). The web
        #: chat has None and moves to a new chat by size, turns, and time instead.
        self.compactor: Any = None
        #: Opens a second session on the same model (one-off helper requests: the summary).
        self.side_factory: Callable[[], Any] | None = None
        #: Replies received in this session, across new chats (the session log's turn).
        self.replies = 0
        #: Notes for the next message: a mid-session change (role) on an append-only conversation.
        self.pending_notes: list[str] = []

    @property
    def last_detail(self) -> Any:
        return getattr(self.session, "last_detail", None)

    def send(self, payload: str) -> str:
        """Send and return the reply, retrying when the page fails or never answers.

        The first retry goes to the same chat. After that the conversation
        itself may be broken, so the next try opens a new chat with the
        instructions and the running task's summary in front of the message.
        """
        attempt = 0
        moved = False
        # A retry in the same chat resends exactly the same text, instructions included:
        # a message that never went out must not lose them.
        prefix, self.prefix = self.prefix, ""
        while True:
            outgoing = prefix + "\n\n" + payload if prefix else payload
            outgoing, hidden = agent_tools.redact_secrets(outgoing)
            if hidden:
                self._note("note", f"Kept {hidden} secret{'s' if hidden != 1 else ''} out of the message to the chat.")
            if not self.quiet:
                _waiting(True)
            stalled = False
            mark = self.log.request(self.session, outgoing, attempt=attempt or None)
            try:
                reply = self.session.send(outgoing)
                problem = _provider_error(reply)
            except CloudflareBlocked:
                raise  # only a visible window gets past it; the session reopens one
            except ChatError as exc:
                reply, problem = "", f"the chat page failed: {_one_line(str(exc), 160)}"
                self.log.write("error", message=problem, attempt=attempt)
                stalled = isinstance(exc, ReplyStalled)
                if self.failure_dir is not None:
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    folder = self.failure_dir / f"{stamp}-attempt{attempt + 1}"
                    model_api.save_failure(self.session, folder)
                    log.info(f"saved what the chat page showed to {folder}")
            finally:
                if not self.quiet:
                    _waiting(False)
            self.chars += len(outgoing) + len(reply or "")
            if problem is None:
                self.sends += 1
                self.replies += 1
                self.log.reply(self.session, mark, reply)
                return reply
            self.note_reply(bad=True)
            if attempt >= self.retries:
                raise _ProviderFailed(problem)
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            attempt += 1
            # A timed-out reply may still be streaming; resending over it
            # duplicates the message and crosses the two answers.
            _stop_reply(self.session)
            # A stalled reply left a half-written answer in the chat, and an unhealthy chat
            # has already shown it keeps going wrong: neither is worth another try in place.
            move_now = stalled or self.unhealthy()
            if (attempt > RETRIES_BEFORE_NEW_CHAT or move_now) and not moved and self.can_restart:
                if self.restart("the chat stopped answering"):
                    moved = True
                    prefix, self.prefix = self.prefix, ""
                    summary = ""
                    if callable(self.resume):
                        try:
                            summary = self.resume() or ""
                        except Exception as exc:  # the retry still goes out without the summary
                            log.debug(f"could not build the summary for the new chat: {exc}")
                    if summary:
                        payload = summary + "\n\n" + payload
                    self._note("note", f"{problem[:1].upper()}{problem[1:]}. Trying again in a new chat ({attempt}/{self.retries}).")
                    _sleep(min(delay, 5.0))
                    continue
            self._note("note", f"{problem[:1].upper()}{problem[1:]}. Sending again in {delay:.0f}s ({attempt}/{self.retries}).")
            _sleep(delay)

    def _note(self, kind: str, message: str) -> None:
        if not self.quiet:
            _ui(kind, message)

    def too_long(self) -> bool:
        return self.can_restart and bool(self.rotation_reason(size_only=True))

    def stale_for_next_task(self) -> bool:
        """Past half its size: a new task is better off in a new chat (only the task list is needed)."""
        return self.can_restart and bool(self.rotation_reason(size_only=True, share=BOUNDARY_ROTATION_SHARE))

    def note_reply(self, *, bad: bool) -> None:
        """Remember whether the latest reply (or try) went wrong, for :meth:`unhealthy`."""
        self.health.append(bad)

    def strike(self) -> None:
        """A protocol violation: counts toward amnesia (in a row) and toward the chat's health."""
        self.violations += 1
        self.note_reply(bad=True)

    def good(self) -> None:
        """A reply that followed the protocol: the in-a-row count starts over."""
        self.violations = 0
        self.note_reply(bad=False)

    def note_cut(self) -> None:
        """A reply that was cut off before it finished."""
        self.cut_replies += 1
        self.note_reply(bad=True)

    def unhealthy(self) -> bool:
        """Bad replies keep coming, even with good ones between them."""
        return sum(1 for bad in self.health if bad) >= HEALTH_BAD_REPLIES

    def rotation_reason(self, *, size_only: bool = False, share: float = 1.0) -> str:
        """Why this chat should move to a new one now, or "". ``share`` scales the size limits.

        With a compactor (a model API session) size, turns, and time never move the chat:
        context pressure compacts it in place instead.
        """
        if self.compactor is not None:
            if size_only:
                return ""
        elif self.chars > self.compact_after * share:
            return "the chat grew long"
        elif self.sends >= max(1, int(self.compact_turns * share)):
            return f"the chat reached {self.sends} messages"
        elif self.sends and (time.monotonic() - self.born) >= self.compact_seconds * share:
            return f"the chat has been open {(time.monotonic() - self.born) / 60:.0f} minutes"
        if size_only:
            return ""
        if self.violations >= AMNESIA_VIOLATIONS:
            return "the model lost track of the instructions"
        if self.cut_replies >= CUT_REPLIES_PER_CHAT:
            return "replies keep getting cut off in this chat"
        if self.unhealthy():
            return "this chat keeps giving broken replies"
        return ""

    def wants_restart(self) -> bool:
        return self.can_restart and bool(self.rotation_reason())

    def restart(self, reason: str) -> bool:
        """Open a new conversation. The instructions go in front of the next message."""
        if not self.can_restart:
            return False
        try:
            started = self.session.new_chat()
        except Exception as exc:  # the old chat still works; keep using it
            log.warn(f"could not open a new chat: {exc}")
            started = False
        if started is False:
            self.can_restart = False
            return False
        self._note("note", f"Starting a new conversation: {reason}.")
        run_stats.count("compactions")
        self.log.write("compaction", strategy="new_chat", reason=reason)
        self.log.sync()
        self.chars = 0
        self.violations = 0
        self.health.clear()
        self.cut_replies = 0
        self.sends = 0
        self.born = time.monotonic()
        self.map_sent = False
        self.skills_sent = set()
        self.prefix = _without_ready(self.seed) if self.seed else ""
        if self.history:
            earlier = "EARLIER IN THIS SESSION (tasks done in the previous chat, newest last):\n- " + "\n- ".join(
                self.history[-8:]
            )
            self.prefix = (self.prefix + "\n\n" + earlier).strip()
        return True


class _Hooks:
    """Where a task run reports to. The terminal by default; helper tabs pass quiet ones."""

    def ui(self, kind: str, message: str) -> None:
        _ui(kind, message)

    def tool_start(self, call: ToolCall) -> None:
        _tool_start(call)

    def tool_done(self, call: ToolCall, result: dict[str, Any]) -> None:
        _tool_done(call, result)

    def status(self, code: str) -> None:
        run_stats.set_value("exit_reason", code)
        _print_status(code)


class _TaskRun:
    """One task on an open chat: send, parse, run tools, repeat until a finish."""

    def __init__(
        self,
        chat: _Chat,
        task: str,
        *,
        ctx: ToolContext,
        sections: dict[str, str],
        turns: list[dict[str, str]],
        show: Callable[[str], None],
        max_rounds: int | None,
        max_result_chars: int,
        check_command: str | None,
        check_timeout: float = DEFAULT_CHECK_TIMEOUT,
        approve_mode: str = "ask",
        always: set[str] | None = None,
        ask: Callable[[str], str | None] | None = None,
        ui_mode_at_start: str | None = None,
        confirm_risky: bool = True,
        hooks: "_Hooks | None" = None,
        read_only: bool = False,
        answer: AnswerContract | None = None,
    ) -> None:
        self.hooks = hooks or _Hooks()
        #: Only reads run (READ_ONLY_TOOLS); nothing is asked of the user.
        self.read_only = read_only
        #: The final answer must pass this contract (None: the usual finish rules).
        self.answer = answer
        self.answer_nudges = 0
        self.chat = chat
        self.task = task
        self.ctx = ctx
        self.state = ctx.state or TaskState(task=task)
        self.sections = sections
        #: How tool calls look in this chat: "tags" (<tool_call>) or "openai" (Responses items).
        self.tool_format = sections.get("_FORMAT", TOOL_FORMAT_TAGS)
        self.turns = turns
        self.show = show
        self.max_rounds = max_rounds
        self.max_result_chars = max_result_chars
        self.check_command = (check_command or "").strip() or None
        self.check_timeout = check_timeout
        self.approve_mode = approve_mode
        self.ui_mode_at_start = ui_mode_at_start if ui_mode_at_start is not None else _ui_approve_mode()
        self.always = always if always is not None else set()
        self.ask = ask
        self.confirm_risky = confirm_risky
        #: Plan mode: the plan was approved (or the task was not planned), so work goes ahead.
        self.plan_done = False
        self.plan_asks = 0
        self.plans = 0
        self.hand_backs = 0
        #: Ask the old chat for a handoff note before moving to a new one.
        self.handoff = True
        self.tools_ran = False
        self.last_failed = False
        self.nudges = 0
        self.refusals = 0
        self.recoveries = 0
        self.truncations = 0
        self.plan_notes = 0
        self.check_cycles = 0
        self.check_passed = False
        self.nothing_nudged = False
        #: Reminded once that jobs were still running at COMPLETED.
        self.jobs_nudged = False
        #: Jobs already running before this task (an earlier task's server): not this task's business.
        self.jobs_before = {row["id"] for row in self._job_rows() if row.get("running")}
        self.reason_asked = False
        self.verify_asked = False
        self.verified = True
        self.questions = 0
        self.reviewed = False
        self.payload = ""
        #: Bumped by each successful change, so a repeat can be told from a retry.
        self.world = 0
        self.history: dict[str, dict[str, Any]] = {}
        self.signatures: list[tuple[str, ...]] = []
        self.findings: list[str] = []
        self.last_output = ""
        #: Calls the user denied in this task (call key -> reason); the same call is not asked twice.
        self.denials: dict[str, str] = {}
        self.denied_rounds = 0
        self.denied_reason = ""
        #: The mode the model was last told about (a switch mid-task is appended as a note).
        self.mode_seen = self._approve_mode()

    def _present(self, text: str) -> None:
        visible = _present_text(text)
        if visible:
            self.show(visible)

    def run(self, first_payload: str) -> str:
        self.payload = first_payload
        self.chat.resume = self._resume_text
        rounds = 0
        while self.max_rounds is None or rounds < self.max_rounds:
            rounds += 1
            if self.chat.compactor is not None:
                _compact_at_boundary(self)
            if self.chat.wants_restart():
                reason = self.chat.rotation_reason() or "the chat grew long"
                # A chat that is merely long still works: ask it for a handoff note first.
                note = self._handoff_note() if self.chat.too_long() and self.handoff else ""
                runtime.hook("pre_compact", {"reason": reason, "task": self.task})
                if self.chat.restart(reason):
                    self.payload = self._resume_text(note) + "\n\n" + self.payload
            self._note_shell_change()
            self._note_mode_change()
            if self.answer and self.answer.last_round and self.max_rounds is not None and rounds == self.max_rounds:
                self.payload = self.payload + "\n\n" + self.answer.last_round
            try:
                reply = self.chat.send(self.payload)
            except _ProviderFailed as exc:
                return self._end("FAILED", str(exc))
            self.turns.append({"role": "assistant", "content": reply})
            code = self._handle(reply)
            if code is not None:
                return code
        return self._end("STOPPED", f"stopped after {self.max_rounds} tool rounds")

    def _handle(self, raw_reply: str) -> str | None:
        detail = self.chat.last_detail
        complete = isinstance(detail, dict) and detail.get("complete") is True
        answer_task = _answer_only(self.task)
        wire = _openai_reply(raw_reply, complete) if self.tool_format in _RESPONSES_WIRE else None
        if wire is not None:
            # Responses items: the calls are explicit, and the words are the message items.
            reply, fabricated, unclosed = wire.text, wire.fabricated, wire.unclosed
            calls = [ToolCall(item.name, item.arguments, item.error, item.call_id) for item in wire.calls]
            note = reply
        else:
            reply, fabricated = _cut_fabricated(raw_reply)
            calls, unclosed = parse_tool_calls(reply, complete=complete)
            note = commentary(reply)
            if calls and not _OPEN_RE.search(reply) and answer_task and len(note) > 80:
                # JSON quoted inside an answer, not a call.
                calls = []
        status = _status_code(reply)
        plan = _plan_text(reply)
        shown = _answer_text(note)
        if note:
            self.findings = (self.findings + [_one_line(note, 300)])[-4:]
        if calls and shown:
            self._present(shown)
        elif shown and not status:
            self._present(plan if plan else shown)
        elif shown and status in _STATUS_OK:
            self._present(shown)
        if fabricated:
            self.chat.strike()
            self.hooks.ui("note", "The reply made up a tool result. Only real results are used.")
        if unclosed or _idle_tool_reply(detail, reply):
            self.truncations += 1
            self.chat.note_cut()
            if self.truncations > MAX_TRUNCATIONS:
                return self._end("FAILED", "the reply was cut off before it finished")
            self.hooks.ui("note", "The reply was cut off. Asking for the rest.")
            dangling = _OPEN_RE.search(_BLOCK_RE.sub("", reply))
            label = _cut_call_label(_BLOCK_RE.sub("", reply)[dangling.end() :]) if dangling else ""
            self._send_message("TRUNCATED", call=label)
            self.last_failed = True
            return None
        if calls:
            self.refusals = 0
            self.plan_notes = 0
            if all(call.error for call in calls):
                self.chat.strike()
            elif not fabricated:
                self.chat.good()
            self._run_calls(calls, extra=self.sections["FABRICATED"] if fabricated else "")
            if self.state.failures_in_row >= MAX_FAILED_ROUNDS:
                return self._end("BLOCKED", f"{MAX_FAILED_ROUNDS} tool rounds in a row failed")
            if self.denied_rounds >= MAX_DENIED_ROUNDS:
                why = f": {self.denied_reason}" if self.denied_reason else ""
                return self._end("BLOCKED", f"the user denied the tool calls of {self.denied_rounds} replies in a row{why}")
            return None
        if self.answer is not None:
            return self._contract_reply(reply)
        if fabricated and not status:
            self._send_message("FABRICATED")
            self.last_failed = True
            return None
        if self._planning() and not answer_task:
            return self._plan_reply(reply, status)
        if (
            not answer_task
            and self.hand_backs < MAX_HAND_BACKS
            and _hands_back(reply)
            and (status not in _STATUS_OK or self.last_failed or not self.tools_ran)
        ):
            self.hand_backs += 1
            self.chat.strike()
            self.hooks.ui("note", "The model asked you to do it yourself. Sending it back to run it.")
            last = ""
            if self.state.last_command and self.last_failed:
                last = f"The last command was {_one_line(self.state.last_command, 160)} ({self.state.last_exit}). "
            self._send_message("HAND_BACK", last=last)
            return None
        if status in {"FAILED", "BLOCKED"} and _refuses(reply):
            return self._refuse(reply)
        if status:
            self.chat.good()
            return self._finish(status, reply=reply)
        if _no_edit_needed(reply):
            return self._finish("COMPLETED", unchanged_ok=True)
        if answer_task:
            if _refuses(reply) or _asks_user(reply) or _only_promises(reply):
                self.refusals += 1
                if self.refusals > MAX_REFUSALS:
                    return self._end(
                        "FAILED", f"stopped working on the task: {_one_line(_hide_tool_markup(reply), 160)}"
                    )
                self.hooks.ui("note", "Still working on it.")
                self._send_message("ANSWER")
                return None
            self.chat.good()
            return self._finish("COMPLETED")
        if plan and self.plan_notes < 2:
            self.plan_notes += 1
            self.hooks.ui("note", "About to make that change.")
            self._send_message("PLAN_NOTED")
            return None
        question = _real_question(reply)
        if question and self.ask is not None and self.questions < MAX_USER_QUESTIONS:
            self.questions += 1
            answer = (self.ask(question) or "").strip()
            if answer:
                self.chat.good()
                self._send_message("USER_ANSWER", answer=answer)
                return None
        if _stalls(reply):
            if self.state.mutated and not self.last_failed and not _refuses(reply):
                return self._finish("COMPLETED")
            return self._refuse(reply)
        if not self.tools_ran and self.nudges < 2:
            self.nudges += 1
            if reply.strip().upper() != "READY":
                self.chat.strike()
            self.hooks.ui("note", "Still working on it.")
            self._send_message("NUDGE")
            return None
        if self.last_failed and self.recoveries < 2:
            self.recoveries += 1
            self.chat.strike()
            self.hooks.ui("note", "Picking up where it left off.")
            self._send_message("RECOVER")
            return None
        return self._finish("COMPLETED")

    def _plan_reply(self, reply: str, status: str | None) -> str | None:
        """Plan mode: a reply with no calls is the plan. The user approves it, asks for changes, or stops."""
        plan = _hide_tool_markup(reply).strip()
        if status in {"FAILED", "BLOCKED"} and len(plan) > 40:
            return self._finish(status, reply=reply)
        words = re.sub(r"\b(?:COMPLETED|FINISHED|DONE)\b\.?", "", plan).strip()
        if len(words) < 40:
            self.plan_asks += 1
            if self.plan_asks > 2:
                return self._end("FAILED", "plan mode: the model did not send a plan")
            self.hooks.ui("note", "Asking for the plan.")
            self._send_message("PLAN_NEEDED")
            return None
        self.plans += 1
        choice, feedback = _review_plan()
        if choice in {"auto", "edits", "ask"}:
            self.plan_done = True
            self.approve_mode = choice
            self._send_message("PLAN_APPROVED")
            return None
        if choice == "no" and feedback and self.plans <= MAX_PLAN_REVISIONS:
            self._send_message("PLAN_REVISE", feedback=feedback)
            return None
        self.hooks.ui("note", "Plan only; no files were changed. Switch modes with Shift+Tab or /mode to carry it out.")
        self.hooks.status("COMPLETED")
        return "COMPLETED"

    def _refuse(self, reply: str) -> str | None:
        """Send a refusal back. FAILED/BLOCKED does not finish the task when the model only claims the tools are missing."""
        self.refusals += 1
        self.chat.strike()
        if self.refusals > MAX_REFUSALS:
            return self._end("FAILED", f"stopped working on the task: {_one_line(_hide_tool_markup(reply), 160)}")
        self.hooks.ui("note", "Still working on it.")
        self._send_message("RECOVER" if self.tools_ran else "NUDGE")
        return None

    # ----------------------------------------------------------------- running calls

    def _run_calls(self, calls: list[ToolCall], *, extra: str = "") -> None:
        """Run one reply's calls in order. Neighbouring read-only calls run side by side."""
        signature = tuple(_call_key(call) for call in calls if not call.error)
        extras = [extra] if extra else []
        if self._is_loop(signature) and all((self.history.get(key) or {}).get("ok") for key in signature):
            self.chat.strike()
            run_stats.count("repeat_breaks")
            self.hooks.ui("note", "The same calls came back again. Not running them.")
            results = [
                {"tool": canonical_tool(call.tool) or call.tool, "ok": False, "error": "not run: the same calls as your previous replies"}
                for call in calls
            ]
            self.state.step += 1
            self._send_results(
                results,
                extra="\n\n".join(extras + [fill(self.sections["LOOP"], count=str(LOOP_REPLIES))]),
                calls=calls,
            )
            return
        slots: list[dict[str, Any] | None] = [None] * len(calls)
        batch: list[tuple[int, ToolCall]] = []

        def flush() -> None:
            if len(batch) == 1:
                index, call = batch[0]
                slots[index] = self._execute_one(call)
            elif batch:
                for _, call in batch:
                    self.hooks.tool_start(call)
                with ThreadPoolExecutor(max_workers=min(PARALLEL_WORKERS, len(batch))) as pool:
                    futures = [
                        (index, call, pool.submit(_execute, call.tool, call.arguments, self.ctx))
                        for index, call in batch
                    ]
                    for index, call, future in futures:
                        result = future.result()
                        self.hooks.tool_done(call, result)
                        self._record(call, result)
                        run_stats.tool_result(canonical_tool(call.tool) or call.tool, result)
                        slots[index] = result
            batch.clear()

        denied = False
        plan_blocked = False
        for index, call in enumerate(calls):
            if call.error:
                flush()
                run_stats.tool_result(call.tool, None, malformed=True)
                slots[index] = {"tool": call.tool, "ok": False, "error": call.error}
                self.hooks.ui("bad", _friendly_error(call.error))
                continue
            repeat = self._repeat_result(call)
            if repeat is not None:
                flush()
                run_stats.count("repeat_answers")
                self.hooks.tool_start(call)
                self.hooks.tool_done(call, repeat)
                slots[index] = repeat
                continue
            perm = self._permission(call)
            kind = getattr(perm, "kind", "read") or "read"
            if self.read_only and not _read_only_allows(call, perm):
                flush()
                result = {
                    "tool": canonical_tool(call.tool) or call.tool,
                    "ok": False,
                    "error": "not run: this task is read-only. Only reads, searches, and git history work; "
                    "no edits, commands, fetches, helpers, or paths outside the project",
                    "planned": True,
                }
                self.hooks.tool_start(call)
                self.hooks.tool_done(call, result)
                slots[index] = result
                continue
            if kind == "read" and canonical_tool(call.tool) in _PARALLEL_SAFE:
                batch.append((index, call))
                continue
            flush()
            if self._planning() and not self._plan_allows(call, perm):
                result = {
                    "tool": canonical_tool(call.tool) or call.tool,
                    "ok": False,
                    "error": "not run: plan mode is on. Read only for now, then reply with the plan",
                    "planned": True,
                }
                self.hooks.tool_start(call)
                self.hooks.tool_done(call, result)
                slots[index] = result
                plan_blocked = True
                continue
            key = _call_key(call)
            if key in self.denials:
                # Asked and denied already in this task: do not ask the user again.
                reason = self.denials[key]
                denied = True
                error = (
                    "not run: you already sent this exact call and the user denied it"
                    + (f" ({reason})" if reason else "")
                    + ". Do not send it again; change the approach or use ask_user"
                )
                result = {"tool": canonical_tool(call.tool) or call.tool, "ok": False, "error": error, "denied": True}
                self.hooks.tool_start(call)
                self.hooks.tool_done(call, result)
                slots[index] = result
                continue
            allowed, reason = taint.check(perm, self._approve_mode(), self._approved)
            if not allowed:
                denied = True
                self.denials[key] = reason
                if reason:
                    self.denied_reason = reason
                error = "denied by the user" + (f": {reason}" if reason else "")
                result = {"tool": canonical_tool(call.tool) or call.tool, "ok": False, "error": error, "denied": True}
                self.hooks.tool_done(call, result)
                slots[index] = result
                continue
            slots[index] = self._execute_one(call)
        flush()
        results = [item for item in slots if item is not None]
        self.state.step += 1
        counted = [item for item in results if not item.get("denied") and not item.get("planned")]
        self.denied_rounds = self.denied_rounds + 1 if denied and not counted else 0
        self.last_failed = any(not item.get("ok") for item in counted)
        if not self.last_failed:
            self.recoveries = 0
        if counted and all(not item.get("ok") for item in counted):
            self.state.failures_in_row += 1
        elif counted:
            self.state.failures_in_row = 0
        if denied:
            extras.append(self.sections["DENIED"])
        if plan_blocked:
            extras.append(self.sections["PLAN_ONLY"])
        self._send_results(results, extra="\n\n".join(extras), calls=[c for c, slot in zip(calls, slots) if slot is not None])

    def _execute_one(self, call: ToolCall) -> dict[str, Any]:
        self.hooks.tool_start(call)
        started = time.monotonic()
        result = _execute(call.tool, call.arguments, self.ctx)
        seconds = time.monotonic() - started
        self.hooks.tool_done(call, result)
        self._record(call, result)
        name = canonical_tool(call.tool) or call.tool
        run_stats.tool_result(name, result)
        if name in run_stats.EDIT_TOOLS:
            try:
                size = len(json.dumps(call.arguments, ensure_ascii=False))
            except (TypeError, ValueError):
                size = 0
            run_stats.edit(name, ok=bool(result.get("ok")), seconds=seconds, argument_chars=size)
        return result

    def _permission(self, call: ToolCall) -> Any:
        name = canonical_tool(call.tool)
        check = getattr(agent_tools, "permission_for", None)
        if check is None or not name:
            return _Perm("read", _activity(call), "")
        try:
            return check(name, call.arguments, self.ctx)
        except Exception as exc:  # a broken check must not let an edit through unasked
            log.debug(f"permission check failed for {name}: {exc}")
            if name in _read_only_tools():
                return _Perm("read", _activity(call), "")
            return _Perm("command" if name == "run_command" else "edit", _activity(call), "")

    def _approved(self, perm: Any) -> tuple[bool, str]:
        """:meth:`_decide`, recorded in the session log when it was not a plain read."""
        allowed, reason = self._decide(perm)
        kind = getattr(perm, "kind", "read") or "read"
        slog = getattr(getattr(self, "chat", None), "log", None)
        if slog is not None and (kind != "read" or not allowed):
            slog.write(
                "permission_decision",
                kind=kind,
                summary=str(getattr(perm, "summary", "") or getattr(perm, "detail", "") or ""),
                allowed=allowed,
                reason=reason or None,
                mode=self._approve_mode(),
            )
        return allowed, reason

    def _decide(self, perm: Any) -> tuple[bool, str]:
        """Ask the user unless the call only reads, the mode allows it, or it was always-allowed.

        ask: every edit, command, fetch, and outside path. edits: file edits in
        the workspace run. auto: edits and fetches run, and so do read-only
        commands; any other command asks (or runs once "always allow" covers its
        program), since a command can send data off this machine in ways no check
        sees. A risky command (a broad delete, git reset --hard, a push, a flash,
        a secret file, a path outside the workspace) asks in every mode, unless
        ``"confirm_risky": false``.
        """
        kind = getattr(perm, "kind", "read") or "read"
        denied = _rule_denied(self.ctx, perm)
        if denied:
            return False, denied
        if kind == "read":
            return True, ""
        mode = self._approve_mode()
        risky = self._risk(perm)
        if risky and not self.confirm_risky and mode == "auto":
            risky = ""
        if not risky:
            # A command inside the OS sandbox (perm.contained) cannot write outside the workspace or
            # reach a host the egress allow-list does not allow, so auto mode runs it unasked.
            if mode == "auto" and (kind != "command" or getattr(perm, "contained", False)
                                   or _read_only_command(str(getattr(perm, "detail", "") or ""))):
                return True, ""
            if mode == "edits" and kind == "edit":
                return True, ""
        if _rule_allows(self.ctx, perm, risky):
            return True, ""
        key = getattr(perm, "key", "") or ""
        rememberable = bool(key) and kind != "outside"
        if rememberable and key in self.always and not risky:
            return True, ""
        decision, reason = _approve_prompt(perm, risky=risky) if risky else _approve_prompt(perm)
        if decision == "always":
            if rememberable:
                self.always.add(key)
            rule = str(getattr(perm, "rule", "") or "")
            if rule and kind == "command" and not risky:
                _save_allow_rule(self.ctx, rule)
            return True, ""
        if decision == "yes":
            return True, ""
        return False, (reason or "").strip()

    def _risk(self, perm: Any) -> str:
        declared = str(getattr(perm, "risk", "") or "")
        if declared:
            return declared
        if getattr(perm, "kind", "") not in {"command", "outside"}:
            return ""
        check = getattr(agent_tools, "risky_command", None)
        command = str(getattr(perm, "detail", "") or "")
        if not callable(check) or not command:
            return ""
        try:
            return str(check(command) or "")
        except Exception as exc:
            log.debug(f"risk check failed: {exc}")
            return ""

    def _planning(self) -> bool:
        return not self.plan_done and self._approve_mode() == "plan"

    def _plan_allows(self, call: ToolCall, perm: Any) -> bool:
        """In plan mode: reads, read-only commands, and fetches run; edits and builds wait for the plan."""
        kind = getattr(perm, "kind", "read") or "read"
        name = canonical_tool(call.tool)
        if kind == "read" or kind == "network":
            return True
        if name in _read_only_tools():
            return True  # a read outside the workspace still asks
        if name in {"run_command", "command_input"}:
            check = getattr(agent_tools, "read_only_command", None)
            command = str(call.arguments.get("command") or call.arguments.get("input") or "")
            return bool(callable(check) and check(command))
        return False

    def _approve_mode(self) -> str:
        """The mode now. /permissions in the UI can flip it mid-session; that change wins."""
        live = _ui_approve_mode()
        if live in _MODES and live != self.ui_mode_at_start:
            return live
        return self.approve_mode

    def _record(self, call: ToolCall, result: dict[str, Any]) -> None:
        """Remember what this call did, so an identical repeat can be answered without running it."""
        name = canonical_tool(call.tool) or call.tool
        ok = bool(result.get("ok"))
        if name == "run_command":
            self.verified = True
            self.last_output = str(result.get("output") or result.get("error") or "")[-1_500:]
        if ok:
            self.tools_ran = True
            if name in MUTATING or (name == "delegate" and result.get("changed")):
                self.check_passed = False
                self.verified = False
            if name not in _read_only_tools():
                self.world += 1
        else:
            self._note_failure(call, result)
        key = _call_key(call)
        previous = self.history.get(key)
        same = previous is not None and previous["world"] == self.world and previous["ok"] == ok
        self.history[key] = {
            "world": self.world,
            "ok": ok,
            "count": previous["count"] + 1 if same else 1,
            "fails": (previous["fails"] + 1 if same else 1) if not ok else 0,
            "step": self.state.step,
            "error": str(result.get("error") or ""),
            "output": str(result.get("output") or ""),
        }

    def _repeat_result(self, call: ToolCall) -> dict[str, Any] | None:
        """The answer to an identical call made when nothing has changed since, or None to run it.

        A failed call is not run again (a command gets one retry); a
        read-only call is not run a third time.
        """
        record = self.history.get(_call_key(call))
        if record is None or record["world"] != self.world:
            return None
        name = canonical_tool(call.tool) or call.tool
        if not record["ok"]:
            if record["fails"] < (2 if name == "run_command" else 1):
                return None
            record["fails"] += 1
            result: dict[str, Any] = {
                "tool": name,
                "ok": False,
                "error": (
                    f"{record['error'] or 'tool failed'}; not run again: this exact call already failed "
                    f"at step {record['step']} and nothing has changed since. Change the arguments or take another approach"
                ),
            }
            if record["output"]:
                result["output"] = record["output"]
            self._note_failure(call, result)
            return result
        if name in _read_only_tools() and name not in _POLLING and record["count"] >= MAX_SAME_CALL:
            record["count"] += 1
            return {
                "tool": name,
                "ok": True,
                "output": (
                    f"not run again: this exact call already ran {record['count'] - 1} times since the last change "
                    f"(last at step {record['step']}) and its result is above. Use it, or do something different."
                ),
            }
        return None

    def _is_loop(self, signature: tuple[str, ...]) -> bool:
        """True when this reply repeats the calls of the previous LOOP_REPLIES - 1 replies."""
        polling = all(key.split(":", 1)[0] in _POLLING for key in signature)
        self.signatures = (self.signatures + [signature])[-LOOP_REPLIES:]
        if not signature or polling or len(self.signatures) < LOOP_REPLIES:
            return False
        if len(set(self.signatures)) == 1:
            self.signatures = []
            return True
        return False

    def _note_failure(self, call: ToolCall, result: dict[str, Any]) -> None:
        key = call.tool + ":" + json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)
        count = self.state.failed_calls.get(key, 0) + 1
        self.state.failed_calls[key] = count
        if count < 2:
            return
        result["error"] = (
            str(result.get("error") or "tool failed")
            + f"; this exact call has now failed {count} times. Do not send it again unchanged"
        )
        if count >= 3 and canonical_tool(call.tool) == "edit_file":
            region = self._wide_region(normalize_args("edit_file", call.arguments))
            if region:
                result["output"] = (
                    "The program read the closest region of the file for you. Copy old_string "
                    "from these lines exactly, without the N| prefix, or use write_files:\n" + region
                )

    def _wide_region(self, arguments: dict[str, Any]) -> str:
        raw = arguments.get("path")
        old = arguments.get("old_string")
        if not isinstance(raw, str) or not isinstance(old, str):
            return ""
        path = self.ctx.workspace / raw
        if not path.is_file():
            return ""
        try:
            text = agent_edit.load_text(path).text
        except OSError:
            return ""
        return agent_edit.best_candidate(text, agent_edit.strip_read_prefix(old), context=15)

    # ----------------------------------------------------------------- finishing

    def _contract_reply(self, reply: str) -> str | None:
        """A reply with no tool calls under an answer contract: an accepted answer ends the task."""
        contract = self.answer
        assert contract is not None
        if contract.accepts(reply):
            self.chat.good()
            self.hooks.status("COMPLETED")
            return "COMPLETED"
        self.answer_nudges += 1
        if self.answer_nudges > contract.max_nudges:
            return self._end("FAILED", contract.failure)
        self.hooks.ui("note", contract.note)
        self.payload = contract.nudge
        self.turns.append({"role": "user", "content": self.payload})
        return None

    def _finish(self, status: str, *, reply: str = "", unchanged_ok: bool = False) -> str | None:
        if status in {"FAILED", "BLOCKED"}:
            reason = _failure_reason(reply)
            if not reason and not self.reason_asked:
                self.reason_asked = True
                self.hooks.ui("note", "Asking why that failed.")
                self._send_message("WHY_FAILED")
                return None
            if reason:
                self.hooks.ui("bad", reason)
        if status in _STATUS_OK and not self.jobs_nudged:
            running = self._running_jobs()
            if running:
                self.jobs_nudged = True
                self.hooks.ui("note", "Background jobs are still running. Asking about them.")
                template = self.sections.get("JOBS_RUNNING") or _FALLBACKS["JOBS_RUNNING"]
                self.sections = {**self.sections, "JOBS_RUNNING": template}
                self._send_message("JOBS_RUNNING", jobs="\n".join(running))
                return None
        if status in _STATUS_OK:
            changed = self._files_changed()
            if changed and self.check_command and not self.check_passed:
                result = self._run_check()
                if result.get("ok"):
                    self.check_passed = True
                    self.hooks.ui("good", "The check passed.")
                else:
                    self.check_cycles += 1
                    if self.check_cycles > MAX_CHECK_CYCLES:
                        return self._end("FAILED", f"the check still fails: {self.check_command}")
                    self.hooks.ui("bad", "The check failed. Sending the failure back.")
                    self._send_results([result], extra=self.sections["CHECK_FAILED"])
                    return None
            elif (
                changed
                and not self.check_command
                and not self.verified
                and not self.verify_asked
                and _has_checks(self.ctx.workspace)
            ):
                self.verify_asked = True
                self.hooks.ui("note", "Nothing was run after the edits. Asking for a check.")
                self._send_message("VERIFY")
                return None
            if (
                not changed
                and not unchanged_ok
                and not self.nothing_nudged
                and _asks_for_change(self.task)
            ):
                self.nothing_nudged = True
                self.hooks.ui("note", "Nothing changed yet. Asking once more.")
                self._send_message("NOTHING_CHANGED")
                return None
        self._show_disk_once()
        self.hooks.status(status)
        return status

    def _note_shell_change(self) -> None:
        """After /shell, use the new shell and tell the model in the next message."""
        take = getattr(ui.current(), "take_shell_change", None)
        chosen = take() if callable(take) else None
        if chosen is None:
            return
        self.ctx.shell = chosen
        available = getattr(self.ctx.session, "available", None)
        try:
            preamble = agent_shell.shell_preamble(chosen, available)
        except Exception:  # a stand-in shell without the usual fields
            preamble = f"SHELL: {getattr(chosen, 'label', chosen)}."
        label = getattr(chosen, "label", "") or str(chosen)
        self.payload = f"Shell changed to {label}: {preamble}\n\n" + self.payload

    def _note_mode_change(self) -> None:
        """A mode switched mid-task (/mode, Shift+Tab) goes to the model as a short appended note."""
        mode = self._approve_mode()
        if mode == self.mode_seen:
            return
        old, self.mode_seen = self.mode_seen, mode
        self.chat.log.write("mode_changed", old=old, new=mode)
        self.payload = prompt_build.change_note("mode", _MODE_NOTES.get(mode, mode)) + "\n\n" + self.payload

    def _run_check(self) -> dict[str, Any]:
        """The project check. It always runs again after a change, with its own long timeout.

        It goes through the same approval as any command: the check command can come from
        a file in the repository (``.crit/AGENT.md``), so it is not trusted on its own.
        """
        self.hooks.ui("work", f"Checking with {_one_line(self.check_command or '', 60)}")
        call = ToolCall(
            "run_command",
            {"command": self.check_command, "timeout": self.check_timeout, "cwd": str(self.ctx.workspace)},
        )
        approved, reason = self._approved(self._permission(call))
        if not approved:
            result = {
                "tool": "run_command",
                "ok": False,
                "error": "the user did not approve the check command" + (f": {reason}" if reason else ""),
            }
            self.last_output = result["error"]
            return result
        session = self.ctx.session
        before = getattr(session, "cwd", None)
        self.hooks.tool_start(call)
        try:
            result = _execute(call.tool, call.arguments, self.ctx)
        finally:
            if before is not None:
                # The check runs in the workspace and leaves the session's cwd where the model put it.
                session.cwd = before
        self.hooks.tool_done(call, result)
        self.last_output = str(result.get("output") or result.get("error") or "")[-1_500:]
        return result

    def _files_changed(self) -> bool:
        """True when this task's bytes on disk differ from the pre-edit copies.

        With no checkpoint, the successful mutating tools are the record.
        """
        checkpoints = self.ctx.checkpoints
        if checkpoints is not None and checkpoints.task_dir is not None:
            diff = checkpoints.disk_diff()
            if diff is not None:
                return bool(diff.strip())
        return self.state.mutated

    def _show_disk_once(self) -> None:
        """Print this task's on-disk diff a single time, for the person watching."""
        if self.reviewed:
            return
        self.reviewed = True
        checkpoints = self.ctx.checkpoints
        diff = checkpoints.disk_diff() if checkpoints is not None else None
        if diff is None:
            if self.state.mutated:
                self._show_diffstat()
            return
        if not diff.strip():
            return
        self.hooks.ui("good", "On disk, this task changed:")
        lines = diff.splitlines()
        if not _show_diff_in_ui(diff):
            for line in lines[:_REVIEW_LINES]:
                log.print_safe(f"  {line}", file=sys.stderr, flush=True)
        if len(lines) > _REVIEW_LINES:
            self.hooks.ui("note", f"{len(lines) - _REVIEW_LINES} more lines in the diff")

    def _show_diffstat(self) -> None:
        changed = ", ".join(self.state.edits) or "none"
        self.hooks.ui("good", f"Changed: {_one_line(changed, 160)}")
        if not is_git_repo(self.ctx.workspace):
            return
        result = _execute("git_diff", {"stat": True}, self.ctx)
        if result.get("ok"):
            for line in str(result.get("output", "")).splitlines()[1:21]:
                self.hooks.ui("good", line)

    def _end(self, code: str, message: str) -> str:
        log.warn(message)
        self.hooks.ui("bad", message)
        self.turns.append({"role": "assistant", "content": message})
        self._show_disk_once()
        self.hooks.status(code)
        return code

    # ----------------------------------------------------------------- messages

    def _state_text(self) -> str:
        return self.state.render(self.sections["STATE"])

    def _handoff_note(self) -> str:
        """The old chat's own summary for its successor (one extra round trip), or "" if it fails."""
        self.hooks.ui("note", "Asking this chat for a handoff note before moving to a new one.")
        try:
            reply = self.chat.send(self.sections["HANDOFF"] + "\n\n" + self._state_text())
        except _ProviderFailed as exc:
            log.debug(f"no handoff note: {exc}")
            return ""
        self.turns.append({"role": "assistant", "content": reply})
        note = _hide_tool_markup(reply).strip()
        if _status_code(note) and len(note) < 40:
            return ""
        return note[:5_000]

    def _resume_text(self, note: str = "") -> str:
        """What a fresh chat needs to carry on: the task, progress, the last result, and the old chat's note."""
        lines = [self.sections["RESUME"], f"Task: {self.task.strip()}"]
        if self.state.todos:
            lines.append(
                "Todos: " + "; ".join(f"[{item.get('status')}] {item.get('content')}" for item in self.state.todos[:12])
            )
        changed = ""
        checkpoints = self.ctx.checkpoints
        if checkpoints is not None and checkpoints.task_dir is not None:
            changed = _diffstat(checkpoints.disk_diff() or "")
        if not changed and self.state.edits:
            changed = ", ".join(self.state.edits)
        lines.append(f"Files changed in this task: {changed or 'none yet'}")
        if self.state.reads:
            lines.append("Files read: " + ", ".join(list(self.state.reads)[-12:]) + " (read again before editing)")
        if self.findings:
            lines.append("Your notes so far:\n- " + "\n- ".join(self.findings))
        if self.state.last_command:
            lines.append(f"Last command: {self.state.last_command} -> {self.state.last_exit}")
            if self.last_output.strip():
                lines.append("Its output ended with:\n" + self.last_output.strip())
        if note:
            lines.append("Handoff note from the previous chat (its own words):\n" + note)
        lines.append("The latest message from the program follows.")
        return "\n".join(lines)

    def _job_rows(self) -> list[dict[str, Any]]:
        session = getattr(self.ctx, "session", None)
        try:
            return list(session.jobs()) if session is not None and hasattr(session, "jobs") else []
        except Exception:  # noqa: BLE001
            return []

    def _running_jobs(self) -> list[str]:
        """``- b2: ./gradlew test`` for each background job this task started that still runs."""
        before = getattr(self, "jobs_before", set())
        return [
            f"- {row.get('id')}: {_one_line(str(row.get('command') or ''), 100)}"
            for row in self._job_rows()
            if row.get("running") and row.get("id") not in before
        ]

    def _job_notes(self) -> str:
        """One line per background job that ended since the model last heard of it."""
        try:
            return "\n".join(tool_api.finished_job_notes(self.ctx))
        except Exception as exc:  # a note is a convenience; never break a turn over it
            log.debug(f"job notes failed: {exc}")
            return ""

    def _send_results(
        self, results: list[dict[str, Any]], *, extra: str = "", calls: list[ToolCall] | None = None
    ) -> None:
        state_text = self._state_text()
        notes = self._job_notes()
        if notes:
            extra = notes + ("\n\n" + extra if extra else "")
        room = max(2_000, self.max_result_chars - len(state_text) - len(extra) - 4)
        if self.tool_format in _RESPONSES_WIRE:
            from critique_bot import openai_format

            ids = [call.call_id for call in calls or []]
            stamped = [
                {**item, "call_id": ids[index] if index < len(ids) and ids[index] else f"call_{index + 1}"}
                for index, item in enumerate(results)
            ]
            body = _cap(openai_format.format_results(stamped), room)
        else:
            body = _cap("\n".join(format_tool_result(item) for item in results), room)
        if extra:
            body += "\n\n" + extra
        self.payload = body + "\n\n" + state_text
        self.turns.append({"role": "tool", "content": self.payload})

    def _send_message(self, section: str, **values: str) -> None:
        text = fill(self.sections[section], path=self.state.last_path or "path/to/file", **values)
        notes = self._job_notes()
        if notes:
            text = notes + "\n\n" + text
        self.payload = text + "\n\n" + self._state_text()
        self.turns.append({"role": "user", "content": self.payload})


def effective_settings(settings: object) -> dict[str, Any]:
    """``.crit/settings.json`` as the agent applies it: under strict egress nothing but the chat is reached.

    With ``"egress": "strict"`` in config.json (the default), commands run offline, web_fetch is
    off, and commands that can send data are blocked, whatever the settings file says.
    """
    options = dict(settings) if isinstance(settings, dict) else {}
    if egress.strict():
        options.update({"offline": True, "web_fetch": False, "network_commands": "block"})
    return options


def run_agent_loop(
    session: Any,
    *,
    workspace: Path,
    index_path: Path | None,
    cache_dir: Path | None,
    first_task: str,
    max_rounds: int | None,
    max_result_chars: int,
    seed: str | None = None,
    read_message: Callable[[], str | None] | None = None,
    emit: Callable[[str], None] | None = None,
    outcome: list[str] | None = None,
    check_command: str | None = None,
    shell: agent_shell.Shell | None = None,
    runner: Callable[..., Any] | None = None,
    approve_mode: str = "ask",
    session_shell: Any = None,
    turns: list[dict[str, str]] | None = None,
    ask_user: Callable[[str], str | None] | None = None,
    settings: dict[str, Any] | None = None,
    seed_first: bool = False,
    helper_factory: Callable[[], Any] | None = None,
    helper_count: int = 0,
    helper_inline: bool = False,
    role: str | None = None,
    reseed: Callable[[str], str] | None = None,
    read_only: bool = False,
    answer: AnswerContract | None = None,
    single_task: bool = False,
    session_log: Any = None,
    resume: bool = False,
    side_factory: Callable[[], Any] | None = None,
) -> list[dict[str, str]]:
    """Talk to ``session.send`` until each task reaches a finish code.

    ``seed`` (the instructions) goes in front of the first task, so the first
    reply already works on it; ``seed_first`` sends it alone first instead.
    ``role`` is the role the seed was built for (``"role"`` in settings when
    not given); its skills go with every task. ``reseed(role_id)`` builds the
    seed for another role: after ``/role`` the next task starts a new chat with it.
    Each task gets fresh working memory and an undo checkpoint. COMPLETED,
    FINISHED, DONE, FAILED, BLOCKED, a direct answer to a question, or a plain
    answer after a tool has run ends that task; the same chat waits for the
    next one. Ctrl+C ends the running task as INTERRUPTED; ``/new`` starts a
    new chat. ``turns`` (when given) is filled as the session goes.

    ``settings`` keys: ``check_timeout`` (seconds, default 600),
    ``compact_after_chars`` (default 120000), ``reply_retries`` (default 3).

    ``session_log`` (a :class:`session_log.SessionLog`) records every event of the session;
    with ``resume`` it already holds an earlier run, and the conversation is rebuilt from
    it first. ``side_factory`` opens a second session on the same model for one-off
    requests (the compaction summary). A model API session (``_context_window`` in
    settings, a session with ``replace_history``) is compacted by context pressure
    (:mod:`compaction`) instead of moving to a new chat by size, turns, and time.
    """
    turns = turns if turns is not None else []
    reader = read_message or _read_message
    show = emit or _emit
    ask_person = ask_user or (lambda question: _ask_user(question, None))
    sections = load_prompt_sections(tool_format_of(settings if isinstance(settings, dict) else None))
    native: list[dict[str, Any]] | None = None
    if sections.get("_FORMAT") == TOOL_FORMAT_NATIVE:
        found = (settings or {}).get("_native_tools") if isinstance(settings, dict) else None
        native = list(found) if found else sorted(
            tool_api.registry().openai_tools(), key=lambda tool: str((tool.get("function") or {}).get("name")))
    runtime.start(native=native, extensions_=(settings or {}).get("_extensions") if isinstance(settings, dict) else None)
    _NATIVE_TOOLS = native

    def ask(question: str) -> str | None:
        # Auto mode does not stop for questions: the model decides and says what it chose.
        if decide_in_auto and _current_mode(approve_mode, ui_mode_at_start) == "auto":
            _ui("note", f"Auto mode: the model decides ({_one_line(question, 100)})")
            return sections["AUTO_DECIDE"]
        return ask_person(question)

    workspace = Path(workspace).resolve()
    checkpoints = agent_edit.Checkpoints(cache_dir, workspace)
    chosen = shell or getattr(session_shell, "shell", None) or agent_shell.detect_shell()
    options = effective_settings(settings)
    role_now = roles.normalize_role(role) or roles.normalize_role(options.get("role")) or roles.GENERIC
    chat = _Chat(
        session,
        seed=seed or "",
        retries=_int_setting(options, "reply_retries", DEFAULT_REPLY_RETRIES, 0),
        compact_after=_int_setting(options, "compact_after_chars", DEFAULT_COMPACT_AFTER_CHARS, 10_000),
        compact_turns=_int_setting(options, "compact_after_turns", DEFAULT_COMPACT_AFTER_TURNS, 5),
        compact_minutes=_int_setting(options, "compact_after_minutes", DEFAULT_COMPACT_AFTER_MINUTES, 5),
    )
    if cache_dir is not None:
        chat.failure_dir = Path(cache_dir) / "failures"
    slog = session_log if session_log is not None else session_log_mod.NullLog()
    chat.log = slog
    chat.side_factory = side_factory
    _setup_model_api_session(chat, options, slog)
    handoff_notes = options.get("handoff_notes") is not False
    check_timeout = _int_setting(options, "check_timeout", DEFAULT_CHECK_TIMEOUT, 1)
    build_timeout = _int_setting(options, "build_timeout", int(agent_build.DEFAULT_BUILD_TIMEOUT), 60)
    command_retries = _int_setting(options, "command_retries", 1, 0)
    if session_shell is not None and options.get("background_on_timeout") is False:
        try:
            session_shell.detach_on_timeout = False
        except Exception:
            pass
    confirm_risky = options.get("confirm_risky") is not False
    decide_in_auto = str(options.get("auto_questions") or "decide").lower() != "ask"
    auto_skills = options.get("auto_skills") is not False
    max_skills = _int_setting(options, "max_skills", 2, 0)
    always: set[str] = set()
    ui_mode_at_start = _ui_approve_mode()
    base = dict(
        workspace=workspace,
        index_path=index_path,
        cache_dir=cache_dir,
        max_chars=min(DEFAULT_TOOL_CHARS, max_result_chars),
        runner=runner,
        shell=chosen,
        session=session_shell,
        on_output=_tool_output,
        ask_user=lambda question: ask(question) or "",
        confirm=lambda question: ask_person(question) or "",
        build_timeout=float(build_timeout),
        command_retries=command_retries,
        network_commands=(
            "ask" if str(options.get("network_commands") or "").lower() == "ask" and options.get("offline") is not True else "block"
        ),
        web_hosts=_web_hosts(options),
        rules=_load_rules(options),
        settings=options,
    )
    outcome_code = "COMPLETED"
    pool_box: list[Any] = []

    def make_delegate(
        cancel: threading.Event, checkpoints_now: Any, state: TaskState
    ) -> Callable[[list[dict[str, Any]]], dict[str, Any]] | None:
        """The delegate tool's runner for one task: briefs go to helper tabs, all at once."""
        if helper_factory is None or helper_count < 1:
            return None
        from critique_bot import agent_helpers

        def delegate(briefs: list[dict[str, Any]]) -> dict[str, Any]:
            if not pool_box:
                # Inline: one extra tab on this thread, briefs in turn (no remote debugging needed).
                pool_class = agent_helpers.InlineHelperPool if helper_inline else agent_helpers.HelperPool
                pool_box.append(
                    pool_class(
                        helper_factory,
                        helper_count,
                        seed=chat.seed or seed or "",
                        sections=sections,
                        base=base,
                        max_result_chars=max_result_chars,
                        retries=_int_setting(options, "reply_retries", DEFAULT_REPLY_RETRIES, 0),
                        compact_after=_int_setting(options, "compact_after_chars", DEFAULT_COMPACT_AFTER_CHARS, 10_000),
                    )
                )
            started = time.monotonic()
            _set_active("delegate", len(briefs))
            try:
                reports = pool_box[0].run(
                    briefs,
                    cancel=cancel,
                    progress=lambda line: _tool_output(line + "\n"),
                    context={"checkpoints": checkpoints_now},
                )
            finally:
                _set_active("delegate", 1)
            result = agent_helpers.format_reports(reports, time.monotonic() - started)
            for name in result.get("changed") or []:
                # The coordinator's task owns these changes: the diff, the check, and undo cover them.
                state.edits[name] = state.edits.get(name, 0) + 1
            return result

        return delegate

    carry = ""
    restored = False
    if resume and slog.enabled:
        restored, carry = _resume_from_log(chat, slog)
    if seed and seed.strip():
        slog.prefix = _without_ready(seed) if not seed_first else seed.strip()
        if not restored:
            slog.write("system_prompt", hash=prompt_build.prefix_hash(seed), text=seed)
            if _NATIVE_TOOLS:
                declared = json.dumps(_NATIVE_TOOLS, sort_keys=True, ensure_ascii=False)
                # One string, so the long list goes to a blob instead of a long line in the log.
                slog.write("tools_declared", hash=session_log_mod.text_hash(declared)[:16], tools_json=declared,
                           names=[str((tool.get("function") or {}).get("name")) for tool in _NATIVE_TOOLS])
        if restored:
            pass  # the rebuilt conversation already starts with the instructions
        elif seed_first:
            _seed_session(
                chat, seed.strip(), turns, ctx=_tool_context(**base), max_result_chars=max_result_chars, show=show
            )
        else:
            chat.prefix = _without_ready(seed)
    first_context = str(options.get("_first_turn_context") or "").strip() if not restored else ""
    pending = first_task.strip()
    announced = False
    mode_told = _current_mode(approve_mode, ui_mode_at_start)
    while True:
        if not pending and single_task and turns:
            break  # one task, and it ran: do not wait for another
        if not pending:
            # Between tasks: ask about sites the chat page was blocked from (terminal sessions only).
            config_prompts.offer_blocked_hosts()
            if not announced:
                _ui("note", "Ready. Type a task, or exit.")
                announced = True
            try:
                pending = reader() or ""
            except (KeyboardInterrupt, EOFError):
                break
            if not pending.strip():
                break
        task = pending.strip()
        pending = ""
        if task.lower() == "/theme":
            handler = getattr(ui.current(), "handle_command", None)
            if callable(handler):
                handler("/theme", "")
            continue
        if task.lower() == "/new":
            if not chat.restart("you asked for one"):
                _ui("note", "This chat page cannot open a new conversation; staying in this one.")
            continue
        if task.lower().split(None, 1)[0] in {"/role", "/agent"}:
            # A reader without the slash-command layer: handle it here rather than send it as a task.
            handler = getattr(ui.current(), "handle_command", None)
            if callable(handler):
                handler("/role", task.split(None, 1)[1] if " " in task.strip() else "")
            continue
        if task.lower().split(None, 1)[0] in {"/resume", "/fork", "/sessions"}:
            # A reader without the slash-command layer: the UI records the request; the next task uses it.
            handler = getattr(ui.current(), "handle_command", None)
            if callable(handler):
                handler(task.lower().split(None, 1)[0], task.split(None, 1)[1] if " " in task.strip() else "")
            continue
        switched = _take_session_change(chat, slog)
        if switched is not None:
            slog, switch_note = switched
            carry = (carry + "\n\n" + switch_note).strip() if switch_note else carry
        changed_role = _apply_role_change(chat, reseed)
        if changed_role:
            slog.write("role_changed", old=role_now, new=changed_role)
        role_now = changed_role or role_now
        prompt_hook = runtime.hook("user_prompt", {"prompt": task})
        if prompt_hook is not None and prompt_hook.denied:
            _ui("bad", "A hook stopped this task" + (f": {prompt_hook.message}" if prompt_hook.message else "."))
            continue
        _ui("task", f"Working on: {_one_line(task, 100)}")
        turns.append({"role": "user", "content": task})
        slog.write("user_message", text=task, task=True, mode=_current_mode(approve_mode, ui_mode_at_start),
                   role=role_now)
        if chat.stale_for_next_task():
            chat.restart(chat.rotation_reason(size_only=True, share=BOUNDARY_ROTATION_SHARE) or "the chat grew long")
        repo_map = _prepare_index(workspace, index_path, task)
        if chat.map_sent:
            repo_map = ""
        elif repo_map.strip():
            chat.map_sent = True
        skills = _task_skills(task, workspace, chat, options, auto=auto_skills, limit=max_skills, role=role_now)
        mode_now = _current_mode(approve_mode, ui_mode_at_start)
        mode_note = sections["PLAN_MODE"] if mode_now == "plan" and not _answer_only(task) else ""
        if mode_now != mode_told:
            slog.write("mode_changed", old=mode_told, new=mode_now)
            chat.pending_notes.append(prompt_build.change_note("mode", _MODE_NOTES.get(mode_now, mode_now)))
            mode_told = mode_now
        checkpoints.start_task()
        if checkpoints.task_dir is not None:
            slog.write("checkpoint", undo=checkpoints.task_dir.name)
        cancel = threading.Event()
        task_state = TaskState(task=task)
        ctx = _tool_context(
            **base,
            state=task_state,
            checkpoints=checkpoints,
            cancel=cancel,
            delegate=make_delegate(cancel, checkpoints, task_state),
        )
        run = _TaskRun(
            chat,
            task,
            ctx=ctx,
            sections=sections,
            turns=turns,
            show=show,
            max_rounds=max_rounds,
            max_result_chars=max_result_chars,
            check_command=check_command,
            check_timeout=check_timeout,
            approve_mode=approve_mode,
            always=always,
            ask=ask,
            ui_mode_at_start=ui_mode_at_start,
            confirm_risky=confirm_risky,
            read_only=read_only,
            answer=answer,
        )
        if read_only or mode_now != "plan" or _answer_only(task):
            run.plan_done = True
        run.handoff = handoff_notes
        tickets = tool_api.jira_context(task)
        message = task_message(
            task, repo_map=repo_map, skills=skills, mode_note=mode_note, prefix=sections["TASK_PREFIX"], tickets=tickets,
        )
        if chat.pending_notes:
            message = "\n\n".join(chat.pending_notes) + "\n\n" + message
            chat.pending_notes = []
        if first_context:
            message = first_context + "\n\n" + message
            first_context = ""
        if carry:
            message = carry + "\n\n" + message
            carry = ""
        if slog.enabled:
            run.hooks = _LoggedHooks(run.hooks, slog)
        if prompt_hook is not None and prompt_hook.append_context:
            message += "\n\nCONTEXT FROM A HOOK\n" + prompt_hook.append_context
        try:
            outcome_code = run.run(message)
        except KeyboardInterrupt:
            cancel.set()
            _stop_background(session_shell)
            try:
                _stop_reply(chat.session)
            except KeyboardInterrupt:  # a second Ctrl+C: skip it; the next send stops it
                pass
            outcome_code = "INTERRUPTED"
            turns.append({"role": "assistant", "content": "INTERRUPTED by the user"})
            try:
                run._show_disk_once()
            except Exception as exc:  # the diff is a courtesy; the prompt must come back
                log.debug(f"diff after interrupt failed: {exc}")
            _print_status(outcome_code)
            carry = (
                "The user interrupted the previous task. Stop working on it; "
                "its last reply was not acted on. The new task follows."
            )
        mode_told = run.mode_seen
        changed = list(task_state.edits)
        slog.write("task_finished", code=outcome_code, task=task, changed=changed or None)
        _update_session_meta(slog)
        runtime.hook("task_finished", {"task": task, "status": outcome_code, "changed": changed})
        chat.history.append(
            f"{_one_line(task, 100)} -> {outcome_code}"
            + (f" (changed {', '.join(changed[:6])}{' ...' if len(changed) > 6 else ''})" if changed else "")
        )
    for pool in pool_box:
        try:
            pool.close()
        except Exception as exc:  # the session is over; a stuck tab must not hang the exit
            log.debug(f"closing helper tabs failed: {exc}")
    runtime.finish()
    if outcome is not None:
        outcome[:] = [outcome_code]
    return turns


def _current_mode(default: str, at_start: str | None = None) -> str:
    """``default`` (the session's mode), unless the UI switched to another one since ``at_start``."""
    live = _ui_approve_mode()
    if live in _MODES and live != at_start:
        return live
    return default if default in _MODES else "ask"


def _apply_role_change(chat: Any, reseed: Callable[[str], str] | None) -> str | None:
    """After ``/role``, the next task goes to a new chat seeded for that role. Returns the new role id."""
    taker = getattr(ui.current(), "take_role_change", None)
    if not callable(taker):
        return None
    try:
        changed = taker()
    except Exception:
        return None
    if not changed:
        return None
    if getattr(chat, "compactor", None) is not None:
        # A model API conversation is append-only (its prefix is cached): the new role's
        # instructions go in as a note with the next message instead of a new chat.
        text = load_role_instructions(changed)
        chat.pending_notes.append(
            prompt_build.change_note("role", f"{roles.role_label(changed)}." + (f"\n{text}" if text else ""))
        )
        return changed
    if reseed is None:
        return changed
    try:
        chat.seed = (reseed(changed) or "").strip()
    except Exception as exc:  # the old seed still works
        log.warn(f"could not rebuild the instructions for role {changed}: {exc}")
        return changed
    if chat.restart(f"the role is now {roles.role_label(changed)}"):
        return changed
    # No new chat: the next message carries the new instructions in front of it.
    chat.prefix = _without_ready(chat.seed) if chat.seed else ""
    _ui("note", "This chat page cannot open a new conversation; the new role goes in front of the next task.")
    return changed


# --------------------------------------------------------------------------- sessions and context
#
# The hooks between the loop and session_log / compaction / prompt_build (Stage 3). Stage 5
# moves this section, with _Chat and _TaskRun, into the engine core.


class _LoggedHooks:
    """A task run's hooks that also record tool calls in the session log."""

    def __init__(self, inner: Any, slog: Any) -> None:
        self.inner = inner
        self.slog = slog

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def ui(self, kind: str, message: str) -> None:
        self.inner.ui(kind, message)

    def status(self, code: str) -> None:
        self.inner.status(code)

    def tool_start(self, call: ToolCall) -> None:
        self.slog.write(
            "tool_started",
            tool=canonical_tool(call.tool) or call.tool,
            call_id=call.call_id or None,
            arguments=call.arguments,
            summary=_activity(call),
        )
        self.inner.tool_start(call)

    def tool_done(self, call: ToolCall, result: dict[str, Any]) -> None:
        name = canonical_tool(call.tool) or call.tool
        ok = bool(result.get("ok"))
        text = str(result.get("output") if ok else result.get("error") or "") or ""
        paths = _call_paths(call)
        self.slog.write(
            "tool_result",
            tool=name,
            call_id=call.call_id or None,
            ok=ok,
            summary=_one_line(text, 200),
            path=paths or None,
            mutating=(name in MUTATING) or None,
            denied=bool(result.get("denied")) or None,
            result=result,
        )
        self.inner.tool_done(call, result)


def _setup_model_api_session(chat: _Chat, options: dict[str, Any], slog: Any) -> None:
    """For a model API session: compaction by context pressure, and llama.cpp slot pinning.

    ``_context_window`` in ``options`` is the backend's context window (run_agent sets it for
    a local server). The web chat has none and keeps its size/turn/time rotation.
    """
    from critique_bot import compaction

    session = chat.session
    window = options.get("_context_window")
    if (
        isinstance(window, int)
        and not isinstance(window, bool)
        and window > 0
        and callable(getattr(session, "replace_history", None))
        and options.get("compaction") is not False
    ):
        from critique_bot.redaction import redact_outgoing

        chat.compactor = compaction.Compactor(
            compaction.Policy.from_settings(window, options),
            summarize=_side_summarizer(chat),
            put_blob=lambda text: chat.log.put_blob(text),
            redact=lambda text: redact_outgoing(text, report=False),
        )
    _pin_slot(session, slog.id if slog.enabled else "", options.get("slot_pinning"))


def _pin_slot(session: Any, key: str, setting: Any) -> int | None:
    """llama.cpp: keep the session in one slot (``id_slot``). Off with ``"slot_pinning": false``;
    a number is the server's slot count (no detection)."""
    pin = getattr(session, "pin_slot", None)
    if not callable(pin) or not key or setting is False:
        return None
    slots = setting if isinstance(setting, int) and not isinstance(setting, bool) and setting > 0 else None
    try:
        slot = pin(key, slots)
    except Exception as exc:  # the cache is an optimization; never stop a session over it
        log.debug(f"slot pinning failed: {exc}")
        return None
    if slot is not None:
        log.info(f"llama.cpp: this session uses slot {slot} (id_slot) so its prompt cache stays warm")
    return slot


def _side_summarizer(chat: _Chat) -> Callable[[list[dict[str, Any]], str], str] | None:
    """The compaction summary as one request on a second session that holds the same messages.

    The messages share the conversation's prefix, so a server with a prompt cache (and the
    same llama.cpp slot) reuses it. None without a second session.
    """
    if chat.side_factory is None:
        return None
    from critique_bot import compaction

    def summarize(messages: list[dict[str, Any]], prompt: str) -> str:
        assert chat.side_factory is not None
        handle = chat.side_factory()
        enter = getattr(handle, "__enter__", None)
        side = enter() if callable(enter) and not hasattr(handle, "send") else handle
        try:
            native = runtime.native_tools()
            if native and callable(getattr(side, "use_tools", None)):
                side.use_tools(native)
            _pin_slot(side, chat.log.id if chat.log.enabled else "", None)
            replace = getattr(side, "replace_history", None)
            if callable(replace):
                replace(messages)
                reply = side.send(prompt)
            else:
                rendered = "\n\n".join(f"[{m.get('role')}]\n{m.get('content') or ''}" for m in messages)
                reply = side.send(rendered + "\n\n" + prompt)
        finally:
            close = getattr(handle, "__exit__", None) if handle is not side else None
            try:
                if callable(close):
                    close(None, None, None)
                elif callable(getattr(side, "close", None)):
                    side.close()
            except Exception as exc:  # noqa: BLE001
                log.debug(f"closing the summary session failed: {exc}")
        return compaction.reply_text(reply)

    return summarize


def _compact_at_boundary(run: "_TaskRun") -> bool:
    """At a turn boundary of a model API session: compact when context pressure calls for it."""
    from critique_bot import compaction

    chat = run.chat
    session = chat.session
    history = getattr(session, "history", None)
    replace = getattr(session, "replace_history", None)
    if chat.compactor is None or not callable(history) or not callable(replace):
        return False
    detail = chat.last_detail if isinstance(chat.last_detail, dict) else {}
    used = int(detail.get("prompt_eval_count") or 0) + int(detail.get("eval_count") or 0)
    state = run.state
    facts = {
        "notes": "; ".join(run.findings),
        "changed": ", ".join(state.edits),
        "problems": run.last_output.strip()[-800:] if run.last_failed else "",
        "failing": f"{state.last_command} -> {state.last_exit}" if state.last_command and run.last_failed else "",
    }
    ctx = compaction.Context(
        used_tokens=used,
        pending_chars=len(run.payload),
        turn=chat.replies,
        task=run.task,
        todos=list(state.todos),
        recent_files=list(state.reads),
        workspace=run.ctx.workspace,
        facts=facts,
    )
    try:
        result = chat.compactor.compact(history(), ctx)
    except Exception as exc:  # a failed compaction leaves the conversation as it was
        log.warn(f"compaction failed: {exc}")
        return False
    if result is None:
        return False
    replace(result.messages)
    chat.log.write(
        "compaction",
        strategy=result.strategy,
        reason=result.reason,
        tokens_before=result.tokens_before,
        tokens_after=result.tokens_after,
        masked=result.masked or None,
        summarized=list(result.summarized) if result.summarized else None,
        summary=result.summary or None,
        helper=result.helper or None,
        history=result.messages if result.strategy == "summarize" else None,
    )
    chat.log.sync()
    run_stats.count("compactions")
    run_stats.count(f"compaction_{result.strategy}")
    what = "Hid old tool results" if result.strategy == "mask" else "Summarized the earlier turns"
    run.hooks.ui("note", f"{what} to save context: about {result.tokens_before} -> {result.tokens_after} tokens.")
    return True


def _resume_from_log(chat: _Chat, slog: Any) -> tuple[bool, str]:
    """Rebuild the conversation from the session log. (restored, a note for the first message).

    A model API session gets its message list back (``replace_history``), up to the last
    complete turn. A session without one (the web chat) starts a new chat whose first
    message carries a summary of the log.
    """
    events = slog.events()
    projection = session_log_mod.history_from_events(events, slog.folder)
    chat.history = projection.tasks[-8:]
    replace = getattr(chat.session, "replace_history", None)
    if projection.has_wire and projection.messages and callable(replace):
        replace(projection.messages)
        chat.map_sent = True
        slog.write("session_resumed", strategy="history", messages=len(projection.messages))
        note = "This session was resumed: crit restored the conversation from its log."
        if projection.unanswered_calls:
            note += " The last tool calls did not finish before crit stopped; check the state before repeating them."
        _ui("note", f"Resumed the conversation ({len(projection.messages)} messages).")
        return True, note
    summary = session_log_mod.resume_summary(events, slog.folder)
    slog.write("session_resumed", strategy="summary", messages=0)
    _ui("note", "Resumed: a new conversation starts with a summary of the session's log.")
    return False, summary


def _take_session_change(chat: _Chat, slog: Any) -> tuple[Any, str] | None:
    """After ``/resume`` or ``/fork`` in the UI: switch this conversation to that session's log.

    Returns (the new log, a note for the next message), or None when nothing was asked.
    """
    taker = getattr(ui.current(), "take_session_change", None)
    if not callable(taker):
        return None
    try:
        request = taker()
    except Exception:
        return None
    if not request:
        return None
    folder = Path(request.get("folder")) if isinstance(request, dict) and request.get("folder") else None
    if folder is None:
        return None
    try:
        new_log = session_log_mod.SessionLog.open(folder)
    except OSError as exc:
        _ui("bad", f"Could not open session {folder.name}: {exc}")
        return None
    if chat.can_restart:
        chat.restart(f"switching to session {folder.name}")  # recorded in the session being left
    slog.close(status="switched")
    chat.log = new_log
    restored, note = _resume_from_log(chat, new_log)
    chat.prefix = "" if restored else (_without_ready(chat.seed) if chat.seed else "")
    if not restored:
        chat.map_sent = False
    _announce_session(new_log)
    return new_log, note


def _announce_session(slog: Any) -> None:
    setter = getattr(ui.current(), "set_session", None)
    if callable(setter) and slog.enabled:
        try:
            setter(slog.folder.parent, slog.id)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"telling the UI about the session failed: {exc}")


def _open_session_log(
    home: BotHome,
    config: BotConfig,
    task: str,
    *,
    resume: str | None = None,
    settings: dict[str, Any] | None = None,
    kind: str = "agent",
) -> Any:
    """The session's event log in ``.crit/sessions``: a new one, or ``resume``'s (raises
    SessionNotFound). NullLog when ``"session_log": false`` or the folder cannot be written."""
    options = settings or {}
    if options.get("session_log") is False and not resume:
        return session_log_mod.NullLog()
    if resume:
        folder = session_log_mod.find_session(home.sessions_dir, resume)
        slog = session_log_mod.SessionLog.open(folder)
        tokens = slog.meta.get("tokens") if isinstance(slog.meta.get("tokens"), dict) else {}
        slog.meta["tokens_base"] = dict(tokens)
        return slog
    try:
        slog = session_log_mod.SessionLog.create(
            home.sessions_dir,
            title=task,
            kind=kind,
            model=config.model or "",
            backend=config.backend,
            cwd=str(home.root),
        )
    except OSError as exc:
        log.warn(f"no session log: {exc}")
        return session_log_mod.NullLog()
    slog.write(
        "session_started",
        id=slog.id,
        kind=kind,
        model=config.model or "",
        backend=config.backend,
        cwd=str(home.root),
        crit_version=slog.meta.get("crit_version") or None,
    )
    return slog


def _close_session_log(slog: Any, status: str) -> None:
    if not slog.enabled:
        return
    try:
        _update_session_meta(slog)
        slog.close(status=status)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"closing the session log failed: {exc}")


def _memory_notes(home: BotHome, settings: dict[str, Any]) -> str:
    """The memory files for the prefix (prompt_build.load_memory); a warning when over the cap."""
    if settings.get("memory_files") is False:
        return home.project_notes()
    try:
        memory = prompt_build.load_memory(home.root)
    except Exception as exc:  # the notes help; they must never stop a session
        log.warn(f"reading the memory files failed: {exc}")
        return home.project_notes()
    if memory.warning:
        _ui("note", memory.warning[:1].upper() + memory.warning[1:] + ".")
    return memory.text


def _session_settings(config: BotConfig, home: BotHome) -> dict[str, Any]:
    """Settings the loop takes from the run, not the settings file: the model API context window
    (compaction by pressure) and the first turn's session context (date, git status)."""
    values: dict[str, Any] = {"_first_turn_context": prompt_build.session_context(home.root)}
    if config.is_local:
        window = model_api.context_tokens(config)
        if window:
            values["_context_window"] = int(window)
    return values


def _update_session_meta(slog: Any) -> None:
    """Title, status, turns, and tokens in meta.json (what ``crit sessions`` lists)."""
    if not slog.enabled:
        return
    stats = run_stats.snapshot()
    base = slog.meta.get("tokens_base") if isinstance(slog.meta.get("tokens_base"), dict) else {}
    tokens = {
        key: int(base.get(key) or 0) + int(stats.get(name) or 0)
        for key, name in (("prompt", "prompt_tokens"), ("completion", "completion_tokens"), ("cached", "cached_tokens"))
    }
    slog.update_meta(turns=slog.turn, tokens=tokens)
    slog.sync()


def _task_skills(
    task: str,
    workspace: Path,
    chat: Any,
    settings: dict[str, Any],
    *,
    auto: bool,
    limit: int,
    role: str | None = None,
) -> str:
    """The SKILLS block for this task: pinned skills, the role's skills, and the ones that match, each sent once per chat."""
    select = getattr(agent_tools, "select_skills", None)
    if not callable(select):
        return ""
    pinned = _setting_list(settings, "skills")
    pinned += [name for name in roles.role_skills(role if role is not None else settings.get("role")) if name not in pinned]
    getter = getattr(ui.current(), "pinned_skills", None)
    if callable(getter):
        try:
            pinned += [name for name in getter() if name not in pinned]
        except Exception:
            pass
    try:
        chosen = select(task, workspace, pinned=pinned, limit=limit if auto else 0)
    except Exception as exc:  # skills help; they must never stop a task
        log.debug(f"choosing skills failed: {exc}")
        return ""
    sent = getattr(chat, "skills_sent", None)
    if not isinstance(sent, set):
        sent = set()
        try:
            chat.skills_sent = sent
        except Exception:
            pass
    fresh = [item for item in chosen if item["name"] not in sent]
    if chosen:
        _ui("note", "Skills: " + ", ".join(item["name"] for item in chosen))
    block = skills_block(fresh)
    if block:
        sent.update(item["name"] for item in fresh)
    elif chosen:
        block = "SKILLS: " + ", ".join(item["name"] for item in chosen) + " (sent earlier in this chat; still apply)."
    return block


def _stop_background(session_shell: Any) -> None:
    """Kill the commands still running in the shell session (Ctrl+C)."""
    if session_shell is None:
        return
    try:
        jobs = session_shell.jobs()
    except Exception as exc:
        log.debug(f"listing background jobs failed: {exc}")
        return
    for job in jobs or []:
        if isinstance(job, dict) and job.get("running"):
            try:
                session_shell.kill_background(job.get("id"))
            except Exception as exc:
                log.debug(f"killing background job failed: {exc}")


def _remove_old_codegraph() -> None:
    """Delete the CodeGraph copy earlier versions unpacked (crit's own folder; nothing else uses it)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        folder = base / "critique-bot" / "codegraph"
    elif sys.platform == "darwin":
        folder = Path.home() / "Library" / "Application Support" / "critique-bot" / "codegraph"
    else:
        folder = Path.home() / ".config" / "critique-bot" / "codegraph"
    if not folder.is_dir():
        return
    try:
        shutil.rmtree(folder)
        log.info(f"removed the old CodeGraph copy at {folder}")
    except OSError as exc:
        log.debug(f"could not remove {folder}: {exc}")


def _prepare_index(workspace: Path, index_path: Path | None, task: str) -> str:
    """Bring the index up to date with the disk and build the repo map."""
    if index_path is None or not Path(index_path).is_file():
        return ""
    try:
        code_index.refresh_index(workspace, index_path)
        return code_index.repo_map(index_path, keywords=_keywords(task), budget_chars=REPO_MAP_CHARS)
    except Exception as exc:  # the map is a hint; a broken index must not stop the task
        log.debug(f"index refresh failed: {exc}")
        return ""


#: Tools the reply to the instructions may run before there is a task.
_SEED_TOOLS = _PARALLEL_SAFE | {"skill"}


def _seed_needs_approval(name: str, call: ToolCall, ctx: ToolContext) -> bool:
    """True when a read-only call would still need the user (a path outside the workspace)."""
    check = getattr(agent_tools, "permission_for", None)
    if check is None:
        return False
    try:
        return (getattr(check(name, call.arguments, ctx), "kind", "read") or "read") != "read"
    except Exception:
        return True


def _seed_session(
    chat: Any,
    seed: str,
    turns: list[dict[str, str]],
    *,
    ctx: ToolContext,
    max_result_chars: int,
    show: Callable[[str], None],
) -> None:
    """Send the tool instructions alone. Read-only tool calls in the reply still run.

    Nothing that changes files or runs a command runs before there is a
    task, whatever name the model uses for it (write_file, patch, bash).
    """
    if not isinstance(chat, _Chat):
        chat = _Chat(chat)
    _ui("note", "Getting ready.")
    payload = seed
    turns.append({"role": "user", "content": seed})
    for _ in range(3):
        try:
            reply = chat.send(payload)
        except _ProviderFailed as exc:
            _ui("bad", str(exc))
            return
        reply, _fabricated = _cut_fabricated(reply)
        calls, unclosed = parse_tool_calls(reply)
        note = commentary(reply)
        if note and _status_code(note) is None and note.strip().upper() != "READY":
            visible = _hide_tool_markup(note)
            if visible:
                show(visible)
        turns.append({"role": "assistant", "content": reply})
        if unclosed or _idle_tool_reply(chat.last_detail, reply):
            payload = format_tool_result(
                {"tool": "", "ok": False, "error": "reply was truncated before the tool call closed; resend complete tool_call blocks"}
            )
            turns.append({"role": "tool", "content": payload})
            continue
        if not calls:
            return
        results = []
        for call in calls:
            name = canonical_tool(call.tool)
            if call.error:
                results.append({"tool": call.tool, "ok": False, "error": call.error})
            elif name and (name not in _SEED_TOOLS or _seed_needs_approval(name, call, ctx)):
                results.append(
                    {"tool": name, "ok": False, "error": "no task yet; wait for the task before changing files or running commands"}
                )
            else:
                _tool_start(call)
                result = _execute(call.tool, call.arguments, ctx)
                _tool_done(call, result)
                results.append(result)
        payload = _cap("\n".join(format_tool_result(item) for item in results), max_result_chars)
        turns.append({"role": "tool", "content": payload})


def run_agent(
    config: BotConfig,
    home: BotHome,
    task: str,
    *,
    max_rounds: int | None,
    output_dir: Path | None,
    headed: bool,
    approve_mode: str | None = None,
    role: str | None = None,
    resume: str | None = None,
) -> int:
    """Open the Edge session, run the tool loop, and write the transcript.

    ``approve_mode`` is ``"ask"``, ``"edits"``, ``"auto"`` (``--yes``), or ``"plan"``
    (``--plan``); ``None`` reads ``"permissions"`` from ``.crit/settings.json``. ``role`` is the
    persona for this run (``--role``); ``None`` reads ``"role"`` from the settings, and Generic
    is the default. The transcript is saved even when Ctrl+C or an error ends the session.

    Every session is logged in ``.crit/sessions/<id>/`` (session_log). ``resume`` is the id
    (or a prefix, or ``last``) of a logged session to continue instead of starting a new one.
    """
    started = datetime.now(timezone.utc)
    run_stats.reset()
    turns: list[dict[str, str]] = []
    outcome = ["COMPLETED"]
    settings = effective_settings(home.settings)
    try:
        slog = _open_session_log(home, config, task, resume=resume, settings=settings)
    except session_log_mod.SessionNotFound as exc:
        log.notice(f"Problem: {exc}")
        return 1
    helpers = min(_int_setting(settings, "helper_sessions", DEFAULT_HELPER_SESSIONS, 0), MAX_HELPER_SESSIONS)
    extensions = _load_extensions(home.root, settings)
    setup = _tool_setup(settings, helpers=helpers, web=bool(_web_hosts(settings)), native_ok=model_api.native_tools_ok(config),
                        extensions=extensions)
    try:
        instructions = load_agent_instructions(setup.tool_format)
    except OSError as exc:
        log.error(str(exc))
        log.notice(f"Problem: {exc}")
        if extensions is not None:
            extensions.close()
        return 1
    if settings.get("seed_instructions") is False:
        instructions = ""
    role_id = _resolve_role(role, settings, home.root)
    if approve_mode not in _MODES:
        approve_mode = ui_api.normalize_mode(settings.get("permissions") or settings.get("mode"))
    notes = home.project_notes()
    check_command = resolve_check_command(settings, notes)
    memory = _memory_notes(home, settings)
    if helpers and config.max_parallel_tabs < helpers + 1:
        # Extra tabs are opened over the browser's remote debugging port, which this turns on.
        config = dataclasses.replace(config, max_parallel_tabs=helpers + 1)
    _prewarm_shell_environment()
    shell = agent_shell.detect_shell(preference=_shell_preference(settings))
    available = _available_shells()
    offline = settings.get("offline") is True
    session_shell = _open_shell_session(home.root, shell, available, offline=offline)
    sandbox_note = agent_shell.use_sandbox(session_shell, settings, default=getattr(config, "sandbox", None), offline=offline)
    taint.start_session(untrusted=settings.get("untrusted_workspace") is True, workspace=home.root)
    ui.current().configure(
        workspace=home.root,
        model=_model_label(config),
        shell=shell,
        approve_mode=approve_mode,
        theme=effective_theme(settings),
        history_path=home.bot_dir / "history",
        cache_dir=home.cache_dir,
        session_shell=session_shell,
        tools=setup.names or (setup.registry.names() if setup.registry is not None else ALLOWED_TOOLS),
        role=role_id,
    )
    ui.current().welcome_header()
    for line in (extensions.notes if extensions is not None else []):
        _ui("note", line)
    started_hook = _session_start_hook(extensions, setup)
    if started_hook:
        notes = (notes + "\n\n" + started_hook).strip()
    ui.current().install_log_bridge()
    if sandbox_note:
        _ui("work", sandbox_note)
    _announce_session(slog)
    try:
        skill_names = [item["name"] for item in agent_tools.discover_skills(home.root)]
    except Exception as exc:
        log.debug(f"listing skills failed: {exc}")
        skill_names = []

    def build_seed(chosen_role: str) -> str:
        return seed_message(
            home.root,
            instructions,
            shell=shell,
            notes=memory,
            available=available,
            skills=skill_names,
            helpers=helpers,
            web_hosts=_web_hosts(settings),
            offline=settings.get("offline") is True,
            role=load_role_instructions(chosen_role, home.root),
            tools=setup.tools_block,
        )

    seed = build_seed(role_id)
    code = 1
    previous_registry = tool_api.use_registry(setup.registry) if setup.registry is not None else None
    try:
        _remove_old_codegraph()
        code = _run_session(
            config,
            home,
            task,
            headed=headed,
            loop_kwargs={
                "workspace": home.root,
                "index_path": home.index_path,
                "cache_dir": home.cache_dir,
                "max_rounds": max_rounds,
                "max_result_chars": _result_budget(config, settings),
                "seed": seed,
                "outcome": outcome,
                "check_command": check_command,
                "shell": shell,
                "approve_mode": approve_mode,
                "session_shell": session_shell,
                "turns": turns,
                "settings": {**_loop_settings(config, settings), "tool_format": setup.tool_format,
                             "_native_tools": setup.native_tools, "_extensions": extensions, **_session_settings(config, home)},
                "ask_user": _ask_user,
                "helper_count": helpers,
                "role": role_id,
                "reseed": build_seed,
                "session_log": slog,
                "resume": bool(resume),
            },
            turns=turns,
        )
    except KeyboardInterrupt:
        outcome[:] = ["INTERRUPTED"]
        ui.current().shutdown()
        _print_status("INTERRUPTED")
        code = 130
    finally:
        if previous_registry is not None:
            tool_api.use_registry(previous_registry)
        if extensions is not None:
            extensions.close()
        ui.current().shutdown()
        if session_shell is not None:
            try:
                session_shell.close()
            except Exception as exc:
                log.warn(f"closing the shell session failed: {exc}")
        _save_transcript(
            config,
            home,
            turns,
            started=started,
            output_dir=output_dir,
            commands=list(getattr(session_shell, "history", []) or []),
            folder=slog.folder if slog.enabled else None,
        )
        _close_session_log(slog, outcome[-1] if code in (0, 1) else ("INTERRUPTED" if code == 130 else "FAILED"))
    if code != 0:
        return code
    return 0 if outcome[-1] in _STATUS_OK else 1


#: The "minimal" tool set: the shell does the searching, listing, and git; files are read and
#: edited with tools that number lines, check syntax, and can be undone.
MINIMAL_TOOLS = (
    "run_command",
    "command_output",
    "command_input",
    "kill_command",
    "read_files",
    "edit_file",
    "write_files",
    "todo",
    "ask_user",
)


@dataclass(frozen=True)
class _ToolSetup:
    tool_format: str
    #: The tools block for the first message ("" with the tag format, whose prompt lists the tools).
    tools_block: str
    #: The registry to install for the session, or None for the built-in one.
    registry: Any
    #: Native format: the tools in OpenAI chat format, handed to each session (use_tools).
    native_tools: list[dict[str, Any]] | None = None
    #: The tools the model is told about (for /tools); () when that is the registry's list.
    names: tuple[str, ...] = ()


def _tool_setup(
    settings: dict[str, Any],
    *,
    helpers: int,
    web: bool,
    only: Iterable[str] | None = None,
    native_ok: bool = False,
    extensions: Any = None,
) -> _ToolSetup:
    """The tool format, the tool set, and the tools block, from ``tool_format`` and ``tool_set``.

    ``native_ok``: the backend calls tools natively (a local model server). Then native is the
    default; ``"tool_format": "openai"`` or ``"tags"`` still picks a text format.
    """
    raw = str(settings.get("tool_format") or "").strip().lower()
    fmt = tool_format_of(settings, DEFAULT_TOOL_FORMAT)
    if native_ok and raw in {"", "auto"}:
        fmt = TOOL_FORMAT_NATIVE
    elif fmt == TOOL_FORMAT_NATIVE and not native_ok:
        fmt = TOOL_FORMAT_OPENAI  # the web chat has no native tool calls
    minimal = str(settings.get("tool_set") or "").strip().lower() == "minimal"
    names = list(only) if only is not None else list(MINIMAL_TOOLS if minimal else ALLOWED_TOOLS)
    if helpers < 1 and "delegate" in names:
        names.remove("delegate")
    if not web and "web_fetch" in names:
        names.remove("web_fetch")
    registry = tool_api.builtin_registry().only(names) if (minimal or only is not None) else None
    block = ""
    native: list[dict[str, Any]] | None = None
    source = registry or tool_api.builtin_registry().only(names)
    if extensions is not None and extensions.active():
        # Manifest and MCP tools, hooks, and the agents delegate can start: one merged registry
        # (extensions/session.py), decided now and kept for the whole session.
        source = extensions.registry(source)
        registry = extensions.registry(registry or tool_api.builtin_registry())
        if fmt == TOOL_FORMAT_TAGS:
            block = extensions.tools_text()
    if fmt == TOOL_FORMAT_OPENAI:
        from critique_bot import openai_format

        block = openai_format.tools_block(source.openai_tools())
    elif fmt == TOOL_FORMAT_NATIVE:
        # Sorted by name, so the request's tools are byte-identical turn after turn (prompt cache).
        native = sorted(source.openai_tools(), key=lambda tool: str((tool.get("function") or {}).get("name")))
    names_now = source.names() if extensions is not None and extensions.active() else ()
    return _ToolSetup(fmt, block, registry, native, names_now)


def _load_extensions(root: Path, settings: dict[str, Any]) -> Any:
    """This session's extension tools, MCP servers, hooks and agents (extensions.load_session); None on failure."""
    from critique_bot import extensions

    try:
        return extensions.load_session(root, settings, builtin_names=ALLOWED_TOOLS)
    except Exception as exc:  # extensions are optional: crit runs without them
        log.warn(f"extensions not loaded: {exc}")
        return None


def _session_start_hook(extensions: Any, setup: _ToolSetup) -> str:
    """Run the session_start hooks; the context they add goes into the project notes."""
    if extensions is None:
        return ""
    try:
        decision = extensions.run_hook("session_start", {"tools": list(setup.names or ())})
    except Exception as exc:
        log.warn(f"session_start hook failed: {exc}")
        return ""
    return decision.append_context


def run_read_only_task(
    config: BotConfig,
    home: BotHome,
    task: str,
    *,
    answer: AnswerContract,
    role: str,
    max_rounds: int,
    headed: bool,
) -> tuple[int, list[dict[str, str]]]:
    """Run one read-only task (a review) and return (exit code, turns).

    The model reads the project and git history with tools; edits, commands, fetches,
    helper chats, and paths outside the project are refused without asking, so no
    prompt waits for a person and the input cannot make crit act. The task ends on a
    reply that ``answer`` accepts. The turns are saved as a transcript in
    ``.crit/sessions`` as an agent run is.
    """
    started = datetime.now(timezone.utc)
    turns: list[dict[str, str]] = []
    outcome = ["COMPLETED"]
    settings = effective_settings(home.settings)
    slog = _open_session_log(home, config, task, settings=settings, kind="review")
    setup = _tool_setup(settings, helpers=0, web=False, only=sorted(READ_ONLY_TOOLS), native_ok=model_api.native_tools_ok(config))
    try:
        instructions = load_agent_instructions(setup.tool_format)
    except OSError as exc:
        log.error(str(exc))
        log.notice(f"Problem: {exc}")
        return 1, turns
    if settings.get("seed_instructions") is False:
        instructions = ""
    shell = agent_shell.detect_shell(preference=_shell_preference(settings))
    ui.current().configure(
        workspace=home.root,
        model=_model_label(config),
        shell=shell,
        approve_mode="plan",
        theme=effective_theme(settings),
        cache_dir=home.cache_dir,
        tools=sorted(READ_ONLY_TOOLS),
        role=role,
    )
    ui.current().install_log_bridge()
    seed = seed_message(
        home.root,
        instructions,
        shell=shell,
        notes=_memory_notes(home, settings),
        helpers=0,
        offline=True,
        role=load_role_instructions(role),
        tools=setup.tools_block,
    )
    code = 1
    try:
        code = _run_session(
            config,
            home,
            task,
            headed=headed,
            loop_kwargs={
                "workspace": home.root,
                "index_path": home.index_path,
                "cache_dir": home.cache_dir,
                "max_rounds": max_rounds,
                "max_result_chars": _result_budget(config, settings),
                "seed": seed,
                "outcome": outcome,
                "check_command": None,
                "shell": shell,
                "approve_mode": "plan",
                "turns": turns,
                "settings": {**_loop_settings(config, settings), "tool_format": setup.tool_format,
                             "_native_tools": setup.native_tools},
                "ask_user": lambda _question: "",
                "helper_count": 0,
                "role": role,
                "read_only": True,
                "answer": answer,
                "single_task": True,
                "session_log": slog,
            },
            turns=turns,
        )
    except KeyboardInterrupt:
        outcome[:] = ["INTERRUPTED"]
        _print_status("INTERRUPTED")
        code = 130
    finally:
        ui.current().shutdown()
        _save_transcript(config, home, turns, started=started, output_dir=None, folder=slog.folder if slog.enabled else None)
        _close_session_log(slog, outcome[-1])
    if code == 0 and outcome[-1] not in _STATUS_OK:
        code = 1
    return code, turns


def _model_label(config: BotConfig) -> str:
    """What the header and /status call the model: the name, and where it runs when local."""
    return model_api.model_label(config)


def _load_rules(options: dict[str, Any]) -> Any:
    from critique_bot import agent_rules

    rules, problems = agent_rules.load_rules(options)
    for problem in problems:
        log.warn(f".crit/settings.json: {problem}")
    return rules or None


def _loop_settings(config: BotConfig, settings: dict[str, Any]) -> dict[str, Any]:
    """The settings the loop reads, with chat-size defaults that fit a local model's context window.

    The browser defaults (a 300000-character chat, 40000-character tool results) assume a
    hosted model with a large context. A local model has ``local.num_ctx`` tokens; a chat
    bigger than that is silently truncated, so a new chat starts well before that.
    """
    num_ctx = model_api.context_tokens(config)
    if num_ctx is None:
        return settings
    merged = dict(settings)
    # about 3 characters per token of code; leave room for the reply and the instructions
    merged.setdefault("compact_after_chars", max(10_000, int(num_ctx * 2)))
    merged.setdefault("max_result_chars", max(4_000, min(DEFAULT_RESULT_CHARS, int(num_ctx / 2))))
    return merged


def _resolve_role(flag: str | None, settings: dict[str, Any], root: Path | None = None) -> str:
    """The role id for this run: ``--role``, then ``"role"`` in settings, then Generic. Unknown names warn.

    The name of a file-defined agent (``/agent``) counts too: its instructions become the role.
    """
    for source, raw in (("--role", flag), ('settings "role"', settings.get("role"))):
        if raw is None or str(raw).strip() == "":
            continue
        found = roles.normalize_role(raw)
        if found is not None:
            return found
        if root is not None and _file_agent(raw, root) is not None:
            return str(raw).strip()
        log.warn(f"{source}: {raw!r} is not one of {', '.join(roles.ROLE_IDS)}; using {roles.GENERIC}")
    return roles.GENERIC


_SHELL_SETTINGS = ("auto", "pwsh", "powershell", "cmd", "bash", "sh", "zsh", "gitbash", "git-bash")


def _shell_preference(settings: dict[str, Any]) -> str:
    """The ``"shell"`` setting (auto, pwsh, powershell, cmd, bash, sh, zsh); auto when unset or unknown."""
    value = str(settings.get("shell") or "auto").strip().lower()
    if value not in _SHELL_SETTINGS:
        log.warn(f'settings "shell": {value!r} is not one of {", ".join(_SHELL_SETTINGS[:7])}; using auto')
        return "auto"
    return value


def _prewarm_shell_environment() -> None:
    """Start reading the login-shell environment now, so the first command does not wait."""
    try:
        agent_shell.prewarm_login_environment()
    except Exception as exc:  # noqa: BLE001
        log.warn(f"login environment: {exc}")


def _available_shells() -> dict[str, agent_shell.Shell]:
    try:
        return agent_shell.available_shells()
    except Exception as exc:  # noqa: BLE001
        log.warn(f"listing shells failed: {exc}")
        return {}


def _open_shell_session(
    workspace: Path,
    shell: agent_shell.Shell,
    available: dict[str, agent_shell.Shell] | None = None,
    *,
    offline: bool = False,
) -> Any:
    """A persistent ShellSession when agent_shell provides one, else ``None``."""
    factory = getattr(agent_shell, "ShellSession", None)
    if factory is None:
        return None
    try:
        return factory(workspace, shell, available=available, offline=offline)
    except TypeError:
        return factory(workspace, shell, available=available)
    except Exception as exc:
        log.warn(f"shell session unavailable: {exc}")
        return None


def _loop_kwargs(candidates: dict[str, Any]) -> dict[str, Any]:
    """Only the keyword arguments ``run_agent_loop`` accepts (it gains new ones over time)."""
    import inspect

    try:
        accepted = set(inspect.signature(run_agent_loop).parameters)
    except (TypeError, ValueError):
        return candidates
    return {key: value for key, value in candidates.items() if key in accepted}


def _run_session(
    config: BotConfig,
    home: BotHome,
    task: str,
    *,
    headed: bool,
    loop_kwargs: dict[str, Any],
    turns: list[dict[str, str]],
) -> int:
    """One browser session. A Cloudflare block in headless mode retries once, headed."""
    from critique_bot.model_api import ModelError, open_provider

    kwargs = _loop_kwargs(loop_kwargs)
    try:
        with open_provider(config, headed=headed) as provider:
            with provider.session() as session:
                if kwargs.get("helper_count") and getattr(provider, "can_parallelize", False):
                    kwargs["helper_factory"] = lambda: provider.session(isolated=True)
                elif kwargs.get("helper_count"):
                    # Without remote debugging the helpers take turns in one extra tab on this thread.
                    log.info("helper chats run one at a time in a second tab (no remote debugging)")
                    kwargs["helper_factory"] = lambda: provider.session()
                    kwargs["helper_inline"] = True
                if config.is_local and "side_factory" in _loop_kwargs({"side_factory": None}):
                    # One-off requests on the same model (the compaction summary): another session.
                    kwargs["side_factory"] = lambda: provider.session()
                try:
                    result = run_agent_loop(session, first_task=task, **kwargs)
                    if "turns" not in kwargs and result is not turns:
                        turns[:] = list(result or [])
                except Exception:
                    model_api.save_failure(session, home.sessions_dir)
                    raise
    except ChatError as exc:
        if not headed and "Cloudflare" in str(exc):
            _ui("note", "The headless window was blocked. Opening a visible browser.")
            return _run_session(config, home, task, headed=True, loop_kwargs=loop_kwargs, turns=turns)
        log.error(str(exc))
        ui.current().shutdown()
        log.notice(f"Problem: {exc}")
        return 1
    except ModelError as exc:
        log.error(str(exc))
        ui.current().shutdown()
        log.notice(f"Problem: {exc}")
        return 1
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        log.exception(f"unexpected failure: {exc}")
        ui.current().shutdown()
        log.notice(f"crit stopped: {exc}. {log.details_hint()}")
        return 1
    return 0


def _save_transcript(
    config: BotConfig,
    home: BotHome,
    turns: list[dict[str, str]],
    *,
    started: datetime,
    output_dir: Path | None,
    commands: list[dict[str, Any]] | None = None,
    folder: Path | None = None,
) -> None:
    """``agent.json`` / ``agent.md`` for this run: in the session's folder (``folder``), else in a
    folder named after the start time. ``crit eval`` reads ``turns`` and ``stats`` from it."""
    if not turns:
        return
    finished = datetime.now(timezone.utc)
    # The transcript holds what went to the chat: secrets removed, as they were from the messages.
    turns = [{**turn, "content": agent_tools.redact_secrets(str(turn.get("content") or ""))[0]} for turn in turns]
    body = format_transcript(turns)
    payload = {
        "mode": "agent",
        "model": config.model,
        "backend": config.backend,
        "url": config.url,
        "workspace": str(home.root),
        "response": body,
        "turns": turns,
        "started_at": isoformat(started),
        "finished_at": isoformat(finished),
    }
    if commands:
        payload["commands"] = commands
    payload["stats"] = run_stats.snapshot()
    stamp = started.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        write_output(folder or home.sessions_dir / stamp, body, payload, stem="agent", print_body=False)
        if output_dir is not None:
            write_output(output_dir, body, payload, stem="agent", print_body=False)
    except OSError as exc:
        log.warn(f"could not save the transcript: {exc}")


def _result_budget(config: BotConfig, settings: dict[str, Any]) -> int:
    """Characters per message sent back to the chat. Smaller keeps the task in view."""
    raw = _loop_settings(config, settings).get("max_result_chars")
    if isinstance(raw, int) and raw >= 4_000:
        return raw
    return min(DEFAULT_RESULT_CHARS, config.max_prompt_chars)


def format_transcript(turns: list[dict[str, str]]) -> str:
    headings = {"user": "You", "assistant": "Assistant", "tool": "Tool"}
    parts = ["# Agent", ""]
    for turn in turns:
        parts.append(f"## {headings.get(turn.get('role', ''), 'Note')}")
        parts.append("")
        parts.append(turn.get("content", "").rstrip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


# --------------------------------------------------------------------------- permission rules

def _rule_denied(ctx: Any, perm: Any) -> str:
    """Why a saved deny rule refuses this step, or ""."""
    rules = getattr(ctx, "rules", None)
    if not rules:
        return ""
    from critique_bot import agent_rules

    tool = str(getattr(perm, "tool", "") or "")
    rule = agent_rules.denied_by(
        rules,
        tool=tool,
        perm_kind=str(getattr(perm, "kind", "") or ""),
        command=str(getattr(perm, "detail", "") or "") if tool in {"run_command", "command_input"} else "",
        paths=tuple(getattr(perm, "paths", ()) or ()),
        workspace=ctx.workspace,
    )
    return f"blocked by the deny rule {rule} in .crit/settings.json" if rule is not None else ""


#: Risks a saved allow rule may answer: a read or write outside the workspace that the user
#: allowed by path. Network commands, risky commands, secret files, and files the task wrote
#: keep asking whatever the rules say.
_RULE_ANSWERABLE_RISKS = frozenset({agent_tools.OUTSIDE_READ_RISK, "changes files outside the workspace"})


def _rule_allows(ctx: Any, perm: Any, risky: str) -> bool:
    rules = getattr(ctx, "rules", None)
    if not rules:
        return False
    kind = str(getattr(perm, "kind", "") or "")
    tool = str(getattr(perm, "tool", "") or "")
    if kind == "network" or not tool:
        return False
    if kind in {"command", "edit"} and risky:
        return False
    if kind == "outside" and (tool == "run_command" or risky not in _RULE_ANSWERABLE_RISKS):
        return False
    from critique_bot import agent_rules

    rule = agent_rules.allowed_by(
        rules,
        tool=tool,
        perm_kind=kind,
        command=str(getattr(perm, "detail", "") or "") if tool in {"run_command", "command_input"} else "",
        paths=tuple(getattr(perm, "paths", ()) or ()),
        workspace=ctx.workspace,
    )
    if rule is not None:
        log.debug(f"allowed by rule {rule}")
    return rule is not None


def _save_allow_rule(ctx: Any, rule: str) -> None:
    """Remember ``rule`` for this session and in .crit/settings.json, so it holds after a restart."""
    from critique_bot import agent_rules, project_home

    ctx.rules = agent_rules.with_allow(getattr(ctx, "rules", None) or agent_rules.Rules(), rule)
    try:
        home = project_home.find_bot_home(ctx.workspace)
        if home is not None:
            project_home.add_allow_rule(home, rule)
    except Exception as exc:  # the session still remembers it
        log.warn(f"could not save the rule {rule!r}: {exc}")


def _read_only_command(command: str) -> bool:
    check = getattr(agent_tools, "read_only_command", None)
    try:
        return bool(callable(check) and command and check(command))
    except Exception as exc:  # a broken check asks rather than runs
        log.debug(f"read-only check failed: {exc}")
        return False


def __getattr__(name: str) -> Any:
    # ``agent.agent_ui``: the UI installed now (engine.ui.current()).
    if name == "agent_ui":
        return ui.current()
    raise AttributeError(name)
