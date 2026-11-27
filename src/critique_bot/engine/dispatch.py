"""Running one reply's tool calls: permission, approval, parallel reads, repeats, loops.

Every call goes through the installed ``ToolRegistry`` (``tool_api.execute``). Neighbouring
read-only calls run side by side; anything else is checked against the mode, the saved
rules and taint, and asked when it must be. An identical call when nothing changed is
answered from memory instead of run again, and the same set of calls three replies in a
row is a loop.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from critique_bot import agent_edit, agent_shell, log, run_stats, taint
from critique_bot import tool_api as agent_tools
from critique_bot.engine import ui
from critique_bot.engine.parse import ToolCall
from critique_bot.engine.ui import _activity, _friendly_error
from critique_bot.engine.replies import _call_key
from critique_bot.tool_api import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_TOOL_CHARS,
    MUTATING,
    TaskState,
    ToolContext,
    canonical_tool,
    fill,
    normalize_args,
)
from critique_bot.tool_api import execute as _execute


PARALLEL_WORKERS = 4


#: Read-only tools that may run side by side within one reply.
_PARALLEL_SAFE = frozenset(
    {"list_files", "find_files", "read_files", "search_code", "git_status", "git_diff", "git_log", "git_show"}
)


_READ_ONLY_FALLBACK = frozenset(
    _PARALLEL_SAFE | {"skill", "todo", "command_output", "ask_user", "web_fetch"}
)


#: Tools whose result changes over time, so repeating them is not a loop.
_POLLING = frozenset({"command_output", "ask_user"})


#: An identical read-only call at the same disk version is not run a third time.
MAX_SAME_CALL = 2


#: The same set of calls in this many replies in a row is a loop.
LOOP_REPLIES = 3


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


@dataclass
class _Perm:
    """Stand-in for agent_tools.Permission when that is not available."""

    kind: str = "read"
    summary: str = ""
    key: str = ""
    detail: str = ""


def _tool_context(**values: Any) -> ToolContext:
    """A ToolContext with only the fields this version of agent_tools has."""
    names = {item.name for item in dataclasses.fields(ToolContext)}
    return ToolContext(**{key: value for key, value in values.items() if key in names})


def _read_only_tools() -> frozenset[str]:
    return getattr(agent_tools, "READ_ONLY", None) or _READ_ONLY_FALLBACK


#: What a read-only task may run: reads in the project, and nothing that waits on a person or a command.
READ_ONLY_TOOLS = frozenset(_PARALLEL_SAFE | {"todo", "skill"})


def _read_only_allows(call: ToolCall, perm: Any) -> bool:
    """A read-only task reads the project and git history only. Input under review (a patch, an
    issue) must not be able to make crit edit, run, or fetch anything, or read outside the project."""
    kind = getattr(perm, "kind", "read") or "read"
    return kind == "read" and canonical_tool(call.tool) in READ_ONLY_TOOLS


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


class Dispatch:
    """The tool-running half of a task run (mixed into _TaskRun)."""

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
        decision, reason = ui._approve_prompt(perm, risky=risky) if risky else ui._approve_prompt(perm)
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
