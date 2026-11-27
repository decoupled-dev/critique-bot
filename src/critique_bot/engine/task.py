"""One task on an open chat: send, read the reply, run its tools, repeat until a finish.

The turn loop. :class:`_TaskRun` keeps a task's working state; the halves that run tools
(:mod:`dispatch`), follow the mode (:mod:`modes`) and decide when to stop
(:mod:`stop_rules`) are mixed in.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from critique_bot import log, run_stats, tool_api
from critique_bot.engine import runtime, ui
from critique_bot.engine.chat import _Chat, _ProviderFailed
from critique_bot.engine.context import _compact_at_boundary
from critique_bot.engine.contracts import AnswerContract
from critique_bot.engine.dispatch import Dispatch
from critique_bot.engine.modes import Modes
from critique_bot.engine.parse import (
    _BLOCK_RE,
    _OPEN_RE,
    _STATUS_OK,
    ToolCall,
    _answer_text,
    _cut_call_label,
    _cut_fabricated,
    _hide_tool_markup,
    _openai_reply,
    commentary,
    format_tool_result,
    parse_tool_calls,
)
from critique_bot.engine.prompts import _RESPONSES_WIRE, TOOL_FORMAT_TAGS
from critique_bot.engine.replies import (
    MAX_HAND_BACKS,
    _answer_only,
    _asks_user,
    _diffstat,
    _hands_back,
    _idle_tool_reply,
    _no_edit_needed,
    _only_promises,
    _plan_text,
    _real_question,
    _refuses,
    _stalls,
    _status_code,
)
from critique_bot.engine.stop_rules import (
    DEFAULT_CHECK_TIMEOUT,
    MAX_DENIED_ROUNDS,
    MAX_FAILED_ROUNDS,
    MAX_REFUSALS,
    MAX_TRUNCATIONS,
    MAX_USER_QUESTIONS,
    StopRules,
)
from critique_bot.engine.text import _cap, _one_line
from critique_bot.engine.ui import _present_text, _print_status, _tool_done, _tool_start, _ui
from critique_bot.tool_api import TaskState, ToolContext, fill


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


class _TaskRun(Dispatch, Modes, StopRules):
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
        self.ui_mode_at_start = ui_mode_at_start if ui_mode_at_start is not None else ui._ui_approve_mode()
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


    # ----------------------------------------------------------------- running calls


    # ----------------------------------------------------------------- finishing


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
