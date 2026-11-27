# SPDX-License-Identifier: Apache-2.0
"""The verified flow: REPRODUCE -> FIX -> TEST -> REVIEW -> DONE, on top of the turn loop.

One writer keeps its whole context; each state is a sub-goal message
(``prompts/flows/verified.txt``) and a permission profile, and the harness runs the checks
between states:

REPRODUCE  the model states the expected failure and writes a repro (test tree or
           ``.crit/repro/``; source files cannot be written yet). crit runs it on the
           unmodified code and accepts it only when it fails for the stated reason (k=2
           tries). Otherwise the repro is "untrusted" and the flow goes on (a soft gate).
FIX        normal editing. COMPLETED ends an attempt's round: crit runs the repro (it must
           pass now). At most 3 repair rounds per attempt; a rejected attempt's diff and
           why are kept and shown to the next attempt ("do not repeat these"); 2 attempts.
TEST       the selected tests (the project's test command, tests for the changed files by
           name and through the index's import graph), before (the pre-task copies swapped
           in) and after. A test that passed before and fails after sends it back to FIX.
REVIEW     a fresh read-only chat (the ``verifier`` agent file) sees the task, the diff and
           the evidence, not the author's reasoning, and answers in the review JSON
           contract. Blocking findings send it back to FIX once.
DONE       the evidence checklist; the normal finish (the project check, the diff) follows.

Stop rules (``stop_rules``): no change in the failing-test count for N model turns in
FIX stops the task; each state has a token budget. Everything is logged as
``flow_state``, ``repro_result``, ``test_run`` and ``review_result`` events.
"""

from __future__ import annotations

import contextlib
import dataclasses
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from critique_bot import log
from critique_bot import tool_api
from critique_bot.engine.flows import checks
from critique_bot.engine.flows import review as review_mod
from critique_bot.engine.flows.review import (  # noqa: F401 - the flow's public names
    BLOCKING,
    Review,
    ReviewRequest,
    Reviewer,
    parse_review,
    review_from_text,
)
from critique_bot.engine.parse import ToolCall
from critique_bot.engine.prompts import _agent_prompt_path, _read_sections
from critique_bot.engine.stop_rules import ProgressWatch, StateBudgets
from critique_bot.engine.task_class import FlowChoice
from critique_bot.engine.text import _cap, _one_line
from critique_bot.tool_api import canonical_tool, fill

#: on_reply / on_complete: the normal loop handles this reply.
PASS = object()

REPRODUCE, FIX, TEST, REVIEW, DONE, STOPPED = "REPRODUCE", "FIX", "TEST", "REVIEW", "DONE", "STOPPED"
MAX_REPRO_TRIES = 2
MAX_REPAIR_ROUNDS = 3
MAX_ATTEMPTS = 2
MAX_REVIEW_BACKS = 1
OUTPUT_TAIL = 1_500
DIFF_CHARS = 24_000
_EXIT_RE = re.compile(r"^exit (-?\d+)\b", re.M)


def flow_sections() -> dict[str, str]:
    """The flow's model-facing text (prompts/flows/verified.txt)."""
    return _read_sections(_agent_prompt_path("flows/verified.txt"))


def exit_code(result: dict[str, Any]) -> int | None:
    """The exit code of a run_command result ("exit 1 (0.1s)" first line); None when it did not finish."""
    match = _EXIT_RE.search(str(result.get("output") or "")[:200])
    if match:
        return int(match.group(1))
    if result.get("denied") or result.get("skipped"):
        return None
    return 0 if result.get("ok") else (None if "timed out" in str(result.get("error") or "") else 1)


def _tail(text: str, limit: int = OUTPUT_TAIL) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


# --------------------------------------------------------------------------- the evidence


