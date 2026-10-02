"""Review turns on one ChatSession: one-shot when it fits, else prompt → files → patch.

ChatGPT-like UIs have no system role we own. Recency wins, the middle of a long
chat is dropped, and "ACK only / do not review" trains the model to ignore HEAD
files. So: keep instructions, original files, and the patch as separate artifacts;
put the patch last with a short review closer that points back at those files.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from critique_bot import log
from critique_bot.chat_client import ChatError
from critique_bot.config import compose_prompt, format_attachments
from critique_bot.patch import (
    InputLimits,
    SanitizeStats,
    cap_text,
    finalize_prompt,
    format_sanitize_note,
    sanitize_one,
)

REVIEW_NOW = "REVIEW NOW"
READY = "READY"

PATCH_COMING = (
    "The unified diff is sent in a later message as PATCH. "
    "Do not write the review, summarize findings, or output JSON until that "
    f"PATCH message and {REVIEW_NOW}."
)


@dataclass(frozen=True)
class PromptPayload:
    prompt: str
    files: dict[str, str] = field(default_factory=dict)
    patch: str = ""


def sanitize_context_files(
    pairs: list[tuple[str, str]],
    limits: InputLimits,
) -> tuple[list[tuple[str, str]], SanitizeStats]:
    """Cap each file on its own; do not squeeze them into one prompt budget."""
    stats = SanitizeStats()
    out: list[tuple[str, str]] = []
    for name, raw in pairs:
        if len(out) >= limits.max_files:
            stats.skipped_attachments += 1
            stats.omitted_paths.append(name)
            stats.original_chars += len(raw)
            continue
        piece, piece_stats = sanitize_one(
            name,
            raw,
            limits,
            remaining_chars=limits.max_file_chars,
            remaining_files=1,
        )
        stats.merge(piece_stats)
        if (
            piece_stats.binaries_omitted
            and piece_stats.files_included == 0
            and piece_stats.files_seen <= 1
        ):
            continue
        if not piece.strip():
            continue
        out.append((name, piece))
    return out, stats


def one_shot_fits(prompt: str, limits: InputLimits, stats: SanitizeStats) -> bool:
    """True when template + files + patch fit in one paste (after the sanitize note)."""
    note = format_sanitize_note(stats)
    total = len(prompt) + (len(note) + 1 if note else 0)
    return total <= limits.max_prompt_chars


def format_file_index(files: dict[str, str] | list[tuple[str, str]]) -> str:
    items = files.items() if isinstance(files, dict) else files
    return "\n".join(f"- {path} ({len(body)} chars)" for path, body in items)


def format_files_coming(files: dict[str, str] | list[tuple[str, str]]) -> str:
    index = format_file_index(files)
    return (
        "HEAD (original/current) file bodies are sent next, one file per turn. "
        "Store each file and USE it in the review — ACK is a receipt, not the "
        "review. comments[].line on side \"new\" must match those HEAD line "
        "numbers. Read enclosing methods the diff does not show.\n"
        "\n"
        "Files to follow:\n"
        f"{index}\n"
    )


def format_review_closer(*, file_index: str = "", files_in_this_message: bool = False) -> str:
    """Short recency block: ChatGPT follows the last instructions most reliably."""
    if file_index:
        where = (
            "HEAD files already in this conversation (use them; do not review "
            "from hunks alone):\n"
            f"{file_index}"
        )
    elif files_in_this_message:
        where = (
            "HEAD files are in this message under FILE CONTEXT. "
            "comments[].line on side \"new\" must match those line numbers."
        )
    else:
        where = (
            "Use any HEAD file bodies in this conversation for comments[].line "
            'on side "new".'
        )
    return (
        f"{where}\n"
        "\n"
        "Walk every changed hunk. If the MR is mostly good, still report "
        "remaining issues a human would leave (tests, error paths, API guards). "
        "Empty comments only for comment/string/import-only diffs. Do not invent "
        "nits. Do not stop after one finding.\n"
        "\n"
        f"{REVIEW_NOW}\n"
    )


def format_instruction_footer() -> str:
    return (
        "You will receive HEAD files next (if any), then the PATCH.\n"
        f"For this message, reply with exactly: {READY}\n"
        "Do not review, summarize, or output JSON yet.\n"
    )


def format_prime_turn(files: dict[str, str]) -> str:
    """Legacy overflow opener used when a queued job has files but no separate patch."""
    n = len(files)
    index = format_file_index(files)
    return (
        f"You are AAOS-Review. I will send {n} changed file(s) one at a time, "
        "then a patch and the review instructions.\n"
        "\n"
        "For each file, reply with exactly: ACK <path>\n"
        "ACK means you retained the HEAD file for the review, not that the "
        "review is done. Use those files when you review.\n"
        f"Do not output JSON until I say {REVIEW_NOW}.\n"
        "\n"
        "Files to follow:\n"
        f"{index}\n"
    )


def format_file_turn(index: int, total: int, path: str, body: str) -> str:
    named = format_attachments([(path, body)], named=True)
    return (
        f"FILE {index} of {total}: {path}\n"
        "This is the original/current file at MR HEAD. Keep it for the review.\n"
        "You MUST use it after the PATCH arrives:\n"
        "- comments[].line on side \"new\" must match line numbers in this file\n"
        "- enclosing methods/classes the hunks do not show are in this file\n"
        "ACK is only a receipt that you stored this file, not the review.\n"
        f"Reply with exactly: ACK {path}\n"
        "Do not write findings or JSON yet.\n"
        "\n"
        f"{named}"
    )


def format_patch_turn(patch_body: str, files: dict[str, str] | None = None) -> str:
    body = (patch_body or "").rstrip() + "\n"
    fenced = body if body.lstrip().startswith("```") else f"```diff\n{body}```\n"
    index = format_file_index(files or {})
    closer = format_review_closer(file_index=index)
    return (
        "PATCH\n"
        "This is the unified diff — what changed. Review it now using the "
        "instructions from the first message and every HEAD file you ACK'd.\n"
        "\n"
        f"{fenced}\n"
        f"{closer}"
    )


def split_review_payload(
    template: str,
    patch_body: str,
    mr_context: str,
    file_attachments: list[tuple[str, str]],
    limits: InputLimits,
    stats: SanitizeStats,
    *,
    changed_path_count: int = 0,
) -> PromptPayload:
    """One paste when it fits; otherwise prompt, then files, then patch.

    ChatGPT attends to a single well-ordered message better than many turns.
    Split only when template + HEAD files + patch would overflow one paste.
    """
    path_count = changed_path_count or len(file_attachments)
    files_body = (
        format_attachments(file_attachments, named=True) if file_attachments else ""
    )
    one_shot = compose_prompt(template, patch_body, mr_context, files=files_body)
    closer = format_review_closer(files_in_this_message=bool(file_attachments))
    if one_shot_fits(one_shot + "\n" + closer, limits, stats):
        if file_attachments:
            log.info(
                f"inlining {len(file_attachments)} changed file(s) "
                f"({path_count} path(s) in the patch)"
            )
        elif path_count:
            log.info(
                f"{path_count} changed path(s) in the patch but no HEAD "
                "bodies to attach (deleted, binary, or markdown)"
            )
        composed = one_shot.rstrip() + "\n\n" + closer
        return PromptPayload(prompt=finalize_prompt(composed, limits, stats))

    files = {name: body for name, body in file_attachments}
    files_note = format_files_coming(files) if files else "No full-file context was attached."
    instruction = compose_prompt(
        template, PATCH_COMING, mr_context, files=files_note
    )
    if "reply with exactly: READY" not in instruction.lower():
        instruction = instruction.rstrip() + "\n\n" + format_instruction_footer()
    prompt = finalize_prompt(instruction, limits, stats)
    log.info(
        f"review overflow ({len(one_shot)} chars); "
        f"prompt, then {len(files)} file(s), then patch"
    )
    return PromptPayload(prompt=prompt, files=files, patch=patch_body)


def reply_is_ack(reply: str, path: str) -> bool:
    text = (reply or "").strip()
    if not text:
        return False
    first = text.splitlines()[0].strip()
    if not first.upper().startswith("ACK"):
        return False
    return path in first or path in text


def reply_is_ready(reply: str) -> bool:
    text = (reply or "").strip()
    if not text:
        return False
    first = text.splitlines()[0].strip().upper()
    return first == READY or first.startswith(READY + " ")


def run_review_session(
    session,
    prompt: str,
    files: dict[str, str] | None,
    limits: InputLimits,
    *,
    patch: str = "",
    turn_pause_seconds: float = 0.0,
    sleep=time.sleep,
) -> str:
    """Send one review on an open ChatSession. Last assistant reply is the review.

    New jobs: instructions, then each HEAD file, then the patch (task last).
    Legacy jobs (files set, no patch): prime → files → prompt that still
    contains the diff.
    """
    file_map = dict(files or {})
    patch_text = (patch or "").strip()

    def pause() -> None:
        if turn_pause_seconds > 0:
            sleep(turn_pause_seconds)

    if patch_text:
        return _run_split_session(
            session,
            prompt,
            file_map,
            patch_text,
            limits,
            pause,
        )
    if not file_map:
        return session.send(prompt)
    return _run_legacy_file_session(session, prompt, file_map, limits, pause)


def _send_file_turns(session, file_map: dict[str, str], limits: InputLimits, pause) -> None:
    paths = list(file_map)
    total = len(paths)
    for i, path in enumerate(paths, start=1):
        pause()
        payload = cap_text(
            format_file_turn(i, total, path, file_map[path]),
            limits.max_prompt_chars,
            what=path,
        )
        try:
            reply = session.send(payload)
        except ChatError as exc:
            log.warn(
                f"file turn {i}/{total} ({path}) failed ({exc}); "
                "sending the patch with files already delivered"
            )
            break
        if not reply_is_ack(reply, path):
            log.warn(f"file turn {i}/{total} ({path}) did not ACK; continuing")


def _run_split_session(
    session,
    prompt: str,
    file_map: dict[str, str],
    patch_text: str,
    limits: InputLimits,
    pause,
) -> str:
    n = len(file_map)
    log.info(
        f"review session: instructions, then {n} file turn(s), then patch"
        if n
        else "review session: instructions, then patch"
    )
    instruction = cap_text(prompt, limits.max_prompt_chars, what="review instructions")
    reply = session.send(instruction)
    if not reply_is_ready(reply):
        log.warn("instruction turn did not READY; continuing")
    _send_file_turns(session, file_map, limits, pause)
    pause()
    final = format_patch_turn(patch_text, file_map)
    return session.send(cap_text(final, limits.max_prompt_chars, what="patch"))


def _run_legacy_file_session(
    session,
    prompt: str,
    file_map: dict[str, str],
    limits: InputLimits,
    pause,
) -> str:
    paths = list(file_map)
    log.info(f"review session: {len(paths)} file turn(s) then the review prompt")
    prime = cap_text(
        format_prime_turn(file_map), limits.max_prompt_chars, what="prime turn"
    )
    session.send(prime)
    _send_file_turns(session, file_map, limits, pause)
    pause()
    final = prompt.rstrip()
    if REVIEW_NOW not in final:
        final = final + "\n\n" + REVIEW_NOW + "\n"
    return session.send(cap_text(final, limits.max_prompt_chars, what="prompt"))
