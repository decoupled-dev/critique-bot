"""Helper chats: the coordinating session hands a self-contained brief to a short, fresh chat.

A helper is a subagent. The main chat gives it one brief (an investigation, a
contained change, a build to run) and gets back only a short report, so the
files the helper reads and the output it sees never land in the main chat.
Several briefs run at the same time, one helper each.

The chats cannot see each other; crit is what they share. Every helper works
on the same folder through crit's tools, reads anything, changes only the
files its brief was given (no two helpers get the same file), and runs builds
and tests only when it is the single helper of its delegate call. Its report
goes back as the delegate call's result; the coordinator reviews the changes.

Each helper tab lives on its own thread for the whole session: Playwright's
sync API belongs to the thread that opened it. The tab stays open, but every
brief starts a fresh chat in it, so a helper never inherits an earlier brief's
conversation.
"""

from __future__ import annotations

import queue
import tempfile
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from critique_bot import log
from critique_bot import tool_api as agent_tools
from critique_bot.engine import ui
from critique_bot.engine.chat import _Chat
from critique_bot.engine.dispatch import _rule_denied, _tool_context
from critique_bot.engine.parse import _hide_tool_markup
from critique_bot.engine.prompts import task_message
from critique_bot.engine.task import _TaskRun
from critique_bot.engine.ui import _activity
from critique_bot.prompt_build import without_ready as _without_ready
from critique_bot.tool_api import TaskState, ToolContext, canonical_tool, fill

#: Rounds one brief may take before its helper stops and reports.
HELPER_MAX_ROUNDS = 40
#: Characters of each helper's report passed back to the coordinator.
REPORT_CHARS = 6_000
#: A file-defined agent's summary: about 2K tokens.
AGENT_REPORT_CHARS = 8_000
#: Characters of a writer agent's patch shown to the coordinator.
PATCH_CHARS = 40_000
_LAST_STEP = ("This is your last step: tools are off now. Reply COMPLETED and your summary "
              "(what you found or changed, with paths), nothing else.")