@dataclass
class Evidence:
    """What the flow can show for a change: the checklist in the UI, the API and ``crit run --json``."""

    flow: str = "verified"
    task_class: str = ""
    #: none (not a bugfix), accepted, untrusted, passes (accepted, passes after), fails (still fails)
    repro: str = "none"
    repro_command: str = ""
    repro_reason: str = ""
    tests_before: dict[str, dict[str, Any]] = field(default_factory=dict)
    tests_after: dict[str, dict[str, Any]] = field(default_factory=dict)
    regressions: list[str] = field(default_factory=list)
    unrun_tests: list[str] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)
    impact_unchecked: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    attempts: int = 1
    repair_rounds: int = 0
    rejected_patches: int = 0
    state: str = ""
    stop_reason: str = ""

    def failing(self, rows: dict[str, dict[str, Any]]) -> int | None:
        counted = [row for row in rows.values() if not row.get("skipped")]
        return sum(int(row.get("failing") or 0) for row in counted) if counted else None

    def checklist(self) -> list[str]:
        repro = {
            "none": "repro: not used (" + (self.task_class or "change") + ")",
            "accepted": "repro: ✓ fails before the change (not run after)",
            "passes": f"repro: ✓ failed before, passes after ({_one_line(self.repro_command, 80)})",
            "fails": f"repro: ✗ still fails ({_one_line(self.repro_command, 80)})",
            "untrusted": f"repro: untrusted ({_one_line(self.repro_reason, 100)})",
        }.get(self.repro, f"repro: {self.repro}")
        before, after = self.failing(self.tests_before), self.failing(self.tests_after)
        if after is None:
            tests = "tests: none selected" if not self.tests_after else "tests: could not run"
        else:
            mark = "✓" if after == 0 else "✗"
            shown_before = "?" if before is None else str(before)
            tests = f"tests: {mark} failing {shown_before} → {after} ({len(self.tests_after)} target(s))"
            if self.regressions:
                tests += "; regressions: " + ", ".join(self.regressions[:4])
        if self.review.get("skipped"):
            review = f"review: skipped ({_one_line(str(self.review['skipped']), 100)})"
        elif self.review:
            blocking = int(self.review.get("blocking") or 0)
            mark = "✗" if blocking else "✓"
            review = (f"review: {mark} {self.review.get('risk') or 'no risk given'}, {blocking} blocking, "
                      f"{int(self.review.get('findings') or 0) - blocking} other finding(s)")
        else:
            review = "review: not reached"
        lines = [repro, tests, review]
        if self.impact_unchecked:
            lines.append(f"impact: {len(self.impact_unchecked)} unchecked: " + "; ".join(self.impact_unchecked[:3]))
        lines += [f"caveat: {item}" for item in self.caveats[:6]]
        return lines

    def as_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["failing_before"] = self.failing(self.tests_before)
        data["failing_after"] = self.failing(self.tests_after)
        data["checklist"] = self.checklist()
        return data


@dataclass
class RejectedPatch:
    diff: str
    why: str


# --------------------------------------------------------------------------- the flow


