# SPDX-License-Identifier: Apache-2.0
"""When a task stops: finish codes, the project check, refusals, the answer contract, budgets.

COMPLETED is checked before it is accepted: background jobs still running, a project
check after edits, a change that was asked for but never made, a failure with no reason.
"""

from __future__ import annotations

import sys
from typing import Any

from critique_bot import log
from critique_bot.engine import dispatch, ui
from critique_bot.engine.parse import _STATUS_OK, ToolCall, _hide_tool_markup
from critique_bot.engine.prompts import _FALLBACKS
from critique_bot.engine.replies import _failure_reason, _has_checks, _asks_for_change
from critique_bot.engine.text import _one_line
from critique_bot.engine.ui import _REVIEW_LINES, _show_diff_in_ui
from critique_bot.tool_api import is_git_repo


MAX_REFUSALS = 3


MAX_FAILED_ROUNDS = 8


#: Replies in a row whose every call the user denied end the task as BLOCKED.
MAX_DENIED_ROUNDS = 3


MAX_TRUNCATIONS = 4


MAX_CHECK_CYCLES = 2


MAX_USER_QUESTIONS = 2


DEFAULT_CHECK_TIMEOUT = 600


class ProgressWatch:
    """The no-progress stop (Stage 7): model turns since the failing-test count last changed.

    The count is what the test runs show (the model's own and the harness's). Until a first
    count is seen, nothing stalls: the state's token budget is the limit then.
    """

    DEFAULT_TURNS = 12

    def __init__(self, limit: int = DEFAULT_TURNS) -> None:
        self.limit = max(1, int(limit))
        self.failing: int | None = None
        self.turns = 0

    def seen(self, failing: int) -> None:
        if failing != self.failing:
            self.failing = failing
            self.turns = 0

    def turn(self) -> None:
        if self.failing is not None:
            self.turns += 1

    def stalled(self) -> bool:
        return self.failing is not None and self.failing > 0 and self.turns >= self.limit

    def reset(self) -> None:
        self.turns = 0


class StateBudgets:
    """Estimated tokens (characters / 4) per flow state, and the limit of each.

    Defaults below; ``"flow_budgets": {"reproduce": 40000, "fix": 400000, "review": 80000}``
    in the settings, or the agent file's ``budget:`` with the same keys, change them.
    """

    DEFAULTS = {"REPRODUCE": 40_000, "FIX": 400_000, "REVIEW": 80_000}

    def __init__(self, limits: dict[str, int] | None = None) -> None:
        self.limits = dict(self.DEFAULTS)
        for key, value in (limits or {}).items():
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                self.limits[str(key).upper()] = value
        self.used: dict[str, int] = {}

    @classmethod
    def from_settings(cls, settings: dict[str, Any], *, extra: dict[str, int] | None = None) -> "StateBudgets":
        raw = settings.get("flow_budgets") if isinstance(settings, dict) else None
        merged: dict[str, int] = dict(raw) if isinstance(raw, dict) else {}
        merged.update({key: value for key, value in (extra or {}).items() if str(key).upper() in cls.DEFAULTS})
        return cls(merged)

    def spend(self, state: str, tokens: int) -> None:
        self.used[state] = self.used.get(state, 0) + max(0, int(tokens))

    def limit(self, state: str) -> int | None:
        return self.limits.get(state)

    def over(self, state: str) -> int | None:
        """The tokens used when ``state`` is past its limit, else None."""
        limit = self.limits.get(state)
        used = self.used.get(state, 0)
        return used if limit is not None and used > limit else None


class StopRules:
    """Finishing a task run (mixed into _TaskRun)."""

    def _refuse(self, reply: str) -> str | None:
        """Send a refusal back. FAILED/BLOCKED does not finish the task when the model only claims the tools are missing."""
        self.refusals += 1
        self.chat.strike()
        if self.refusals > MAX_REFUSALS:
            return self._end("FAILED", f"stopped working on the task: {_one_line(_hide_tool_markup(reply), 160)}")
        self.hooks.ui("note", "Still working on it.")
        self._send_message("RECOVER" if self.tools_ran else "NUDGE")
        return None

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
        flow = getattr(self, "flow", None)
        if status in _STATUS_OK and flow is not None:
            from critique_bot.engine.flows import PASS

            verdict = flow.on_complete()
            if verdict is not PASS:
                return verdict
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

    def _run_check(self) -> dict[str, Any]:
        """The project check. It always runs again after a change, with its own long timeout.

        It goes through the same approval as any command: the check command can come from
        a file in the repository (``.crit/AGENT.md``), so it is not trusted on its own.
        """
        return self._run_harness_command(self.check_command or "", label="")

    def _run_harness_command(self, command: str, *, label: str) -> dict[str, Any]:
        """A command crit runs itself (the check, a repro, a test target), in the workspace, approved
        like any command, with the check's long timeout. The model's shell cwd is left as it was."""
        self.hooks.ui("work", f"Checking with {_one_line(command, 60)}" if not label
                      else f"Running {label}: {_one_line(command, 60)}")
        call = ToolCall(
            "run_command",
            {"command": command, "timeout": self.check_timeout, "cwd": str(self.ctx.workspace)},
        )
        approved, reason = self._approved(self._permission(call))
        if not approved:
            result = {
                "tool": "run_command",
                "ok": False,
                "error": f"the user did not approve {'the check command' if not label else label}"
                + (f": {reason}" if reason else ""),
                "denied": True,
            }
            self.last_output = result["error"]
            return result
        session = self.ctx.session
        before = getattr(session, "cwd", None)
        self.hooks.tool_start(call)
        try:
            result = dispatch._execute(call.tool, call.arguments, self.ctx)
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
        result = dispatch._execute("git_diff", {"stat": True}, self.ctx)
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