@dataclass
class Report:
    name: str
    status: str
    text: str
    changed: list[str] = field(default_factory=list)
    rounds: int = 0
    seconds: float = 0.0
    error: str = ""
    #: A writer agent's changes, made in its own worktree, as a git patch for the coordinator.
    patch: str = ""
    agent: str = ""


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
        self._say(_activity(call))

    def tool_done(self, call: Any, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self._say("failed: " + str(result.get("error") or "")[:120])

    def status(self, code: str) -> None:
        self._say(code.lower())


def _helper_run_class():

    class HelperRun(_TaskRun):
        """A task run that never asks: it may read anything and change only its own files."""

        def __init__(
            self,
            *args: Any,
            allowed: set[str],
            commands: bool = False,
            tools: Any = None,
            agent_name: str = "",
            anywhere: bool = False,
            last_step: bool = False,
            **kwargs: Any,
        ) -> None:
            super().__init__(*args, **kwargs)
            self.allowed = allowed
            #: May run builds and tests: only the single helper of a delegate call.
            self.commands = commands
            #: A file-defined agent's tools (a ToolRegistry from .only), or None for every tool.
            self.tools = tools
            self.agent_name = agent_name
            #: Works in its own worktree: may change any file there.
            self.anywhere = anywhere
            #: The last allowed step removes the tools, so the agent summarizes.
            self.last_step = last_step
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
            name = canonical_tool(getattr(call, "tool", "")) or str(getattr(call, "tool", ""))
            if self.tools is not None and self.tools.spec(name) is None:
                # Not batched as a read: _approved refuses it with the agent's list.
                return agent_tools.Permission("command", f"{name} (not in the {self.agent_name} agent's tools)", "",
                                              risk="not allowed for this agent", tool=name)
            return super()._permission(call)

        def _run_calls(self, calls: list[Any], *, extra: str = "") -> None:
            if self.last_step and self.max_rounds is not None and calls:
                if self.state.step >= self.max_rounds - 1:
                    results = [{"tool": canonical_tool(c.tool) or c.tool, "ok": False,
                                "error": "not run: no tools on the last step"} for c in calls]
                    self.state.step += 1
                    self._send_results(results, extra=_LAST_STEP, calls=calls)
                    return
                if self.state.step == self.max_rounds - 2:
                    extra = (extra + "\n\n" if extra else "") + _LAST_STEP
            super()._run_calls(calls, extra=extra)

        def _approved(self, perm: Any) -> tuple[bool, str]:
            call = self._call
            name = canonical_tool(getattr(call, "tool", "")) if call is not None else ""
            kind = getattr(perm, "kind", "read") or "read"
            if self.tools is not None and self.tools.spec(name or str(getattr(call, "tool", ""))) is None:
                return False, (f"the {self.agent_name} agent cannot use {name or getattr(call, 'tool', '?')}; "
                               f"its tools: {', '.join(self.tools.names())}")
            if name == "delegate":
                return False, "a helper cannot delegate; do the brief yourself"
            denied = _rule_denied(self.ctx, perm)
            if denied:
                return False, denied
            if kind == "read":
                return True, ""
            # A helper asks no one, so anything a person would be asked about is refused:
            # fetches, network commands, secret files, and paths outside the workspace.
            if kind in {"network", "outside"} or getattr(perm, "risk", ""):
                why = getattr(perm, "risk", "") or "it needs the user's yes"
                return False, f"not done in a helper: {why}. Put it in your report for the coordinator"
            if name == "run_command":
                command = str((call.arguments or {}).get("command") or "")
                if agent_tools.read_only_command(command):
                    return True, ""
                risky = agent_tools.risky_command(command)
                if risky:
                    return False, f"not run in a helper: {risky}. Put it in your report for the coordinator"
                return False, (
                    "helper chats use only read-only commands; do not build or test here. "
                    "Put the command the coordinator should run in your report"
                )
            if name in agent_tools.MUTATING:
                args = agent_tools.normalize_args(name, call.arguments or {})
                targets = [agent_tools.rel(self.ctx.workspace, path) for path in agent_tools._paths_of(name, args, self.ctx)]
                if name == "move_file":
                    destination = args.get("destination") or args.get("to") or args.get("new_path")
                    if isinstance(destination, str) and destination.strip():
                        targets.append(agent_tools.rel(self.ctx.workspace, agent_tools.resolve(self.ctx.workspace, destination)))
                outside = [] if self.anywhere else [item for item in targets if item not in self.allowed]
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


def _open_tab(pool: "HelperPool") -> tuple[Any, Any, Any]:
    """A helper tab from ``pool.factory``: (context manager, session, chat)."""
    cm = pool.factory()
    enter = getattr(cm, "__enter__", None)
    session = enter() if callable(enter) else cm
    chat = _Chat(
        session,
        seed=pool.seed,
        retries=pool.retries,
        compact_after=pool.compact_after,
        quiet=True,
    )
    chat.prefix = _without_ready(pool.seed) if pool.seed else ""
    return cm, session, chat


def _close_tab(cm: Any, index: int) -> None:
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
        log.debug(f"closing helper tab {index}: {exc}")


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
        self.session_cm, self.session, self.chat = _open_tab(self.pool)

    def _close(self) -> None:
        cm, self.session_cm, self.session, self.chat = self.session_cm, None, None, None
        _close_tab(cm, self.index)

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
                elif self.chat.sends:
                    # A fresh chat for every brief: the last brief's reading stays out of this one.
                    self.chat.restart("a new brief")
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
        # Helpers never build or test: no one is there to approve a command, and a build runs
        # project code that can reach the network. The coordinator runs those after the reports.
        context = {**(context or {}), "commands": False}
        refused = _over_parallel(briefs, self.base.get("workspace"))
        futures = [
            None if index in refused else self.submit(brief, cancel=cancel, progress=progress, context=context)
            for index, brief in enumerate(briefs)
        ]
        reports: list[Report] = []
        for index, (brief, future) in enumerate(zip(briefs, futures)):
            if future is None:
                reports.append(Report(brief["name"], "FAILED", "", error=refused[index]))
                continue
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
        started = time.monotonic()
        name = brief["name"]
        state = TaskState(task=brief["brief"])
        context = dict(context or {})
        commands = bool(context.pop("commands", False))
        fields = {key: value for key, value in self.base.items() if key not in {"session", "on_output", "ask_user", "delegate"}}
        fields.update(context)
        profile = _AgentRun.start(brief, fields)
        if profile.error:
            return Report(name, "FAILED", "", error=profile.error, agent=str(brief.get("agent") or ""))
        try:
            return self._run_brief(chat, brief, profile, fields, state, commands, started, cancel, progress)
        finally:
            profile.finish()

    def _run_brief(
        self,
        chat: Any,
        brief: dict[str, Any],
        profile: "_AgentRun",
        fields: dict[str, Any],
        state: TaskState,
        commands: bool,
        started: float,
        cancel: threading.Event | None,
        progress: Callable[[str], None] | None,
    ) -> Report:
        name = brief["name"]
        files = list(brief.get("files") or [])
        ctx = _tool_context(
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
            max_rounds=profile.max_steps or self.max_rounds,
            max_result_chars=self.max_result_chars,
            check_command=None,
            approve_mode="auto",
            ui_mode_at_start="__helper__",
            hooks=_QuietHooks(name, progress),
            allowed=set(files),
            commands=commands,
            tools=profile.tools,
            agent_name=profile.name,
            anywhere=profile.worktree is not None,
            last_step=profile.definition is not None,
        )
        if profile.worktree is not None:
            may_change = "any file in your own copy of the project (a git worktree); your changes go back as a patch"
        else:
            may_change = ", ".join(files) if files else "none: this brief is read only"
        message = profile.instructions() + fill(
            self.sections["HELPER"],
            name=name,
            brief=brief["brief"],
            files=may_change,
            commands=(
                "You may run builds and tests with run_command; report their result, not their whole output."
                if commands
                else "Do not run builds or tests (other helpers work at the same time); read-only commands are fine."
            ),
        )
        status = run.run(task_message(message))
        text = ""
        for turn in reversed(turns):
            if turn.get("role") == "assistant":
                text = _hide_tool_markup(turn.get("content") or "").strip()
                break
        if text.upper().startswith(("COMPLETED", "FINISHED", "DONE")):
            text = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
        if not text and run.findings:
            text = "\n".join(run.findings)
        return Report(
            name=name,
            status=status,
            text=text[: AGENT_REPORT_CHARS if profile.definition is not None else REPORT_CHARS],
            changed=[] if profile.worktree is not None else sorted(state.edits),
            rounds=state.step,
            seconds=time.monotonic() - started,
            patch=profile.patch(),
            agent=profile.name,
        )

    def close(self) -> None:
        for slot in self._slots:
            slot.jobs.put(None)
        for slot in self._slots:
            slot.thread.join(timeout=15)
        self._slots.clear()


class InlineHelperPool(HelperPool):
    """Briefs one after another in one extra tab, on the caller's own thread.

    Parallel helper tabs each need their own thread, and a Playwright page belongs to the
    thread that opened it, so those need the browser's remote debugging. A second tab
    opened on the coordinator's thread needs nothing: the briefs take turns in it, each
    in a fresh chat, and the coordinator's chat still sees only their reports.
    """

    def __init__(self, factory: Callable[[], Any], size: int = 1, **kwargs: Any) -> None:
        super().__init__(factory, 1, **kwargs)
        self._cm: Any = None
        self._chat: Any = None

    def run(
        self,
        briefs: list[dict[str, Any]],
        *,
        cancel: threading.Event | None = None,
        progress: Callable[[str], None] | None = None,
        context: dict[str, Any] | None = None,
    ) -> list[Report]:
        context = {**(context or {}), "commands": False}
        refused = _over_parallel(briefs, self.base.get("workspace"))
        reports: list[Report] = []
        for index, brief in enumerate(briefs):
            started = time.monotonic()
            if index in refused:
                reports.append(Report(brief["name"], "FAILED", "", error=refused[index]))
                continue
            if cancel is not None and cancel.is_set():
                reports.append(Report(brief["name"], "INTERRUPTED", ""))
                continue
            try:
                if self._chat is None:
                    self._cm, _session, self._chat = _open_tab(self)
                elif self._chat.sends:
                    # A fresh chat for every brief: the last brief's reading stays out of this one.
                    self._chat.restart("a new brief")
                reports.append(self.run_brief(self._chat, brief, cancel=cancel, progress=progress, context=context))
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - one failed brief does not lose the others
                log.warn(f"helper tab failed: {exc}")
                self.close()
                reports.append(Report(brief["name"], "FAILED", "", error=str(exc), seconds=time.monotonic() - started))
        return reports

    def close(self) -> None:
        cm, self._cm, self._chat = self._cm, None, None
        _close_tab(cm, 1)


class _AgentRun:
    """A brief run by a file-defined agent (``delegate(agent=...)``): its tools, steps, instructions,
    and for an agent that writes, a git worktree of its own whose diff goes back as a patch."""

    def __init__(self) -> None:
        self.definition: Any = None
        self.name = ""
        self.tools: Any = None
        self.worktree: Any = None
        self.error = ""

    @classmethod
    def start(cls, brief: dict[str, Any], fields: dict[str, Any]) -> "_AgentRun":
        run = cls()
        wanted = str(brief.get("agent") or "").strip()
        if not wanted:
            return run
        from critique_bot.extensions import agent_files, worktree

        workspace = Path(fields.get("workspace") or ".")
        found = agent_files.find_agent(wanted, workspace)
        if found is None:
            names = ", ".join(sorted(agent_files.load_agents(workspace)[0]))
            run.error = f"no agent named {wanted!r}; the agents are: {names}"
            return run
        run.definition = found
        run.name = found.name
        if found.tools is not None or found.deny_tools or found.read_only:
            registry = agent_tools.registry()
            run.tools = registry.only(found.allowed_tools(registry.names()))
        if found.effective_isolation == "worktree":
            parent = Path(fields.get("cache_dir") or tempfile.gettempdir()) / "worktrees"
            try:
                run.worktree = worktree.create(workspace, parent)
            except (worktree.WorktreeError, OSError) as exc:
                run.error = f"the {found.name} agent changes files, so it works in a git worktree, and none could be made: {exc}"
                return run
            fields["workspace"] = run.worktree.path
            fields["index_path"] = None
            fields.pop("checkpoints", None)
        return run

    @property
    def max_steps(self) -> int | None:
        return getattr(self.definition, "max_steps", None)

    def instructions(self) -> str:
        if self.definition is None:
            return ""
        body = self.definition.body.strip()
        return f"AGENT: {self.definition.name}. {self.definition.description}\n" + (body + "\n\n" if body else "\n")

    def patch(self) -> str:
        if self.worktree is None:
            return ""
        try:
            return self.worktree.diff()
        except Exception as exc:  # noqa: BLE001 - the report says so; the worktree is removed anyway
            log.warn(f"could not read the {self.name} agent's changes: {exc}")
            return ""

    def finish(self) -> None:
        if self.worktree is not None:
            self.worktree.remove()
            self.worktree = None


def _over_parallel(briefs: list[dict[str, Any]], workspace: Any) -> dict[int, str]:
    """Briefs past an agent's ``max_parallel`` in one delegate call: index -> why they were not run."""
    refused: dict[int, str] = {}
    counts: dict[str, int] = {}
    limits: dict[str, int] = {}
    for index, brief in enumerate(briefs):
        wanted = str(brief.get("agent") or "").strip()
        if not wanted:
            continue
        if wanted not in limits:
            from critique_bot.extensions import agent_files

            found = agent_files.find_agent(wanted, Path(workspace or "."))
            limits[wanted] = found.max_parallel if found is not None else 6
        counts[wanted] = counts.get(wanted, 0) + 1
        if counts[wanted] > limits[wanted]:
            refused[index] = f"the {wanted} agent runs at most {limits[wanted]} at a time (max_parallel)"
    return refused


def format_reports(reports: list[Report], seconds: float) -> dict[str, Any]:
    """The delegate call's result for the coordinator, and its line in the terminal."""
    changed = sorted({name for report in reports for name in report.changed})
    lines = [
        f"{len(reports)} helper{'s' if len(reports) != 1 else ''} finished in {seconds:.0f}s. "
        + (
            "Review what it changed (git_diff or read_files) and trust only the results it quotes."
            if len(reports) == 1
            else "Review what they changed (git_diff or read_files) before building; they did not run builds or tests."
        )
    ]
    for report in reports:
        head = f"--- {report.name}: {report.status} ({report.rounds} rounds, {report.seconds:.0f}s)"
        if report.changed:
            head += "; changed " + ", ".join(report.changed)
        lines.append(head)
        if report.error:
            lines.append("error: " + report.error)
        lines.append(report.text or "(no report)")
        if report.patch:
            shown = report.patch[:PATCH_CHARS]
            cut = len(report.patch) - len(shown)
            lines.append(
                f"Patch from {report.name} (made in its own worktree; nothing in the workspace changed yet). "
                "Review it, then apply it with apply_patch if it is right:\n" + shown
                + (f"\n... {cut} more characters of the patch were cut" if cut else "")
            )
    if changed:
        lines.append("Files changed by helpers: " + ", ".join(changed))
    ok = any(report.status in {"COMPLETED", "FINISHED", "DONE"} for report in reports)
    summary = f"{len(reports)} helper{'s' if len(reports) != 1 else ''} · {seconds:.0f}s" + (f" · changed {len(changed)} file{'s' if len(changed) != 1 else ''}" if changed else "")
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


class SessionHelpers:
    """The delegate tool's runner for one session: briefs go to helper chats, all at once.

    The helper pool opens on the first delegate call (``factory`` opens a helper chat) and
    closes with the session. ``inline``: one extra chat on this thread, briefs in turn (a
    web chat without remote debugging).
    """

    def __init__(
        self,
        factory: Callable[[], Any] | None,
        count: int,
        *,
        inline: bool,
        seed: Callable[[], str],
        sections: dict[str, str],
        base: dict[str, Any],
        max_result_chars: int,
        retries: int,
        compact_after: int,
    ) -> None:
        self.factory = factory
        self.count = count
        self.inline = inline
        self.seed = seed
        self.sections = sections
        self.base = base
        self.max_result_chars = max_result_chars
        self.retries = retries
        self.compact_after = compact_after
        self.pool: Any = None

    def for_task(
        self, cancel: threading.Event, checkpoints: Any, state: TaskState
    ) -> Callable[[list[dict[str, Any]]], dict[str, Any]] | None:
        """The delegate tool's runner for one task, or None when the session has no helpers."""
        if self.factory is None or self.count < 1:
            return None
        show = ui.current()

        def delegate(briefs: list[dict[str, Any]]) -> dict[str, Any]:
            if self.pool is None:
                pool_class = InlineHelperPool if self.inline else HelperPool
                self.pool = pool_class(
                    self.factory,
                    self.count,
                    seed=self.seed(),
                    sections=self.sections,
                    base=self.base,
                    max_result_chars=self.max_result_chars,
                    retries=self.retries,
                    compact_after=self.compact_after,
                )
            started = time.monotonic()
            setter = getattr(show, "set_active", None)
            if callable(setter):
                setter("delegate", len(briefs))
            try:
                reports = self.pool.run(
                    briefs,
                    cancel=cancel,
                    progress=lambda line: show.tool_output(line + "\n"),
                    context={"checkpoints": checkpoints},
                )
            finally:
                if callable(setter):
                    setter("delegate", 1)
            result = format_reports(reports, time.monotonic() - started)
            for name in result.get("changed") or []:
                # The coordinator's task owns these changes: the diff, the check, and undo cover them.
                state.edits[name] = state.edits.get(name, 0) + 1
            return result

        return delegate

    def close(self) -> None:
        if self.pool is None:
            return
        try:
            self.pool.close()
        except Exception as exc:  # the session is over; a stuck tab must not hang the exit
            log.debug(f"closing helper tabs failed: {exc}")
