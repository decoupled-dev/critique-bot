"""One task, several chat tabs: the coordinating session hands self-contained briefs to helper tabs.

The tabs cannot see each other's chats; crit is what they share. Every helper
works on the same folder through crit's tools, reads anything, changes only
the files its brief was given (no two helpers get the same file), and runs no
builds. Its report goes back to the coordinating session as the delegate
call's result, which then reviews the changes and runs the build once.

Each helper tab lives on its own thread for the whole session: Playwright's
sync API belongs to the thread that opened it, and a tab that stays open
skips the page load and the instructions on the next brief.
"""

from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from critique_bot import agent_tools, log
from critique_bot.agent_tools import TaskState, ToolContext, canonical_tool

#: Rounds one brief may take before its helper stops and reports.
HELPER_MAX_ROUNDS = 40
#: Characters of each helper's report passed back to the coordinator.
REPORT_CHARS = 6_000


@dataclass
class Report:
    name: str
    status: str
    text: str
    changed: list[str] = field(default_factory=list)
    rounds: int = 0
    seconds: float = 0.0
    error: str = ""


def _agent():
    from critique_bot import agent

    return agent


class _QuietHooks:
    """A helper's progress goes to one callback line; nothing else reaches the terminal."""

    def __init__(self, name: str, progress: Callable[[str], None] | None) -> None:
        self.name = name
        self.progress = progress

    def _say(self, text: str) -> None:
        if self.progress is not None:
            try:
                self.progress(f"{self.name}: {text}")
            except Exception:  # noqa: BLE001 - the UI never stops a helper
                pass

    def ui(self, kind: str, message: str) -> None:
        if kind in {"bad", "good"}:
            self._say(message)

    def tool_start(self, call: Any) -> None:
        self._say(_agent()._activity(call))

    def tool_done(self, call: Any, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self._say("failed: " + str(result.get("error") or "")[:120])

    def status(self, code: str) -> None:
        self._say(code.lower())


def _helper_run_class():
    agent = _agent()

    class HelperRun(agent._TaskRun):
        """A task run that never asks: it may read anything and change only its own files."""

        def __init__(self, *args: Any, allowed: set[str], **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.allowed = allowed
            self._call: Any = None
            self.plan_done = True
            self.reviewed = True  # the coordinator shows the diff once, at the end
            self.verify_asked = True  # helpers do not build; the coordinator verifies
            self.nothing_nudged = not allowed

        def _approve_mode(self) -> str:
            return "auto"

        def _planning(self) -> bool:
            return False

        def _permission(self, call: Any) -> Any:
            self._call = call
            return super()._permission(call)

        def _approved(self, perm: Any) -> tuple[bool, str]:
            call = self._call
            name = canonical_tool(getattr(call, "tool", "")) if call is not None else ""
            kind = getattr(perm, "kind", "read") or "read"
            if name == "delegate":
                return False, "a helper cannot delegate; do the brief yourself"
            if kind in {"read", "network"}:
                return True, ""
            if name == "run_command":
                command = str((call.arguments or {}).get("command") or "")
                if agent_tools.read_only_command(command):
                    return True, ""
                return False, (
                    "helper tabs run only read-only commands; do not build or test here. "
                    "Put the command the coordinator should run in your report"
                )
            if name in agent_tools.MUTATING:
                args = agent_tools.normalize_args(name, call.arguments or {})
                targets = [agent_tools.rel(self.ctx.workspace, path) for path in agent_tools._paths_of(name, args, self.ctx)]
                if name == "move_file":
                    destination = args.get("destination") or args.get("to") or args.get("new_path")
                    if isinstance(destination, str) and destination.strip():
                        targets.append(agent_tools.rel(self.ctx.workspace, agent_tools.resolve(self.ctx.workspace, destination)))
                outside = [item for item in targets if item not in self.allowed]
                if not targets or outside:
                    mine = ", ".join(sorted(self.allowed)) or "none (this brief is read only)"
                    return False, (
                        f"this helper may change only: {mine}. "
                        + (f"{', '.join(outside)} belongs to another helper or to the coordinator; " if outside else "")
                        + "describe the change it needs in your report instead"
                    )
                return True, ""
            return False, "not available in a helper tab"

        def run(self, first_payload: str) -> str:
            cancel = getattr(self.ctx, "cancel", None)
            if cancel is not None and cancel.is_set():
                return "INTERRUPTED"
            return super().run(first_payload)

    return HelperRun


@dataclass
class _Job:
    brief: dict[str, Any]
    future: Future
    cancel: threading.Event | None
    progress: Callable[[str], None] | None
    context: dict[str, Any] = field(default_factory=dict)


class _Slot:
    """One helper tab on its own thread. It opens the tab on its first brief and keeps it."""

    def __init__(self, index: int, pool: "HelperPool") -> None:
        self.index = index
        self.pool = pool
        self.jobs: "queue.Queue[_Job | None]" = queue.Queue()
        self.session_cm: Any = None
        self.session: Any = None
        self.chat: Any = None
        #: Briefs queued or running on this tab.
        self.pending = 0
        self.thread = threading.Thread(target=self._loop, name=f"crit-helper-{index}", daemon=True)
        self.thread.start()

    def _open(self) -> None:
        agent = _agent()
        self.session_cm = self.pool.factory()
        enter = getattr(self.session_cm, "__enter__", None)
        self.session = enter() if callable(enter) else self.session_cm
        self.chat = agent._Chat(
            self.session,
            seed=self.pool.seed,
            retries=self.pool.retries,
            compact_after=self.pool.compact_after,
            quiet=True,
        )
        self.chat.prefix = agent._without_ready(self.pool.seed) if self.pool.seed else ""

    def _close(self) -> None:
        cm, self.session_cm, self.session, self.chat = self.session_cm, None, None, None
        if cm is None:
            return
        try:
            exit_ = getattr(cm, "__exit__", None)
            if callable(exit_):
                exit_(None, None, None)
            else:
                close = getattr(cm, "close", None)
                if callable(close):
                    close()
        except Exception as exc:  # noqa: BLE001
            log.debug(f"closing helper tab {self.index}: {exc}")

    def _loop(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                break
            if not job.future.set_running_or_notify_cancel():
                continue
            try:
                if self.chat is None:
                    self._open()
                report = self.pool.run_brief(
                    self.chat, job.brief, cancel=job.cancel, progress=job.progress, context=job.context
                )
                job.future.set_result(report)
            except BaseException as exc:  # noqa: BLE001 - reported to the coordinator, the tab is reopened next time
                log.warn(f"helper tab {self.index} failed: {exc}")
                self._close()
                job.future.set_exception(exc)
            finally:
                with self.pool._lock:
                    self.pending -= 1
        self._close()


class HelperPool:
    """``size`` helper tabs. :meth:`run` hands each brief to a free tab and waits for all of them."""

    def __init__(
        self,
        factory: Callable[[], Any],
        size: int,
        *,
        seed: str,
        sections: dict[str, str],
        base: dict[str, Any],
        max_result_chars: int,
        retries: int = 3,
        compact_after: int = 300_000,
        max_rounds: int = HELPER_MAX_ROUNDS,
    ) -> None:
        self.factory = factory
        self.size = max(1, int(size))
        self.seed = seed
        self.sections = sections
        self.base = dict(base)
        self.max_result_chars = max_result_chars
        self.retries = retries
        self.compact_after = compact_after
        self.max_rounds = max_rounds
        self._slots: list[_Slot] = []
        self._next = 0
        self._lock = threading.Lock()

    def submit(
        self,
        brief: dict[str, Any],
        *,
        cancel: threading.Event | None,
        progress: Callable[[str], None] | None,
        context: dict[str, Any] | None = None,
    ) -> Future:
        future: Future = Future()
        with self._lock:
            # An open, idle tab first (no page load, no instructions to send again),
            # then a new tab while there is room, then the least busy one.
            idle = [slot for slot in self._slots if slot.pending == 0]
            if idle:
                slot = idle[0]
            elif len(self._slots) < self.size:
                slot = _Slot(len(self._slots) + 1, self)
                self._slots.append(slot)
            else:
                slot = min(self._slots, key=lambda item: item.pending)
            slot.pending += 1
        slot.jobs.put(_Job(brief, future, cancel, progress, dict(context or {})))
        return future

    def run(
        self,
        briefs: list[dict[str, Any]],
        *,
        cancel: threading.Event | None = None,
        progress: Callable[[str], None] | None = None,
        context: dict[str, Any] | None = None,
    ) -> list[Report]:
        """Run every brief (up to ``size`` at a time) and return their reports in order.

        ``context`` adds ToolContext fields for these briefs, such as the task's checkpoints.
        """
        started = time.monotonic()
        futures = [self.submit(brief, cancel=cancel, progress=progress, context=context) for brief in briefs]
        reports: list[Report] = []
        for brief, future in zip(briefs, futures):
            while True:
                try:
                    report = future.result(timeout=0.25)
                    break
                except FutureTimeout:
                    if cancel is not None and cancel.is_set():
                        report = Report(brief["name"], "INTERRUPTED", "", seconds=time.monotonic() - started)
                        break
                    continue
                except Exception as exc:  # noqa: BLE001 - one broken tab does not lose the others
                    report = Report(brief["name"], "FAILED", "", error=str(exc), seconds=time.monotonic() - started)
                    break
            reports.append(report)
        return reports

    def run_brief(
        self,
        chat: Any,
        brief: dict[str, Any],
        *,
        cancel: threading.Event | None,
        progress: Callable[[str], None] | None,
        context: dict[str, Any] | None = None,
    ) -> Report:
        agent = _agent()
        started = time.monotonic()
        name = brief["name"]
        files = list(brief.get("files") or [])
        state = TaskState(task=brief["brief"])
        fields = {key: value for key, value in self.base.items() if key not in {"session", "on_output", "ask_user", "delegate"}}
        fields.update(context or {})
        ctx = agent._tool_context(
            **fields,
            state=state,
            cancel=cancel,
            ask_user=lambda question: self.sections.get("AUTO_DECIDE", "Decide yourself and continue."),
        )
        turns: list[dict[str, str]] = []
        run = _helper_run_class()(
            chat,
            brief["brief"],
            ctx=ctx,
            sections=self.sections,
            turns=turns,
            show=lambda _text: None,
            max_rounds=self.max_rounds,
            max_result_chars=self.max_result_chars,
            check_command=None,
            approve_mode="auto",
            ui_mode_at_start="__helper__",
            hooks=_QuietHooks(name, progress),
            allowed=set(files),
        )
        message = agent.fill(
            self.sections["HELPER"],
            name=name,
            brief=brief["brief"],
            files=", ".join(files) if files else "none: this brief is read only",
        )
        status = run.run(agent.task_message(message))
        text = ""
        for turn in reversed(turns):
            if turn.get("role") == "assistant":
                text = agent._hide_tool_markup(turn.get("content") or "").strip()
                break
        if text.upper().startswith(("COMPLETED", "FINISHED", "DONE")):
            text = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
        if not text and run.findings:
            text = "\n".join(run.findings)
        return Report(
            name=name,
            status=status,
            text=text[:REPORT_CHARS],
            changed=sorted(state.edits),
            rounds=state.step,
            seconds=time.monotonic() - started,
        )

    def close(self) -> None:
        for slot in self._slots:
            slot.jobs.put(None)
        for slot in self._slots:
            slot.thread.join(timeout=15)
        self._slots.clear()


def format_reports(reports: list[Report], seconds: float) -> dict[str, Any]:
    """The delegate call's result for the coordinator, and its line in the terminal."""
    changed = sorted({name for report in reports for name in report.changed})
    lines = [
        f"{len(reports)} helper tab{'s' if len(reports) != 1 else ''} finished in {seconds:.0f}s. "
        "Review what they changed (git_diff or read_files) before building; they did not run builds or tests."
    ]
    for report in reports:
        head = f"--- {report.name}: {report.status} ({report.rounds} rounds, {report.seconds:.0f}s)"
        if report.changed:
            head += "; changed " + ", ".join(report.changed)
        lines.append(head)
        if report.error:
            lines.append("error: " + report.error)
        lines.append(report.text or "(no report)")
    if changed:
        lines.append("Files changed by helpers: " + ", ".join(changed))
    ok = any(report.status in {"COMPLETED", "FINISHED", "DONE"} for report in reports)
    summary = f"{len(reports)} helper tabs · {seconds:.0f}s" + (f" · changed {len(changed)} file{'s' if len(changed) != 1 else ''}" if changed else "")
    ui_lines = [
        f"{report.name}: {report.status.lower()}" + (f" · {', '.join(report.changed[:3])}" if report.changed else "")
        for report in reports
    ]
    result: dict[str, Any] = {
        "tool": "delegate",
        "ok": ok,
        "changed": changed,
        "output": "\n".join(lines),
        "ui": {"summary": summary, "diff": None, "lines": ui_lines},
    }
    if not ok:
        result["error"] = "no helper finished its brief"
    return result
