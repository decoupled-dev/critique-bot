"""Local coding agent. The web UI already holds the system prompt.

This module parses ``<tool_call>`` blocks, runs an allowlisted tool, and
sends ``<tool_result>`` blocks back on the same chat session. It does not
send tool instructions.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from critique_bot import log
from critique_bot.bot_home import BotHome
from critique_bot.chat_client import COMPLETION_IDLE
from critique_bot.code_index import SKIP_DIR_NAMES, refresh_path, search_symbols
from critique_bot.config import BotConfig
from critique_bot.output import isoformat, write_output
from critique_bot.patch import looks_binary_bytes, looks_binary_path

ALLOWED_TOOLS = (
    "list_files",
    "read_files",
    "search_code",
    "write_files",
    "edit_file",
    "delete_file",
    "run_command",
    "git_status",
    "git_diff",
    "apply_patch",
)

_OPEN_RE = re.compile(r"<tool_call>", re.IGNORECASE)
_CLOSE_RE = re.compile(r"</tool_call>", re.IGNORECASE)
_BLOCK_RE = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>",
    re.IGNORECASE | re.DOTALL,
)
_FENCE_RE = re.compile(r"^```[^\n]*\n(.*)\n```$", re.DOTALL)

DEFAULT_READ_CHARS = 32_000
DEFAULT_TOOL_CHARS = 30_000
DEFAULT_COMMAND_TIMEOUT = 120
_MAX_COMMAND_TIMEOUT = 600
_MAX_LIST_ENTRIES = 2_000
_DEFAULT_READ_LINES = 200
_SCAN_MAX_BYTES = 1_000_000
_LINE_NO_RE = re.compile(r"^\s*\d+\|")
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_PLAN_RE = re.compile(r"<plan>\s*(.*?)\s*</plan>", re.IGNORECASE | re.DOTALL)
_MUTATING = frozenset({"write_files", "edit_file", "delete_file", "apply_patch"})
_MIN_PLAN_CHARS = 40
_QUIT = {"exit", "quit", "/exit", "/quit", "/q"}


@dataclass(frozen=True)
class ToolCall:
    tool: str
    arguments: dict[str, Any]
    error: str | None = None


def command_argv(command: str, *, platform_name: str | None = None) -> list[str]:
    """Build the argv for ``run_command``. The command is one argument."""
    plat = platform_name if platform_name is not None else sys.platform
    if plat == "win32":
        return _powershell_argv(command)
    return ["bash", "-lc", command]


def _powershell_argv(command: str) -> list[str]:
    """Run one PowerShell command and return its real exit code and UTF-8 text.

    Windows PowerShell writes UTF-16 when stdout is a pipe, and a native
    program's exit code stays in ``$LASTEXITCODE`` instead of the process
    code. The wrapper fixes both so the tool result is the text the command
    printed.
    """
    script = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        "$OutputEncoding = [Console]::OutputEncoding\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        f"{command.rstrip()}\n"
        "if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded,
    ]


def parse_tool_calls(text: str) -> tuple[list[ToolCall], bool]:
    """Parse tool calls. The bool is True only when every tag is unclosed.

    A reply that mixes finished blocks with a cut-off block keeps the finished
    calls and adds an error for the unfinished one, so one bad call does not
    discard the rest.
    """
    opens = len(_OPEN_RE.findall(text))
    closes = len(_CLOSE_RE.findall(text))
    calls: list[ToolCall] = []
    for match in _BLOCK_RE.finditer(text):
        calls.extend(_parse_block(match.group(1)))
    if opens != closes and calls:
        calls.append(
            ToolCall(
                tool="",
                arguments={},
                error=(
                    "a later tool_call was cut off; complete calls in this reply "
                    "were run; resend only the unfinished block"
                ),
            )
        )
        return calls, False
    if opens != closes:
        return [], True
    if not calls:
        for match in _FENCED_JSON_RE.finditer(text):
            parsed = _parse_block(match.group(1))
            for call in parsed:
                if call.tool and not call.error:
                    calls.append(call)
    return calls, False


def commentary(text: str) -> str:
    return _BLOCK_RE.sub("", text).strip()


def format_tool_result(result: dict[str, Any]) -> str:
    payload: dict[str, Any] = {
        "tool": result.get("tool", ""),
        "ok": bool(result.get("ok")),
    }
    if payload["ok"]:
        payload["output"] = result.get("output", "")
    else:
        payload["error"] = result.get("error") or "tool failed"
        if result.get("output"):
            payload["output"] = result["output"]
        if result.get("allowed"):
            payload["allowed"] = list(result["allowed"])
    return (
        "<tool_result>\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n</tool_result>"
    )


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
) -> dict[str, Any]:
    if name not in ALLOWED_TOOLS:
        return {
            "tool": name,
            "ok": False,
            "error": "unknown tool",
            "allowed": list(ALLOWED_TOOLS),
        }
    args = arguments if isinstance(arguments, dict) else {}
    handler = _HANDLERS[name]
    try:
        result = handler(
            args,
            workspace=Path(workspace),
            index_path=index_path,
            cache_dir=cache_dir,
            max_chars=max_chars,
            command_timeout=command_timeout,
            runner=runner or subprocess.run,
        )
    except Exception as exc:
        log.debug(f"tool {name} failed: {exc}")
        return {"tool": name, "ok": False, "error": str(exc)}
    result.setdefault("tool", name)
    return result


_SECTION_RE = re.compile(
    r"^<<<(SYSTEM|TASK_PREFIX|NUDGE|RECOVER|PLAN_ACK)>>>\s*$",
    re.MULTILINE,
)
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_FALLBACK_TASK_PREFIX = (
    "Print tool_call blocks to act. Do not refuse.\n\nTask:\n"
)
_FALLBACK_NUDGE = (
    "No tool_call block was found, so nothing was run. "
    "Reply with a tool_call block now.\n"
    "<tool_call>\n"
    '{"tool": "list_files", "arguments": {"path": "."}}\n'
    "</tool_call>"
)
_FALLBACK_RECOVER = (
    "The previous step did not finish. Nothing further was run. "
    "Send the next tool_call now. If old_string was not found, read_files "
    "that path and copy the span without the N| prefix. If a command failed, "
    "run a different command. Do not answer in prose yet."
)
_FALLBACK_PLAN_ACK = (
    "Plan recorded. Do not edit in that same reply. "
    "On the next reply, call edit_file, write_files, delete_file, or apply_patch. "
    "Read any path in the plan that you have not read yet."
)


def load_prompt_sections() -> dict[str, str]:
    """Read prompts/agent.txt. Model-facing text lives only in that file."""
    path = _agent_prompt_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OSError(f"could not read agent instructions {path}: {exc}") from exc
    if not text.strip():
        raise OSError(f"agent instructions are empty: {path}")
    parts = _SECTION_RE.split(text)
    sections: dict[str, str] = {}
    if len(parts) >= 3:
        for index in range(1, len(parts), 2):
            sections[parts[index]] = parts[index + 1].strip()
    if "SYSTEM" not in sections:
        sections["SYSTEM"] = text.strip()
    sections.setdefault("TASK_PREFIX", _FALLBACK_TASK_PREFIX.strip())
    sections.setdefault("NUDGE", _FALLBACK_NUDGE.strip())
    sections.setdefault("RECOVER", _FALLBACK_RECOVER.strip())
    sections.setdefault("PLAN_ACK", _FALLBACK_PLAN_ACK.strip())
    return sections


def load_agent_instructions() -> str:
    """Tool instructions seeded once into the chat."""
    return load_prompt_sections()["SYSTEM"]


def task_message(task: str) -> str:
    """Repeat a short protocol line from the prompt file before the task."""
    body = task.strip()
    prefix = load_prompt_sections()["TASK_PREFIX"]
    if body.startswith(prefix.splitlines()[0]):
        return body
    return prefix.rstrip() + "\n" + body


def _nudge_message() -> str:
    return load_prompt_sections()["NUDGE"]


def _recover_message() -> str:
    return load_prompt_sections()["RECOVER"]


def _plan_ack_message() -> str:
    return load_prompt_sections()["PLAN_ACK"]


def seed_message(workspace: Path, instructions: str) -> str:
    shell = (
        "powershell.exe -NoProfile -NonInteractive -Command"
        if sys.platform == "win32"
        else "bash -lc"
    )
    return (
        instructions.rstrip()
        + "\n\nENVIRONMENT\n"
        + f"os: {sys.platform}\n"
        + f"shell: {shell}\n"
        + f"workspace: {workspace}\n"
        + "run_command uses that shell. Its tool_result is the command result. "
        + "The output starts with exit and the code, then stdout, then stderr.\n"
    )


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
) -> list[dict[str, str]]:
    """Talk to ``session.send`` until the model stops calling tools.

    When ``seed`` is set, that text is sent first. Each task is sent with a short
    reminder to print tool_call blocks. A reply with no tool_call is sent back
    until one tool has run. A question, a refusal, or a promise is sent back
    again with no retry cap. DONE, or a reply that no edit is needed, is answered
    with DONE and that task ends. Any other plain answer after a tool has run
    ends that task, and the session then waits until the user types another
    task or exits.
    """
    turns: list[dict[str, str]] = []
    reader = read_message or _read_message
    show = emit or _emit
    if seed and seed.strip():
        _seed_session(
            session,
            seed.strip(),
            turns,
            workspace=workspace,
            index_path=index_path,
            cache_dir=cache_dir,
            max_result_chars=max_result_chars,
            show=show,
        )
    pending = first_task.strip()
    announced = False
    while True:
        if not pending:
            if not announced:
                print(
                    "Agent ready. Type a task, or exit / Ctrl-D to quit.",
                    file=sys.stderr,
                )
                announced = True
            pending = reader() or ""
            if not pending.strip():
                break
        task = pending.strip()
        turns.append({"role": "user", "content": task})
        payload = task_message(task)
        finished = False
        tools_ran = False
        nudges = 0
        recoveries = 0
        last_failed = False
        plan = ""
        rounds = 0
        while max_rounds is None or rounds < max_rounds:
            rounds += 1
            with log.loading("Waiting for the assistant..."):
                reply = session.send(payload)
            detail = getattr(session, "last_detail", None)
            calls, unclosed = parse_tool_calls(reply)
            note = commentary(reply)
            if note:
                show(note)
            planned_before = bool(plan) and len(plan) >= _MIN_PLAN_CHARS
            found_plan = _plan_text(reply)
            if found_plan and len(found_plan) >= _MIN_PLAN_CHARS:
                plan = found_plan
                print(
                    f"plan recorded ({len(plan)} chars)",
                    file=sys.stderr,
                    flush=True,
                )
            if unclosed or _idle_tool_reply(detail, reply):
                payload = format_tool_result(
                    {
                        "tool": "",
                        "ok": False,
                        "error": (
                            "reply was truncated before the tool call closed; "
                            "resend complete tool_call blocks"
                        ),
                    }
                )
                turns.append({"role": "assistant", "content": reply})
                turns.append({"role": "tool", "content": payload})
                last_failed = True
                continue
            if not calls and found_plan and len(found_plan) >= _MIN_PLAN_CHARS:
                payload = _plan_ack_message()
                print(
                    "plan recorded; waiting for the edit",
                    file=sys.stderr,
                    flush=True,
                )
                turns.append({"role": "assistant", "content": reply})
                turns.append({"role": "user", "content": payload})
                continue
            if not calls and found_plan is not None:
                payload = format_tool_result(
                    {
                        "tool": "",
                        "ok": False,
                        "error": (
                            "plan is too short. Name each file, the change, "
                            "and the check inside <plan>."
                        ),
                    }
                )
                turns.append({"role": "assistant", "content": reply})
                turns.append({"role": "tool", "content": payload})
                last_failed = True
                continue
            if not calls:
                if _no_edit_needed(reply):
                    turns.append({"role": "assistant", "content": reply})
                    if not note and reply.strip():
                        show(reply)
                    print("DONE", file=sys.stderr, flush=True)
                    if not _exact_done(reply):
                        turns.append({"role": "user", "content": "DONE"})
                        with log.loading("Waiting for the assistant..."):
                            session.send("DONE")
                    finished = True
                    break
                stalled = _stalls(reply)
                if stalled or (not tools_ran and nudges < 2):
                    if not stalled:
                        nudges += 1
                    payload = _recover_message() if tools_ran else _nudge_message()
                    print(
                        "model replied without a tool_call; asking again",
                        file=sys.stderr,
                        flush=True,
                    )
                    turns.append({"role": "assistant", "content": reply})
                    turns.append({"role": "user", "content": payload})
                    continue
                if recoveries < 2 and last_failed:
                    recoveries += 1
                    payload = _recover_message()
                    print(
                        "model stopped early; asking for the next tool_call",
                        file=sys.stderr,
                        flush=True,
                    )
                    turns.append({"role": "assistant", "content": reply})
                    turns.append({"role": "user", "content": payload})
                    continue
                turns.append({"role": "assistant", "content": reply})
                if not note and reply.strip():
                    show(reply)
                finished = True
                break
            results: list[dict[str, Any]] = []
            for call in calls:
                if call.error:
                    results.append(
                        {"tool": call.tool, "ok": False, "error": call.error}
                    )
                    continue
                blocked = _mutation_error(
                    call.tool,
                    plan=plan,
                    planned_before=planned_before,
                )
                if blocked:
                    results.append({"tool": call.tool, "ok": False, "error": blocked})
                    print(
                        f"blocked {call.tool}: plan required first",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                _print_call(call)
                results.append(
                    execute_tool(
                        call.tool,
                        call.arguments,
                        workspace=workspace,
                        index_path=index_path,
                        cache_dir=cache_dir,
                        max_chars=min(DEFAULT_TOOL_CHARS, max_result_chars),
                    )
                )
                if results[-1].get("ok"):
                    tools_ran = True
            last_failed = any(not item.get("ok") for item in results)
            if not last_failed:
                recoveries = 0
            payload = _cap(
                "\n".join(format_tool_result(item) for item in results),
                max_result_chars,
            )
            turns.append({"role": "assistant", "content": reply})
            turns.append({"role": "tool", "content": payload})
        if not finished:
            message = f"stopped after {max_rounds} tool rounds"
            log.warn(message)
            print(f"error: {message}", file=sys.stderr)
            turns.append({"role": "assistant", "content": message})
        pending = ""
    return turns


def run_agent(
    config: BotConfig,
    home: BotHome,
    task: str,
    *,
    max_rounds: int | None,
    output_dir: Path | None,
    headed: bool,
) -> int:
    """Open the Edge session, run the tool loop, and write the transcript."""
    from critique_bot.browser import BrowserError
    from critique_bot.chat_client import ChatError
    from critique_bot.provider import open_provider

    started = datetime.now(timezone.utc)
    turns: list[dict[str, str]] = []
    try:
        instructions = load_agent_instructions()
    except OSError as exc:
        log.error(str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        with open_provider(config, headed=headed) as provider:
            with provider.session() as session:
                try:
                    turns = run_agent_loop(
                        session,
                        workspace=home.root,
                        index_path=home.index_path,
                        cache_dir=home.cache_dir,
                        first_task=task,
                        max_rounds=max_rounds,
                        max_result_chars=config.max_prompt_chars,
                        seed=seed_message(home.root, instructions),
                    )
                except Exception:
                    page = getattr(session, "page", None)
                    if page is not None:
                        from critique_bot.output import save_failure

                        save_failure(page, home.sessions_dir)
                    raise
    except (BrowserError, ChatError) as exc:
        log.error(str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        log.exception(f"unexpected failure: {exc}")
        print(f"error: unexpected failure: {exc}", file=sys.stderr)
        return 1

    if not turns:
        return 0
    finished = datetime.now(timezone.utc)
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
    stamp = started.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    write_output(home.sessions_dir / stamp, body, payload, stem="agent", print_body=False)
    if output_dir is not None:
        write_output(output_dir, body, payload, stem="agent", print_body=False)
    return 0


def _seed_session(
    session: Any,
    seed: str,
    turns: list[dict[str, str]],
    *,
    workspace: Path,
    index_path: Path | None,
    cache_dir: Path | None,
    max_result_chars: int,
    show: Callable[[str], None],
) -> None:
    """Send the tool instructions once. Tool calls in the ack are still run."""
    print("seeding tool instructions", file=sys.stderr, flush=True)
    payload = seed
    turns.append({"role": "user", "content": seed})
    for _ in range(3):
        with log.loading("Waiting for the assistant..."):
            reply = session.send(payload)
        calls, unclosed = parse_tool_calls(reply)
        note = commentary(reply)
        if note:
            show(note)
        turns.append({"role": "assistant", "content": reply})
        if unclosed or _idle_tool_reply(getattr(session, "last_detail", None), reply):
            payload = format_tool_result(
                {
                    "tool": "",
                    "ok": False,
                    "error": (
                        "reply was truncated before the tool call closed; "
                        "resend complete tool_call blocks"
                    ),
                }
            )
            turns.append({"role": "tool", "content": payload})
            continue
        if not calls:
            return
        results = []
        for call in calls:
            if call.error:
                results.append({"tool": call.tool, "ok": False, "error": call.error})
                continue
            blocked = _mutation_error(call.tool, plan="", planned_before=False)
            if blocked:
                results.append({"tool": call.tool, "ok": False, "error": blocked})
                continue
            _print_call(call)
            results.append(
                execute_tool(
                    call.tool,
                    call.arguments,
                    workspace=workspace,
                    index_path=index_path,
                    cache_dir=cache_dir,
                    max_chars=min(DEFAULT_TOOL_CHARS, max_result_chars),
                )
            )
        payload = _cap(
            "\n".join(format_tool_result(item) for item in results),
            max_result_chars,
        )
        turns.append({"role": "tool", "content": payload})


def _agent_prompt_path() -> Path:
    package_dir = Path(__file__).resolve().parent
    candidates = [
        Path.cwd() / "prompts" / "agent.txt",
        Path(sys.executable).resolve().parent / "prompts" / "agent.txt",
        package_dir / "prompts" / "agent.txt",
    ]
    try:
        candidates.append(package_dir.parents[2] / "prompts" / "agent.txt")
    except IndexError:
        pass
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.is_file():
            return path
    raise OSError("agent instructions not found (prompts/agent.txt)")


def format_transcript(turns: list[dict[str, str]]) -> str:
    headings = {"user": "You", "assistant": "Assistant", "tool": "Tool"}
    parts = ["# Agent", ""]
    for turn in turns:
        parts.append(f"## {headings.get(turn.get('role', ''), 'Note')}")
        parts.append("")
        parts.append(turn.get("content", "").rstrip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _parse_block(body: str) -> list[ToolCall]:
    text = body.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    objects = _json_objects(text)
    if not objects:
        try:
            data = _loads_lenient(text)
        except json.JSONDecodeError as exc:
            return [ToolCall(tool="", arguments={}, error=f"invalid tool JSON: {exc}")]
        return [_call_from_data(data)]
    calls: list[ToolCall] = []
    for blob in objects:
        try:
            data = _loads_lenient(blob)
        except json.JSONDecodeError as exc:
            calls.append(ToolCall(tool="", arguments={}, error=f"invalid tool JSON: {exc}"))
            continue
        calls.append(_call_from_data(data))
    return calls


def _call_from_data(data: Any) -> ToolCall:
    if not isinstance(data, dict):
        return ToolCall(tool="", arguments={}, error="tool call must be a JSON object")
    name = data.get("tool", data.get("name"))
    if not isinstance(name, str) or not name.strip():
        return ToolCall(tool="", arguments={}, error="tool call needs a tool name")
    if "arguments" in data:
        arguments = data.get("arguments")
    else:
        arguments = data.get("args", {})
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try:
            arguments = _loads_lenient(arguments)
        except json.JSONDecodeError as exc:
            return ToolCall(
                tool=name.strip(),
                arguments={},
                error=f"arguments must be a JSON object: {exc}",
            )
    if not isinstance(arguments, dict):
        return ToolCall(tool=name.strip(), arguments={}, error="arguments must be a JSON object")
    return ToolCall(tool=name.strip(), arguments=arguments)


def _loads_lenient(text: str) -> Any:
    cleaned = text.strip().lstrip("\ufeff")
    cleaned = (
        cleaned.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )
    candidates = [cleaned, _TRAILING_COMMA_RE.sub(r"\1", cleaned)]
    extracted = _json_objects(cleaned)
    for blob in extracted:
        if blob not in candidates:
            candidates.append(blob)
            candidates.append(_TRAILING_COMMA_RE.sub(r"\1", blob))
    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        normalized = (
            candidate.replace(": True", ": true")
            .replace(": False", ": false")
            .replace(": None", ": null")
        )
        for blob in (candidate, normalized):
            try:
                return json.loads(blob)
            except json.JSONDecodeError as exc:
                last_error = exc
    if last_error is not None:
        raise last_error
    raise json.JSONDecodeError("invalid tool JSON", cleaned, 0)


def _json_objects(text: str) -> list[str]:
    found: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    start: int | None = None
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
            continue
        if char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                found.append(text[start : index + 1])
                start = None
    return found


def _exact_done(text: str) -> bool:
    compact = re.sub(r"[^a-z]+", " ", text.lower()).strip()
    return compact == "done"


def _no_edit_needed(text: str) -> bool:
    """True when the model says the task needs no further edit, or replies DONE.

    That reply ends the task. A promise to keep editing does not, unless the
    whole reply is DONE.
    """
    if _exact_done(text):
        return True
    normalized = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    if any(
        marker in normalized
        for marker in ("i'll ", "i will ", "let me ", "going to ", "next i")
    ):
        return False
    markers = (
        "no edit needed",
        "no edits needed",
        "no edit is needed",
        "no further edit",
        "no further change",
        "no change needed",
        "no changes needed",
        "nothing to change",
        "nothing to edit",
        "already present",
        "already done",
        "already applied",
        "already contains",
        "already in the file",
        "no modification",
        "does not need an edit",
        "does not need to edit",
        "do not need to edit",
        "don't need to edit",
        "no additional change",
    )
    return any(marker in normalized for marker in markers)


def _stalls(text: str) -> bool:
    """True when the reply asks the user, refuses, or only promises work.

    Wording varies. A question mark, "unable", "not exposed", and "no tool"
    are the same stop as "what would you like me to change?".
    """
    normalized = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    if "?" in normalized:
        return True
    markers = (
        "what would you like",
        "what should i",
        "would you like",
        "let me know",
        "for example",
        "shall i",
        "do you want",
        "what do you want",
        "i can't",
        "i cannot",
        "unable",
        "not able",
        "aren't available",
        "are not available",
        "not available",
        "isn't available",
        "not exposed",
        "no tool",
        "no repository",
        "file-operation",
        "file operation",
        "don't have",
        "do not have",
        "i won't",
        "i will not",
        "i'll ",
        "i will ",
        "let me ",
        "next i",
        "going to ",
    )
    return any(marker in normalized for marker in markers)


def _promises_more(text: str) -> bool:
    return _stalls(text)


def _plan_text(reply: str) -> str | None:
    """Return the plan body, or None when the reply has no plan tag.

    A tag that is present but blank still returns an empty string so the
    loop can ask for a real plan instead of treating the reply as the answer.
    """
    parts = [part.strip() for part in _PLAN_RE.findall(reply)]
    if not parts:
        return None
    return "\n".join(part for part in parts if part)


def _mutation_error(tool: str, *, plan: str, planned_before: bool) -> str | None:
    if tool not in _MUTATING:
        return None
    if len(plan) < _MIN_PLAN_CHARS or not planned_before:
        if len(plan) < _MIN_PLAN_CHARS:
            return (
                "no plan recorded. Send a <plan> block in its own reply before "
                "edit_file, write_files, delete_file, or apply_patch. "
                "Name each file, the change, and the check. "
                "Do not put those tools in the plan reply."
            )
        return (
            "plan recorded. Send the edit on the next reply. "
            "This reply must not also call edit_file, write_files, "
            "delete_file, or apply_patch."
        )
    return None


def _replace_span(
    text: str,
    old: str,
    new: str,
    *,
    replace_all: bool,
) -> tuple[str | None, int, str]:
    """Return updated text, replacement count, and a short note.

    The updated text is None when nothing matched. Exact text wins. A unique
    match that differs only by the read_files ``N|`` prefix or by surrounding
    whitespace is accepted so a mis-copied span still edits the right place.
    """
    haystack = text.replace("\r\n", "\n").replace("\r", "\n")
    needle = _strip_read_prefix(old).replace("\r\n", "\n").replace("\r", "\n")
    replacement = _strip_read_prefix(new).replace("\r\n", "\n").replace("\r", "\n")
    if _same_lines(needle, replacement):
        return None, 0, (
            "old_string and new_string are the same text; the file was not changed. "
            "Send a real difference, or reply DONE if no edit is needed."
        )
    count = haystack.count(needle)
    if count == 1 or (count > 1 and replace_all):
        times = count if replace_all else 1
        if replace_all:
            updated = haystack.replace(needle, replacement)
        else:
            updated = haystack.replace(needle, replacement, 1)
        note = ""
        if needle != old.replace("\r\n", "\n").replace("\r", "\n"):
            note = "removed N| prefixes from old_string"
        return updated, times, note
    if count > 1:
        return None, 0, f"old_string matched {count} times; pass replace_all or a longer string"
    span = _unique_loose_span(haystack, needle)
    if span == "ambiguous":
        return None, 0, "old_string matched more than once after ignoring whitespace; pass a longer string"
    if span is None:
        return None, 0, "old_string was not found" + _near_miss(haystack, needle)
    start, end = span
    return haystack[:start] + replacement + haystack[end:], 1, "matched ignoring surrounding whitespace"


def _same_lines(left: str, right: str) -> bool:
    def lines(text: str) -> list[str]:
        parts = text.split("\n")
        if parts and parts[-1] == "":
            parts = parts[:-1]
        return [line.strip() for line in parts]

    return lines(left) == lines(right)


def _strip_read_prefix(text: str) -> str:
    return "\n".join(_LINE_NO_RE.sub("", line, count=1) for line in text.split("\n"))


def _unique_loose_span(text: str, old: str) -> tuple[int, int] | str | None:
    lines = old.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines:
        return None
    parts = [r"[ \t]*" + re.escape(line.strip()) + r"[ \t]*" for line in lines]
    pattern = r"(?:^|\n)(" + "\n".join(parts) + r")(?=\n|$)"
    matches = list(re.finditer(pattern, text))
    if not matches:
        return None
    if len(matches) > 1:
        return "ambiguous"
    return matches[0].start(1), matches[0].end(1)


def _near_miss(text: str, old: str) -> str:
    probe = ""
    for line in old.split("\n"):
        stripped = line.strip()
        if len(stripped) >= 12:
            probe = stripped
            break
    if not probe:
        return ""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if probe not in line:
            continue
        begin = max(0, index - 2)
        end = min(len(lines), index + 3)
        window = "\n".join(f"{number}|{lines[number - 1]}" for number in range(begin + 1, end + 1))
        return f"; nearby:\n{window}"
    return ""


def _idle_tool_reply(detail: Any, reply: str) -> bool:
    """True when an idle reply contains a tool tag that did not finish."""
    if not isinstance(detail, dict) or detail.get("completion") != COMPLETION_IDLE:
        return False
    if "<tool_call" not in reply.lower():
        return False
    calls, unclosed = parse_tool_calls(reply)
    return unclosed or not calls


def _print_call(call: ToolCall) -> None:
    preview = json.dumps(call.arguments, ensure_ascii=False)
    if len(preview) > 180:
        preview = preview[:177] + "..."
    print(f"tool {call.tool} {preview}", file=sys.stderr, flush=True)


def _emit(text: str) -> None:
    log.print_safe(text.rstrip(), flush=True)
    log.print_safe(flush=True)


def _read_message() -> str | None:
    if not sys.stdin.isatty():
        return None
    chunks: list[str] = []
    while True:
        prefix = "You> " if not chunks else "... "
        try:
            line = input(prefix)
        except EOFError:
            print(flush=True)
            if chunks:
                return "\n".join(chunks).rstrip()
            return None
        except KeyboardInterrupt:
            print(flush=True)
            return None
        if not chunks:
            if not line.strip():
                continue
            if line.strip().lower() in _QUIT:
                return None
        if line.endswith("\\") and not line.endswith("\\\\"):
            chunks.append(line[:-1])
            continue
        chunks.append(line)
        return "\n".join(chunks).rstrip()


def _cap(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    note = "\n...[truncated; narrow with path, offset, or a tighter pattern]"
    keep = max(0, limit - len(note))
    return text[:keep] + note


def _resolve(workspace: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


def _rel(workspace: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return str(path)


def _tool_ok(name: str, output: str, *, max_chars: int) -> dict[str, Any]:
    return {"tool": name, "ok": True, "output": _cap(output, max_chars)}


def _tool_err(name: str, error: str, *, output: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"tool": name, "ok": False, "error": error}
    if output:
        result["output"] = output
    return result


def _list_files(args, *, workspace, index_path, cache_dir, max_chars, command_timeout, runner):
    del index_path, cache_dir, command_timeout, runner
    raw = args.get("path") or "."
    if not isinstance(raw, str):
        return _tool_err("list_files", "path must be a string")
    raw = raw.strip() or "."
    target = _resolve(workspace, raw)
    if not target.exists():
        return _tool_err("list_files", f"not found: {raw}")
    glob = args.get("glob")
    if glob is not None and not isinstance(glob, str):
        return _tool_err("list_files", "glob must be a string")
    try:
        max_entries = int(args.get("max_entries") or 200)
    except (TypeError, ValueError):
        return _tool_err("list_files", "max_entries must be an integer")
    max_entries = min(max(max_entries, 1), _MAX_LIST_ENTRIES)
    rows: list[str] = []
    if target.is_file():
        rows.append(f"f {_rel(workspace, target)}")
    else:
        recursive = bool(glob and ("**" in glob or "/" in glob or "\\" in glob))
        if recursive:
            matches = _walk_files(workspace, target, glob)
        else:
            matches = []
            try:
                children = sorted(target.iterdir(), key=lambda item: item.name)
            except OSError as exc:
                return _tool_err("list_files", str(exc))
            for child in children:
                if glob and not Path(child.name).match(glob):
                    continue
                kind = "d" if child.is_dir() else "f"
                matches.append(f"{kind} {_rel(workspace, child)}")
        rows.extend(matches[:max_entries])
        if len(matches) > max_entries:
            rows.append(
                f"... {len(matches) - max_entries} more; narrow path or glob. "
                "Generated trees (out, prebuilts, build, intermediates) are skipped."
            )
    return _tool_ok("list_files", "\n".join(rows) if rows else "(empty)", max_chars=max_chars)


def _walk_files(workspace: Path, root: Path, glob: str | None) -> list[str]:
    rows: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = _rel(workspace, path)
            if glob and not Path(rel).match(glob):
                continue
            rows.append(f"f {rel}")
    return rows


def _read_files(args, *, workspace, index_path, cache_dir, max_chars, command_timeout, runner):
    del index_path, cache_dir, command_timeout, runner
    paths = args.get("paths")
    if paths is None and args.get("path"):
        paths = [args.get("path")]
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not paths:
        return _tool_err("read_files", "paths must be a list of files")
    offset = args.get("offset", args.get("start_line", args.get("line")))
    limit = args.get("limit", args.get("count"))
    end_line = args.get("end_line")
    try:
        start = int(offset) if offset is not None else 1
        if limit is None and end_line is not None:
            count = int(end_line) - start + 1
        else:
            count = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        return _tool_err("read_files", "offset and limit must be integers")
    if start < 1:
        return _tool_err("read_files", "offset starts at 1")
    parts: list[str] = []
    file_budget = max(1500, max_chars // max(len(paths), 1))
    for raw in paths:
        if not isinstance(raw, str):
            return _tool_err("read_files", "each path must be a string")
        path = _resolve(workspace, raw)
        parts.append(f"--- {_rel(workspace, path)} ---")
        if not path.is_file():
            parts.append(f"not found: {raw}")
            continue
        if looks_binary_path(str(path)):
            parts.append(f"binary file omitted: {raw}")
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            parts.append(str(exc))
            continue
        if looks_binary_bytes(data):
            parts.append(f"binary file omitted: {raw}")
            continue
        lines = data.decode("utf-8", "replace").splitlines()
        total = len(lines)
        window = count if count is not None else _DEFAULT_READ_LINES
        if window < 1:
            return _tool_err("read_files", "limit must be at least 1")
        selected = lines[start - 1 : start - 1 + window]
        numbered: list[str] = []
        used = 0
        for index, line in enumerate(selected):
            piece = f"{start + index}|{line}"
            if numbered and used + len(piece) + 1 > file_budget:
                break
            numbered.append(piece)
            used += len(piece) + 1
        parts.append("\n".join(numbered))
        next_offset = start + len(numbered)
        if next_offset <= total:
            parts.append(
                f"... {total} lines; next offset={next_offset} "
                f"limit={_DEFAULT_READ_LINES}. Do not edit a span you have not seen."
            )
    return _tool_ok("read_files", "\n".join(parts), max_chars=max_chars)


def _search_code(args, *, workspace, index_path, cache_dir, max_chars, command_timeout, runner):
    del cache_dir, command_timeout, runner
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return _tool_err("search_code", "pattern is required")
    try:
        matcher = re.compile(pattern)
    except re.error as exc:
        return _tool_err("search_code", f"invalid pattern: {exc}")
    raw_path = args.get("path") or "."
    if not isinstance(raw_path, str):
        return _tool_err("search_code", "path must be a string")
    root = _resolve(workspace, raw_path)
    if not root.exists():
        return _tool_err("search_code", f"not found: {raw_path}")
    glob = args.get("glob")
    if glob is not None and not isinstance(glob, str):
        return _tool_err("search_code", "glob must be a string")
    try:
        limit = int(args.get("head_limit") or 50)
    except (TypeError, ValueError):
        return _tool_err("search_code", "head_limit must be an integer")
    limit = min(max(limit, 1), 200)
    prefix = ""
    if root != workspace.resolve():
        prefix = _rel(workspace, root)
    lines: list[str] = []
    seen: set[tuple[str, int]] = set()
    if index_path is not None:
        for rel, line, kind, name in search_symbols(
            index_path, pattern, limit=limit, path_prefix=prefix
        ):
            if glob and not Path(rel).match(glob):
                continue
            lines.append(f"{rel}:{line}: {kind} {name}")
            seen.add((rel, line))
            if len(lines) >= limit:
                break
    if len(lines) < limit:
        for rel, line_no, text in _scan_text(workspace, root, matcher, glob, limit):
            if (rel, line_no) in seen:
                continue
            lines.append(f"{rel}:{line_no}: {text}")
            if len(lines) >= limit:
                break
    return _tool_ok(
        "search_code",
        "\n".join(lines) if lines else "(no matches)",
        max_chars=max_chars,
    )


def _scan_text(workspace, root, matcher, glob, limit):
    files: list[Path] = []
    if root.is_file():
        files = [root]
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
            for name in filenames:
                files.append(Path(dirpath) / name)
    hits = []
    for path in files:
        rel = _rel(workspace, path)
        if glob and not Path(rel).match(glob):
            continue
        if looks_binary_path(rel):
            continue
        try:
            if path.stat().st_size > _SCAN_MAX_BYTES:
                continue
        except OSError:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if looks_binary_bytes(data):
            continue
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), start=1):
            if matcher.search(line) is None:
                continue
            hits.append((rel, number, line.strip()))
            if len(hits) >= limit:
                return hits
    return hits


def _write_files(args, *, workspace, index_path, cache_dir, max_chars, command_timeout, runner):
    del cache_dir, command_timeout, runner
    files = args.get("files")
    if files is None and args.get("path") is not None:
        files = [{"path": args.get("path"), "contents": args.get("contents", "")}]
    if not isinstance(files, list) or not files:
        return _tool_err("write_files", "files must be a list of {path, contents}")
    written: list[str] = []
    errors: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            errors.append("each file must be an object")
            continue
        raw = item.get("path")
        contents = item.get("contents")
        if not isinstance(raw, str) or not raw:
            errors.append("file path is required")
            continue
        if not isinstance(contents, str):
            errors.append(f"contents must be a string: {raw}")
            continue
        path = _resolve(workspace, raw)
        if path == workspace.resolve():
            errors.append("refusing to write the workspace root")
            continue
        if path.exists() and path.is_dir():
            errors.append(f"path is a directory: {raw}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        if index_path is not None:
            refresh_path(workspace, index_path, path)
        written.append(_rel(workspace, path))
    summary = "wrote " + ", ".join(written) if written else "wrote nothing"
    if errors:
        return _tool_err(
            "write_files",
            "; ".join(errors),
            output=summary,
        )
    return _tool_ok("write_files", summary, max_chars=max_chars)


def _edit_file(args, *, workspace, index_path, cache_dir, max_chars, command_timeout, runner):
    del cache_dir, command_timeout, runner
    raw = args.get("path")
    old = args.get("old_string")
    new = args.get("new_string")
    if not isinstance(raw, str) or not raw:
        return _tool_err("edit_file", "path is required")
    if not isinstance(old, str) or old == "":
        return _tool_err("edit_file", "old_string is empty")
    if not isinstance(new, str):
        return _tool_err("edit_file", "new_string must be a string")
    replace_all = bool(args.get("replace_all"))
    path = _resolve(workspace, raw)
    if not path.is_file():
        return _tool_err("edit_file", f"not found: {raw}")
    text = path.read_text(encoding="utf-8")
    updated, times, detail = _replace_span(text, old, new, replace_all=replace_all)
    if updated is None:
        return _tool_err("edit_file", detail)
    path.write_text(updated, encoding="utf-8")
    if index_path is not None:
        refresh_path(workspace, index_path, path)
    message = f"updated {_rel(workspace, path)} ({times})"
    if detail:
        message += f"; {detail}"
    return _tool_ok("edit_file", message, max_chars=max_chars)


def _delete_file(args, *, workspace, index_path, cache_dir, max_chars, command_timeout, runner):
    del cache_dir, command_timeout, runner
    raw = args.get("path")
    if not isinstance(raw, str) or not raw:
        return _tool_err("delete_file", "path is required")
    path = _resolve(workspace, raw)
    if path == workspace.resolve():
        return _tool_err("delete_file", "refusing to delete the workspace root")
    if path.is_dir():
        return _tool_err("delete_file", f"path is a directory: {raw}")
    if not path.is_file():
        return _tool_err("delete_file", f"not found: {raw}")
    path.unlink()
    if index_path is not None:
        refresh_path(workspace, index_path, path)
    return _tool_ok("delete_file", f"deleted {_rel(workspace, path)}", max_chars=max_chars)


def _run_command(args, *, workspace, index_path, cache_dir, max_chars, command_timeout, runner):
    del index_path, cache_dir
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return _tool_err("run_command", "command is required")
    try:
        timeout = float(args.get("timeout") or command_timeout)
    except (TypeError, ValueError):
        return _tool_err("run_command", "timeout must be a number")
    timeout = min(max(timeout, 1), _MAX_COMMAND_TIMEOUT)
    argv = command_argv(command)
    try:
        proc = runner(
            argv,
            cwd=str(workspace),
            capture_output=True,
            check=False,
            timeout=timeout,
            shell=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        output = _decode(exc.stdout) + _decode(exc.stderr)
        return _tool_err("run_command", f"timed out after {timeout:.0f}s", output=_cap(output, max_chars))
    output = _command_output(proc)
    ok = int(getattr(proc, "returncode", 1) or 0) == 0
    if ok:
        return _tool_ok("run_command", output or "(no output)", max_chars=max_chars)
    return _tool_err("run_command", "command failed", output=_cap(output, max_chars))


def _git_status(args, *, workspace, index_path, cache_dir, max_chars, command_timeout, runner):
    del args, index_path, cache_dir, command_timeout
    return _git(workspace, ["status", "--short", "--branch"], max_chars=max_chars, runner=runner, tool="git_status")


def _git_diff(args, *, workspace, index_path, cache_dir, max_chars, command_timeout, runner):
    del index_path, cache_dir, command_timeout
    git_args = ["diff"]
    if args.get("staged"):
        git_args.append("--cached")
    raw = args.get("path")
    if raw:
        if not isinstance(raw, str):
            return _tool_err("git_diff", "path must be a string")
        git_args.extend(["--", raw])
    return _git(workspace, git_args, max_chars=max_chars, runner=runner, tool="git_diff")


def _apply_patch(args, *, workspace, index_path, cache_dir, max_chars, command_timeout, runner):
    del command_timeout
    patch = args.get("patch")
    if not isinstance(patch, str) or not patch.strip():
        return _tool_err("apply_patch", "patch is required")
    if not patch.endswith("\n"):
        patch += "\n"
    folder = cache_dir or Path(tempfile.mkdtemp(prefix="bot-patch-"))
    folder.mkdir(parents=True, exist_ok=True)
    patch_path = folder / "apply.patch"
    patch_path.write_text(patch, encoding="utf-8")
    result = _git(
        workspace,
        ["apply", "--whitespace=nowarn", "--unsafe-paths", str(patch_path)],
        max_chars=max_chars,
        runner=runner,
        tool="apply_patch",
    )
    if result.get("ok") and index_path is not None:
        for rel in _patched_paths(patch):
            refresh_path(workspace, index_path, workspace / rel)
    return result


def _patched_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ "):
            raw = line[4:].split("\t", 1)[0].strip()
            if raw.startswith("b/"):
                raw = raw[2:]
            if raw and raw != "/dev/null":
                paths.append(raw)
    return paths


def _git(workspace, git_args, *, max_chars, runner, tool) -> dict[str, Any]:
    proc = runner(
        ["git", "-C", str(workspace), *git_args],
        capture_output=True,
        check=False,
        shell=False,
    )
    output = _command_output(proc)
    if int(getattr(proc, "returncode", 1) or 0) != 0:
        return _tool_err(tool, output or "git failed", output=output)
    return _tool_ok(tool, output or "(empty)", max_chars=max_chars)


def _command_output(proc: Any) -> str:
    code = int(getattr(proc, "returncode", 1) or 0)
    stdout = _clean_command_text(_decode(getattr(proc, "stdout", b""))).rstrip()
    stderr = _clean_command_text(_decode(getattr(proc, "stderr", b""))).rstrip()
    parts = [f"exit {code}"]
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)
    return "\n".join(parts)


def _decode(data: object) -> str:
    if isinstance(data, str):
        return data
    if not isinstance(data, (bytes, bytearray)):
        return ""
    raw = bytes(data)
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    if len(raw) >= 4 and raw[1] == 0 and raw[3] == 0:
        return raw.decode("utf-16-le", "replace")
    return raw.decode("utf-8", "replace")


def _clean_command_text(text: str) -> str:
    """Turn a PowerShell CLIXML error record into the message it carried."""
    if "#<" not in text or "CLIXML" not in text:
        return text
    messages = re.findall(r"<S\b[^>]*>(.*?)</S>", text, re.DOTALL)
    if not messages:
        return re.sub(r"#<\s*CLIXML[\s\S]*", "", text).strip()
    lines: list[str] = []
    for message in messages:
        decoded = re.sub(
            r"_x([0-9A-Fa-f]{4})_",
            lambda match: chr(int(match.group(1), 16)),
            message,
        )
        decoded = decoded.replace("\r", "").strip()
        if decoded:
            lines.append(decoded)
    return "\n".join(lines)


_HANDLERS = {
    "list_files": _list_files,
    "read_files": _read_files,
    "search_code": _search_code,
    "write_files": _write_files,
    "edit_file": _edit_file,
    "delete_file": _delete_file,
    "run_command": _run_command,
    "git_status": _git_status,
    "git_diff": _git_diff,
    "apply_patch": _apply_patch,
}
