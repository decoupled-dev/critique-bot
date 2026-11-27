# SPDX-License-Identifier: Apache-2.0
"""The model-facing text: prompts/agent.txt sections, tool formats, the task message.

Model-facing words live only in the prompts folder; the fallbacks below are used when a
section is missing there.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from critique_bot import log, roles
from critique_bot import tool_api as agent_tools


_SECTION_RE = re.compile(r"^<<<([A-Z_]+)>>>\s*$", re.MULTILINE)


_FALLBACKS = {
    "TASK_PREFIX": "Print tool_call blocks to act. Do not refuse.",
    "PLAN_MODE": (
        "PLAN MODE. Do not change anything yet. Investigate with read-only tools, then reply with a plan "
        "and no tool_call: the goal, what you found (with paths), the steps (files and the change in each), "
        "risks, and how you will verify. Do not reply COMPLETED."
    ),
    "PLAN_NEEDED": "Plan mode is on. Reply with the plan in words now, with no tool_call and no COMPLETED.",
    "PLAN_ONLY": (
        "Plan mode is on, so nothing that changes files or runs a build was run. Use read-only tools only, "
        "then reply with the plan and no tool_call."
    ),
    "PLAN_APPROVED": (
        "The user approved the plan. Plan mode is off. Carry it out now: write the todo list from the plan, "
        "then send the tool_call blocks for the first steps. Verify, then reply COMPLETED."
    ),
    "PLAN_REVISE": "The user wants the plan changed:\n{feedback}\nInvestigate if needed, then reply with the revised plan and no tool_call.",
    "HAND_BACK": (
        "Do not hand the work to the user. run_command runs on the user's machine, so run the build or "
        "command yourself now. If it fails, read SUMMARY and HINTS in its result, fix the cause, and run it again."
    ),
    "HELPER": (
        "You are {name}, a helper chat doing one part of a larger task that another chat coordinates. "
        "It cannot see this chat; your final reply is all it gets.\nBrief:\n{brief}\n"
        "Files you may change: {files}. You may read any file. {commands} "
        "When done, reply COMPLETED and then a short report: what you found or changed, with paths and line numbers, "
        "and what the coordinator still has to do."
    ),
    "HANDOFF": (
        "This chat is about to be replaced by a new one, which will not see this conversation. Write a handoff "
        "note for it now, with no tool_call: the task and what done means; what you learned about the code "
        "(files, symbols, line numbers, conventions); what you changed and why; what is left, in order; and "
        "what failed or must be avoided. At most 300 words."
    ),
    "AUTO_DECIDE": (
        "Auto mode is on and the user is not answering questions. Decide yourself: pick the safest reasonable "
        "option, say which in one line, and continue the task."
    ),
    "NUDGE": (
        "No tool_call block was found, so nothing was run. "
        "A bare JSON object such as {tool:\"write_file\"} is not a tool call. "
        "Reply with a tool_call block now.\n"
        "<tool_call>\n"
        '{"tool": "list_files", "arguments": {"path": "."}}\n'
        "</tool_call>"
    ),
    "RECOVER": (
        "The previous step did not finish. Do not ask what to change. Do not say the tools "
        "are unavailable. A bare JSON object is not a tool call. Send the next tool_call now, for example:\n"
        "<tool_call>\n"
        '{"tool": "read_files", "arguments": {"path": "{path}"}}\n'
        "</tool_call>"
    ),
    "ANSWER": (
        "The task is a question. Reply with the answer in words and no tool_call when you "
        "already know it or a tool result above shows it. If you still need a file or a command, "
        "send one tool_call. Do not ask what to change."
    ),
    "STATE": (
        "STATE step {step}\nTask: {task}\nRead: {reads}\nChanged: {edits}\n"
        "Last command: {command}\n"
        "Next: the next tool_call, or the answer when the task is only a question, or COMPLETED. "
        "FAILED or BLOCKED must say what failed and why."
    ),
    "PLAN_NOTED": "Plan noted. Send the tool_call for its first step now, with no other words.",
    "JOBS_RUNNING": (
        "You replied COMPLETED, but background jobs you started are still running:\n{jobs}\n"
        "If the task needs a job's result (a build or test), wait for it: command_output with that "
        "job_id and until \"exit\". Stop servers or watchers you no longer need with kill_command. "
        "Then reply COMPLETED again."
    ),
    "NOTHING_CHANGED": (
        "No file changed in this task. If the task needs a change, send the edit now. "
        "If no change is needed, reply COMPLETED again."
    ),
    "CHECK_FAILED": (
        "The project check above failed after your edits. Read the failure, fix it, and "
        "reply COMPLETED when it passes. If it cannot be fixed, reply FAILED and say what failed and why."
    ),
    "WHY_FAILED": (
        "That reply was only the word. Say what failed and why, copied from the tool result. "
        "Start with FAILED or BLOCKED, then one or two sentences. "
        "Send a tool_call instead if you can still fix it."
    ),
    "TRUNCATED": (
        "Your last reply was truncated (cut off) inside a tool_call{call}, so that call did not run. "
        "Continue from there: resend only that one call, complete, and nothing before it. "
        "If it writes a long file, split it: write_files with the first part, then edit_file to add the rest."
    ),
    "FABRICATED": (
        "Your reply contained a tool_result. Only the program writes tool_result blocks; you never do. "
        "Everything from that point on was ignored. The real results of the calls before it are above."
    ),
    "LOOP": (
        "You sent the same tool calls {count} times in a row. They were not run again: the results are above. "
        "Use them and take a different next step, or finish."
    ),
    "VERIFY": (
        "You changed files but ran nothing to check them. Run the relevant tests or build with run_command now. "
        "If there is truly nothing to run, reply COMPLETED again."
    ),
    "DENIED": (
        "The user denied that call, so it did not run. Do not send it again unchanged. "
        "Change the approach, or use ask_user if you need the user's decision."
    ),
    "USER_ANSWER": "The user answered your question:\n{answer}\nContinue the task.",
    "RESUME": (
        "CONTINUING IN A NEW CHAT. The previous chat grew long, so this is a fresh one. "
        "The rules above apply. Summary of the work so far:"
    ),
}


TOOL_FORMAT_TAGS = "tags"


TOOL_FORMAT_OPENAI = "openai"


#: Native tool calling: the server's ``tools`` / ``tool_calls`` (a local model). The session
#: translates to and from the Responses format, so the engine parses replies and sends
#: results as with TOOL_FORMAT_OPENAI; only the prompts differ (prompts/agent_native.txt).
TOOL_FORMAT_NATIVE = "native"


TOOL_FORMATS = (TOOL_FORMAT_TAGS, TOOL_FORMAT_OPENAI, TOOL_FORMAT_NATIVE)


#: Formats whose replies and results use the Responses wire shape inside the engine.
_RESPONSES_WIRE = (TOOL_FORMAT_OPENAI, TOOL_FORMAT_NATIVE)


#: What crit uses when ``"tool_format"`` is not set: the OpenAI Responses format.
DEFAULT_TOOL_FORMAT = TOOL_FORMAT_OPENAI


def tool_format_of(settings: dict[str, Any] | None, default: str = TOOL_FORMAT_TAGS) -> str:
    """``"openai"`` or ``"tags"`` from the ``"tool_format"`` setting, else ``default``.

    crit's sessions pass DEFAULT_TOOL_FORMAT (openai). The engine called directly with no
    setting keeps tags, the format its own tests are written in.
    """
    raw = str((settings or {}).get("tool_format") or "").strip().lower()
    if raw in {"native", "tools", "function-calling"}:
        return TOOL_FORMAT_NATIVE
    if raw in {"openai", "responses", "openai-responses", "json"}:
        return TOOL_FORMAT_OPENAI
    if raw in {"tags", "tag", "tool_call", "xml", "legacy"}:
        return TOOL_FORMAT_TAGS
    return default if default in TOOL_FORMATS else TOOL_FORMAT_TAGS


def _read_sections(path: Path) -> dict[str, str]:
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
    return sections


def load_prompt_sections(tool_format: str = TOOL_FORMAT_TAGS) -> dict[str, str]:
    """Read prompts/agent.txt. Model-facing text lives only in the prompts folder.

    With the OpenAI format, prompts/agent_openai.txt replaces the sections that describe
    how to call tools; every other section is shared. ``sections["_FORMAT"]`` records the
    format, so a task (and its helper chats) parse replies and send results to match.
    """
    sections = _read_sections(_agent_prompt_path())
    if tool_format == TOOL_FORMAT_OPENAI:
        from critique_bot import openai_format

        overlay = _read_sections(_agent_prompt_path("agent_openai.txt"))
        if "SYSTEM" in overlay:
            overlay["SYSTEM"] = overlay["SYSTEM"].replace("{format}", openai_format.INSTRUCTION)
        sections.update(overlay)
    elif tool_format == TOOL_FORMAT_NATIVE:
        sections.update(_read_sections(_agent_prompt_path("agent_native.txt")))
    for key, value in _FALLBACKS.items():
        sections.setdefault(key, value)
    sections["_FORMAT"] = tool_format if tool_format in TOOL_FORMATS else TOOL_FORMAT_TAGS
    return sections


def load_agent_instructions(tool_format: str = TOOL_FORMAT_TAGS) -> str:
    return load_prompt_sections(tool_format)["SYSTEM"]


def load_role_instructions(role_id: object, root: Path | None = None) -> str:
    """The ``ROLE_...`` section of prompts/agent.txt for a role; "" for Generic or a role without one.

    A name that is no role but a file-defined agent (``.crit/agents/<name>.md``, ``/agent``)
    gives that agent's instructions.
    """
    if roles.normalize_role(role_id) is None and str(role_id or "").strip():
        found = _file_agent(role_id, root)
        if found is not None:
            return found.body
    return load_prompt_sections().get(roles.section_key(role_id), "").strip()


def _file_agent(name: object, root: Path | None) -> Any:
    """The file-defined agent called ``name`` (extensions.agent_files), or None."""
    from critique_bot.extensions import agent_files

    try:
        return agent_files.find_agent(str(name), Path(root) if root is not None else Path.cwd())
    except Exception as exc:  # a broken agent file must not stop the session
        log.warn(f"agent {name}: {exc}")
        return None


def task_message(
    task: str, *, repo_map: str = "", skills: str = "", mode_note: str = "", prefix: str | None = None,
    tickets: str = "",
) -> str:
    """The task with the short protocol reminder, the repo map, skills, and the mode note."""
    body = task.strip()
    prefix = (prefix if prefix is not None else load_prompt_sections()["TASK_PREFIX"]).rstrip()
    if body.startswith(prefix.splitlines()[0]):
        return body
    parts = [prefix]
    if mode_note.strip():
        parts.append(mode_note.strip())
    if skills.strip():
        parts.append(skills.strip())
    if repo_map.strip():
        parts.append(
            "REPO MAP (files ranked for this task; numbers are definition lines; "
            "read before editing)\n" + repo_map.strip()
        )
    if tickets.strip():
        parts.append("JIRA (issues the task names, read by crit; read more with the jira tool)\n" + tickets.strip())
    parts.append("Task:\n" + body)
    return "\n\n".join(parts)


MAX_SKILL_CHARS = 14_000


def skills_block(items: list[dict[str, Any]], *, max_chars: int = MAX_SKILL_CHARS) -> str:
    """The SKILLS part of a task message: the body of each chosen skill, within ``max_chars``."""
    text_of = getattr(agent_tools, "skill_text", None)
    if not items or not callable(text_of):
        return ""
    parts: list[str] = []
    room = max_chars
    for item in items:
        body = text_of(item, max_chars=min(7_000, room))
        if not body:
            continue
        chunk = f"=== skill: {item['name']} ===\n{body}"
        if len(chunk) > room:
            break
        parts.append(chunk)
        room -= len(chunk)
    if not parts:
        return ""
    return (
        "SKILLS (expert guidance for this task's domain; apply it unless the task or PROJECT NOTES say otherwise)\n"
        + "\n\n".join(parts)
    )


def _web_hosts(settings: dict[str, Any]) -> tuple[str, ...]:
    """web_fetch's sites, or none. Off unless "web_fetch": true: even a read tells a site what is being looked at.

    With it on: the documentation defaults plus web_fetch_hosts. "offline": true keeps it off.
    """
    if settings.get("web_fetch") is not True or settings.get("offline") is True:
        return ()
    extra = [host.lower().strip().strip(".") for host in _setting_list(settings, "web_fetch_hosts")]
    return tuple(dict.fromkeys([*agent_tools.DEFAULT_WEB_HOSTS, *[host for host in extra if host]]))


def _setting_list(settings: dict[str, Any], key: str) -> list[str]:
    raw = settings.get(key)
    if isinstance(raw, str):
        raw = [raw]
    return [str(item).strip() for item in raw or [] if str(item).strip()] if isinstance(raw, list) else []


def _agent_prompt_path(name: str = "agent.txt") -> Path:
    # Only crit's own copy: a prompts/ folder in the project (or next to some Python) must
    # never replace crit's instructions, since the project is untrusted input.
    package_dir = Path(__file__).resolve().parent.parent  # critique_bot/, not engine/
    candidates = [package_dir / "prompts" / name]
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        candidates += [Path(frozen) / "critique_bot" / "prompts" / name, Path(frozen) / "prompts" / name]
    for path in candidates:
        if path.is_file():
            return path
    raise OSError(f"agent instructions not found (prompts/{name})")