class VerifiedFlow:
    """The state machine. ``run`` is the task's :class:`engine.task._TaskRun`; it calls the hooks."""

    def __init__(
        self,
        run: Any,
        choice: FlowChoice,
        *,
        settings: dict[str, Any] | None = None,
        reviewer: Reviewer | None = None,
        review_session: Callable[[], Any] | None = None,
        seed: Callable[[], str] | None = None,
        nav: Callable[[str], str] | None = None,
        impact: Any = None,
        sections: dict[str, str] | None = None,
        budgets: dict[str, int] | None = None,
    ) -> None:
        options = settings or {}
        self.run = run
        self.choice = choice
        self.text = sections or flow_sections()
        self.reviewer = reviewer
        self.review_session = review_session
        self.seed = seed or (lambda: "")
        #: code_nav's imported_by text for a path (the index), or None.
        self.nav = nav
        self.impact = impact
        self.state = REPRODUCE if choice.repro else FIX
        self.evidence = Evidence(task_class=choice.task_class.kind)
        self.repro_tries = 0
        self.repro_command = ""
        self.repro_expected: checks.Expected | None = None
        self.repro_trusted = False
        self.attempt = 1
        self.rounds = 0
        self.review_backs = 0
        self.rejected: list[RejectedPatch] = []
        self.before: dict[str, checks.TestResult] = {}
        self.after: dict[str, checks.TestResult] = {}
        #: Suggest plan mode when the first attempt fails its checks twice (the plan_triggers setting).
        self.plan_suggested = options.get("plan_triggers") is False
        self.watch = ProgressWatch(_int(options.get("flow_no_progress_turns"), ProgressWatch.DEFAULT_TURNS))
        self.budgets = StateBudgets.from_settings(options, extra=budgets)
        self.done_logged = False

    # ------------------------------------------------------------ plumbing

    @property
    def log(self) -> Any:
        return getattr(self.run.chat, "log", None)

    def _write(self, event: str, **fields: Any) -> None:
        slog = self.log
        if slog is not None:
            try:
                slog.write(event, **fields)
            except Exception as exc:  # noqa: BLE001 - the log never breaks a task
                log.debug(f"flow event {event}: {exc}")

    def _enter(self, state: str, note: str = "") -> None:
        old, self.state = self.state, state
        self.evidence.state = state
        self._write("flow_state", state=state, previous=old, note=note or None, attempt=self.attempt,
                    round=self.rounds)
        if state not in {DONE, STOPPED}:
            self.run.hooks.ui("work", f"Verified flow: {state.lower()}" + (f" ({note})" if note else ""))

    def _say(self, text: str) -> None:
        """The next message to the model: the sub-goal text and the usual STATE block."""
        self.run.payload = text.strip() + "\n\n" + self.run._state_text()
        self.run.turns.append({"role": "user", "content": self.run.payload})

    def _section(self, name: str, **values: str) -> str:
        return fill(self.text.get(name, ""), **values)

    # ------------------------------------------------------------ hooks from the task

    def start(self) -> str:
        """The sub-goal text that goes with the first message, and the first flow_state event."""
        self._write("flow_state", state=self.state, previous=None, flow=self.choice.flow,
                    task_class=self.choice.task_class.as_dict(), source=self.choice.source)
        self.run.hooks.ui("work", f"Verified flow ({self.choice.task_class.kind}): "
                                  + ("reproduce, fix, test, review" if self.choice.repro else "fix, test, review"))
        return self.text.get("REPRODUCE" if self.state == REPRODUCE else "FIX", "")

    def count_turn(self, sent: str, reply: str) -> None:
        """One model turn: its estimated tokens go to the state's budget; FIX turns count toward no-progress."""
        self.budgets.spend(self.state, (len(sent or "") + len(reply or "")) // 4)
        if self.state == FIX:
            self.watch.turn()

    def limits(self) -> str | None:
        """After a handled turn: the no-progress stop and the state budgets. A finish code, or None."""
        if self.state == FIX and self.watch.stalled():
            return self._stop(f"no progress: {self.watch.turns} model turns in FIX without a change in the failing-test "
                              f"count (still {self.watch.failing})")
        over = self.budgets.over(self.state)
        if over is None:
            return None
        if self.state == REPRODUCE:
            self._untrusted(f"the REPRODUCE step used its budget ({over} est. tokens)", lead="REPRODUCE_BUDGET")
            return None
        if self.state == FIX:
            return self._stop(f"the FIX step used its token budget ({over} est. tokens) before its checks passed")
        return None

    def blocks(self, call: ToolCall, perm: Any) -> str:
        """The state's permission profile: in REPRODUCE only the repro and tests may be written."""
        if self.state != REPRODUCE:
            return ""
        name = canonical_tool(call.tool) or call.tool
        if name not in tool_api.MUTATING:
            return ""
        try:
            args = tool_api.normalize_args(name, dict(call.arguments or {}))
            paths = tool_api._paths_of(name, args, self.run.ctx)
        except Exception:  # noqa: BLE001
            paths = []
        workspace = Path(self.run.ctx.workspace).resolve()
        bad = []
        for path in paths:
            try:
                rel = Path(path).resolve().relative_to(workspace).as_posix()
            except (ValueError, OSError):
                rel = str(path)
            if not checks.is_test_path(rel):
                bad.append(rel)
        if not paths or bad:
            return ("not run: the REPRODUCE step writes only the repro (a test in the test tree or a file under "
                    ".crit/repro/); " + (", ".join(bad) + " is source; " if bad else "")
                    + "source changes come in the FIX step, after the repro is in")
        return ""

    def observe(self, call: ToolCall, result: dict[str, Any]) -> None:
        """A command the model ran: a test run's failing count is the progress signal."""
        if canonical_tool(call.tool) != "run_command":
            return
        command = str((call.arguments or {}).get("command") or "")
        if checks.looks_like_tests(command) or (self.repro_command and command.strip() == self.repro_command):
            self.watch.seen(checks.failing_count(str(result.get("output") or ""), bool(result.get("ok"))))

    def on_reply(self, reply: str, status: str | None) -> Any:
        """A reply with no tool calls. REPRODUCE reads its REPRO / EXPECTED lines; other states pass."""
        if self.state != REPRODUCE or self.run._planning():
            return PASS
        command, expected = checks.parse_repro_reply(reply)
        if not command:
            if status in {"FAILED", "BLOCKED"}:
                self._untrusted("the model could not write a repro: " + _one_line(reply, 160))
                return None
            self.repro_tries += 1
            if self.repro_tries >= MAX_REPRO_TRIES:
                self._untrusted(f"no REPRO command after {self.repro_tries} replies")
                return None
            self._say(self.text.get("REPRO_NEEDED", ""))
            return None
        self.repro_command = command
        self.evidence.repro_command = command
        result = self.run._run_harness_command(command, label="the repro (unmodified code)")
        code = exit_code(result)
        output = str(result.get("output") or result.get("error") or "")
        if result.get("denied") or (code is None and not result.get("ok")):
            verdict = checks.ReproVerdict(False, "the repro command did not run: " + _one_line(output, 120))
        else:
            verdict = checks.judge_repro(expected, code, output)
        self.repro_tries += 1
        self._write("repro_result", phase="before", command=command, expected=expected.raw if expected else None,
                    exit=code, accepted=verdict.accepted, reason=verdict.reason, seen=list(verdict.seen) or None,
                    output=_tail(output))
        if verdict.accepted:
            self.repro_trusted = True
            self.repro_expected = expected
            self.evidence.repro = "accepted"
            self.evidence.repro_reason = verdict.reason
            self.run.hooks.ui("good", f"Repro accepted: {_one_line(command, 80)}")
            self.watch.seen(checks.failing_count(output, False))
            self._enter(FIX, "repro accepted")
            self._say(self._section("REPRO_ACCEPTED", command=command))
            return None
        self.run.hooks.ui("bad", f"Repro not accepted: {verdict.reason}")
        if self.repro_tries >= MAX_REPRO_TRIES:
            self._untrusted(verdict.reason)
            return None
        self._say(self._section("REPRO_REJECTED", reason=verdict.reason, output=_tail(output, 1_200)))
        return None

    def on_complete(self) -> Any:
        """The model claims it is done (a COMPLETED finish). FIX runs its checks; other states pass."""
        if self.state == REPRODUCE:
            # Finished without a repro: go on without one, and check the claim at once.
            self._untrusted("the model finished before writing a repro", say=False)
        if self.state != FIX:
            return PASS
        if not self.run._files_changed():
            return PASS  # nothing to verify: the normal finish asks once about a missing change
        self._drop_bytecode()
        if self.repro_trusted:
            result = self.run._run_harness_command(self.repro_command, label="the repro (after the change)")
            code = exit_code(result)
            output = str(result.get("output") or result.get("error") or "")
            passed = code == 0
            self._write("repro_result", phase="after", command=self.repro_command, exit=code, passed=passed,
                        output=_tail(output))
            self.watch.seen(checks.failing_count(output, passed))
            self.evidence.repro = "passes" if passed else "fails"
            if not passed:
                return self._repair("the repro still fails", output)
        self._enter(TEST)
        problem = self._test()
        if problem is not None:
            why, output = problem
            self._enter(FIX, "regression")
            return self._repair(why, output)
        self._enter(REVIEW)
        review = self._review()
        self.evidence.review = review.as_dict()
        self._write("review_result", **{key: value for key, value in review.as_dict().items() if key != "items"},
                    items=review.as_dict()["items"] or None)
        if review.blocking and self.review_backs < MAX_REVIEW_BACKS:
            self.review_backs += 1
            self._enter(FIX, "blocking review findings")
            self.run.hooks.ui("bad", f"The reviewer found {len(review.blocking)} blocking problem(s). Back to FIX.")
            self._say(self._section("REVIEW_BLOCKING", findings=review_mod.findings_text(review.blocking)))
            return None
        if review.blocking:
            self.evidence.caveats.append(f"the reviewer still reports {len(review.blocking)} blocking finding(s)")
        self._done()
        return PASS

    # ------------------------------------------------------------ states

    def _untrusted(self, reason: str, *, lead: str = "", say: bool = True) -> None:
        self.repro_trusted = False
        self.evidence.repro = "untrusted"
        self.evidence.repro_reason = reason
        self.evidence.caveats.append(f"no trusted repro: {reason}")
        self._write("repro_result", phase="before", command=self.repro_command or None, accepted=False,
                    untrusted=True, reason=reason)
        self.run.hooks.ui("note", f"Repro untrusted ({_one_line(reason, 100)}); continuing without it.")
        self._enter(FIX, "repro untrusted")
        if say:
            text = self._section("REPRO_UNTRUSTED", reason=reason)
            if lead:
                text = self.text.get(lead, "") + "\n" + text
                if self.run.payload:
                    self.run.payload = text.strip() + "\n\n" + self.run.payload
                    return
            self._say(text)

    def _repair(self, why: str, output: str) -> Any:
        self.rounds += 1
        self.evidence.repair_rounds += 1
        self.run.hooks.ui("bad", f"Verified flow: {why}.")
        if self.rounds > MAX_REPAIR_ROUNDS:
            self.rejected.append(RejectedPatch(self._diff(DIFF_CHARS // 4), why))
            self.evidence.rejected_patches = len(self.rejected)
            self._write("flow_state", state=FIX, previous=FIX, note="attempt rejected", attempt=self.attempt,
                        why=why)
            if self.attempt >= MAX_ATTEMPTS:
                return self._stop(f"{self.attempt} fix attempts did not pass their checks (last: {why})", code="FAILED")
            self.attempt += 1
            self.rounds = 0
            self.evidence.attempts = self.attempt
            self.watch.reset()
            rejected = "\n\n".join(f"--- rejected patch {index} ({item.why})\n{item.diff or '(no diff)'}"
                                   for index, item in enumerate(self.rejected, start=1))
            self._say(self._section("NEW_ATTEMPT", attempt=str(self.attempt), max=str(MAX_REPAIR_ROUNDS),
                                    rejected=rejected))
            return None
        self._say(self._section("REPAIR", why=why, output=_tail(output), round=str(self.rounds),
                                max=str(MAX_REPAIR_ROUNDS)))
        if self.attempt == 1 and self.rounds == 2 and not self.plan_suggested:
            # ADaPT: decompose when the executor fails. The plan message goes in front of the repair.
            self.plan_suggested = True
            suggest = getattr(self.run, "_suggest_plan", None)
            if callable(suggest):
                suggest(f"the first fix attempt failed its checks twice ({why})")
        return None

    def _stop(self, reason: str, *, code: str = "STOPPED") -> str:
        self.evidence.stop_reason = reason
        tried = [f"attempt {index}: {item.why}" for index, item in enumerate(self.rejected, start=1)]
        if self.run.findings:
            tried.append("last notes: " + " | ".join(self.run.findings[-2:]))
        self._finish_evidence(STOPPED)
        message = f"verified flow stopped: {reason}" + (". Tried: " + "; ".join(tried) if tried else "")
        return self.run._end(code, message)

    def _done(self) -> None:
        if self.evidence.repro == "passes" or any(not row.skipped for row in self.after.values()):
            self.run.verified = True  # the harness ran the checks: no "you ran nothing" nudge
        self._finish_evidence(DONE)
        self.run.hooks.ui("good", "Verified: " + ("; ".join(self.evidence.checklist()[:3])))

    def _finish_evidence(self, state: str) -> None:
        if self.done_logged:
            return
        self.done_logged = True
        if self.impact is not None:
            try:
                self.evidence.impact_unchecked = [item.todo for item in self.impact.unchecked(self.run.state)]
            except Exception as exc:  # noqa: BLE001
                log.debug(f"impact list: {exc}")
        if self.evidence.repro == "accepted" and state == DONE:
            self.evidence.caveats.append("the repro was not run after the change")
        self.state = state
        self.evidence.state = state
        self._write("flow_state", state=state, previous=None, evidence=self.evidence.as_dict())
        for line in self.evidence.checklist():
            self.run.hooks.ui("note", line)

    def finish(self) -> dict[str, Any]:
        """At task end (any code): the evidence, logged once."""
        if not self.done_logged:
            if self.state not in {DONE, STOPPED}:
                self.evidence.caveats.append(f"the task ended in {self.state} before the flow's checks passed")
            self._finish_evidence(self.state)
        return self.evidence.as_dict()

    # ------------------------------------------------------------ TEST

    def _changed(self) -> list[str]:
        edits = list(getattr(self.run.state, "edits", {}) or {})
        cp = self.run.ctx.checkpoints
        if cp is not None and getattr(cp, "task_dir", None) is not None and cp.task_dir.is_dir():
            for step in sorted(item for item in cp.task_dir.iterdir() if item.is_dir()):
                marker = step / "path.txt"
                if marker.is_file():
                    rel = marker.read_text(encoding="utf-8").strip()
                    if rel and rel not in edits:
                        edits.append(rel)
        return [rel for rel in edits if not Path(rel).is_absolute()]

    def _test(self) -> tuple[str, str] | None:
        """Run the selected set before (once per target) and after. (why, output) on a regression."""
        workspace = Path(self.run.ctx.workspace)
        selection = checks.select_tests(workspace, self._changed(), check_command=self.run.check_command, nav=self.nav)
        self.evidence.unrun_tests = selection.unrun
        if selection.unrun:
            note = "tests found but not run by crit (run them with the project's tool): " + ", ".join(selection.unrun[:5])
            if note not in self.evidence.caveats:
                self.evidence.caveats.append(note)
        if not selection.targets:
            if "no tests were selected" not in self.evidence.caveats:
                self.evidence.caveats.append("no tests were selected")
            return None
        missing = [target for target in selection.targets if target.label not in self.before]
        if missing:
            with self._originals() as swapped:
                self._drop_bytecode()
                for target in missing:
                    if swapped:
                        self.before[target.label] = self._run_target(target, "before")
                    else:
                        self.before[target.label] = checks.TestResult(target, False, 0, skipped="no pre-task copies")
            if not any(not row.skipped for row in self.before.values()):
                note = "no before run (no undo copies of the changed files)"
                if note not in self.evidence.caveats:
                    self.evidence.caveats.append(note)
        self._drop_bytecode()
        self.after = {target.label: self._run_target(target, "after") for target in selection.targets}
        self.evidence.tests_before = {label: row.as_dict() for label, row in self.before.items()
                                      if label in self.after}
        self.evidence.tests_after = {label: row.as_dict() for label, row in self.after.items()}
        failing = sum(row.failing for row in self.after.values() if not row.skipped)
        self.watch.seen(failing)
        self._write("test_run", targets=[target.label for target in selection.targets],
                    before={label: row.as_dict() for label, row in self.before.items() if label in self.after},
                    after={label: row.as_dict() for label, row in self.after.items()}, failing_after=failing)
        found = checks.regressions(self.before, self.after)
        self.evidence.regressions = found
        if found:
            output = "\n\n".join(f"{label}:\n{_tail(self.after[label].output, 1_000)}" for label in found)
            return f"tests that passed before now fail: {', '.join(found)}", output
        still = [label for label, row in self.after.items() if not row.ok and not row.skipped]
        if still:
            self.evidence.caveats.append("failing before and after (not caused by this change): " + ", ".join(still))
        check = next((row for row in self.after.values() if row.target.source == "check"), None)
        if check is not None and check.ok:
            self.run.check_passed = True
        return None

    def _drop_bytecode(self) -> None:
        """Remove the cached bytecode of the changed Python files. Python trusts a .pyc whose source has
        the same size and mtime second, and an edit (or the before/after swap) can keep both."""
        workspace = Path(self.run.ctx.workspace)
        for rel in self._changed():
            if not rel.endswith(".py"):
                continue
            source = workspace / rel
            for cached in (source.parent / "__pycache__").glob(source.stem + ".*.pyc"):
                try:
                    cached.unlink()
                except OSError:
                    pass

    def _run_target(self, target: checks.TestTarget, phase: str) -> checks.TestResult:
        result = self.run._run_harness_command(target.command, label=f"{target.label} ({phase})")
        output = str(result.get("output") or result.get("error") or "")
        if result.get("denied") or exit_code(result) is None and not result.get("ok"):
            return checks.TestResult(target, False, 0, output=_tail(output), skipped=_one_line(output, 120) or "did not run")
        ok = bool(result.get("ok"))
        return checks.TestResult(target, ok, checks.failing_count(output, ok), output=_tail(output, 3_000))

    @contextlib.contextmanager
    def _originals(self) -> Iterator[bool]:
        """The pre-task copies of the changed files on disk for a moment (files the task made are moved
        aside), then the task's versions back. Yields False when there are no copies."""
        cp = self.run.ctx.checkpoints
        task_dir = getattr(cp, "task_dir", None) if cp is not None else None
        if task_dir is None or not Path(task_dir).is_dir():
            yield False
            return
        workspace = Path(self.run.ctx.workspace)
        aside = Path(tempfile.mkdtemp(prefix="crit-flow-"))
        #: (file, the task's version saved aside or None when the task removed it)
        swapped: list[tuple[Path, Path | None]] = []
        try:
            for index, step in enumerate(sorted(item for item in Path(task_dir).iterdir() if item.is_dir())):
                marker = step / "path.txt"
                if not marker.is_file():
                    continue
                rel = marker.read_text(encoding="utf-8").strip()
                if not rel or rel.startswith(".crit/") or Path(rel).is_absolute():
                    continue
                target = workspace / rel
                keep: Path | None = None
                if target.is_file():
                    keep = aside / f"{index:03d}"
                    shutil.copy2(target, keep)
                swapped.append((target, keep))
                original = step / "before"
                if original.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(original, target)
                elif target.exists():
                    target.unlink()  # the task made it: it was not there before
            yield True
        finally:
            lost = False
            for target, keep in swapped:
                try:
                    if keep is not None:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(keep, target)
                    elif target.exists():
                        target.unlink()  # the task removed it
                except OSError as exc:
                    log.warn(f"verified flow: could not put {target} back: {exc} (a copy is in {aside})")
                    lost = True
            if not lost:
                shutil.rmtree(aside, ignore_errors=True)

    # ------------------------------------------------------------ REVIEW

    def _diff(self, limit: int = DIFF_CHARS) -> str:
        cp = self.run.ctx.checkpoints
        diff = cp.disk_diff() if cp is not None else None
        if not diff:
            return ""
        chunks = re.split(r"(?m)^(?=--- )", diff)
        kept = [chunk for chunk in chunks if chunk.strip() and not re.match(r"--- (?:a/)?\.crit/", chunk)]
        return _cap("".join(kept), limit)

    def _evidence_text(self) -> str:
        lines = []
        if self.repro_command:
            lines.append(f"Repro: {self.repro_command} -> {self.evidence.repro}"
                         + (f" ({self.evidence.repro_reason})" if self.evidence.repro_reason else ""))
        for label, row in self.after.items():
            before = self.before.get(label)
            was = "skipped" if before is None or before.skipped else ("pass" if before.ok else f"{before.failing} failing")
            now = "skipped" if row.skipped else ("pass" if row.ok else f"{row.failing} failing")
            lines.append(f"Tests `{row.target.command}`: before {was}, after {now}")
            if not row.ok and row.output:
                lines.append(_tail(row.output, 800))
        if not lines:
            lines.append("No repro and no tests were run.")
        return "\n".join(lines)

    def _review(self) -> Review:
        request = ReviewRequest(task=self.run.task, diff=self._diff(), evidence=self._evidence_text(),
                                workspace=Path(self.run.ctx.workspace))
        if self.reviewer is None and self.review_session is None:
            return Review(skipped="no second chat for an independent reviewer")
        try:
            if self.reviewer is not None:
                text = self.reviewer(request)
            else:
                assert self.review_session is not None
                text, used = review_mod.review_in_chat(request, open_session=self.review_session, run=self.run,
                                                   seed=self.seed() or "", text=self.text,
                                                   budget=self.budgets.limit(REVIEW))
                self.budgets.spend(REVIEW, used)
        except Exception as exc:  # noqa: BLE001 - a failed review is a caveat, not a failed task
            log.warn(f"verified flow: the review failed: {exc}")
            return Review(skipped=f"the review failed: {_one_line(str(exc), 120)}")
        if not text.strip():
            return Review(skipped="the reviewer gave no answer")
        return review_from_text(text)

def _int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default
