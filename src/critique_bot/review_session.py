"""Multi-turn review on one ChatSession: one-shot when it fits, else file ACKs.

The worker and local CLI share this loop. History lives in the web UI tab;
each ``send()`` is a paste-and-wait turn, not a websocket we own.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from critique_bot import log
from critique_bot.config import compose_prompt, format_attachments
from critique_bot.patch import (
    InputLimits,
    SanitizeStats,
    cap_text,
    finalize_prompt,
    format_sanitize_note,
    sanitize_one,
)

FILES_ALREADY_SENT = (
    "Changed-file bodies were already sent earlier in this conversation "
    "(one file per turn). Use those HEAD line numbers for comments[].line "
    'on side: "new".'
)
REVIEW_NOW = "REVIEW NOW"


@dataclass(frozen=True)
class PromptPayload:
    prompt: str
    files: dict[str, str] = field(default_factory=dict)


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


def split_review_payload(
    template: str,
    patch_body: str,
    mr_context: str,
    file_attachments: list[tuple[str, str]],
    limits: InputLimits,
    stats: SanitizeStats,
) -> PromptPayload:
    """Return prompt + files. ``files`` is empty when one-shot is enough."""
    files_body = (
        format_attachments(file_attachments, named=True) if file_attachments else ""
    )
    one_shot = compose_prompt(template, patch_body, mr_context, files=files_body)
    if not file_attachments or one_shot_fits(one_shot, limits, stats):
        return PromptPayload(prompt=finalize_prompt(one_shot, limits, stats))
    staged = compose_prompt(
        template, patch_body, mr_context, files=FILES_ALREADY_SENT
    )
    prompt = finalize_prompt(staged, limits, stats)
    files = {name: body for name, body in file_attachments}
    log.info(
        f"review overflow ({len(one_shot)} chars); "
        f"staging {len(files)} file(s) across chat turns"
    )
    return PromptPayload(prompt=prompt, files=files)


def format_file_index(files: dict[str, str]) -> str:
    return "\n".join(
        f"- {path} ({len(body)} chars)" for path, body in files.items()
    )


def format_prime_turn(files: dict[str, str]) -> str:
    n = len(files)
    index = format_file_index(files)
    return (
        f"You are AAOS-Review. I will send {n} changed file(s) one at a time, "
        "then a patch and the review instructions.\n"
        "\n"
        "For each file, reply with exactly: ACK <path>\n"
        "Do not review, summarize, list issues, or output JSON until I say "
        f"{REVIEW_NOW}.\n"
        "\n"
        "Files to follow:\n"
        f"{index}\n"
    )


def format_file_turn(index: int, total: int, path: str, body: str) -> str:
    named = format_attachments([(path, body)], named=True)
    return (
        f"FILE {index} of {total}. Reply with exactly: ACK {path}\n"
        "Do not review yet.\n"
        "\n"
        f"{named}"
    )


def reply_is_ack(reply: str, path: str) -> bool:
    text = (reply or "").strip()
    if not text:
        return False
    first = text.splitlines()[0].strip()
    if not first.upper().startswith("ACK"):
        return False
    return path in first or path in text


def run_review_session(
    session,
    prompt: str,
    files: dict[str, str] | None,
    limits: InputLimits,
    *,
    turn_pause_seconds: float = 0.0,
    sleep=time.sleep,
) -> str:
    """Send one review on an open ChatSession. Last assistant reply is the review."""
    file_map = dict(files or {})
    if not file_map:
        return session.send(prompt)

    def pause() -> None:
        if turn_pause_seconds > 0:
            sleep(turn_pause_seconds)

    paths = list(file_map)
    log.info(f"review session: {len(paths)} file turn(s) then the review prompt")
    prime = cap_text(
        format_prime_turn(file_map), limits.max_prompt_chars, what="prime turn"
    )
    session.send(prime)
    total = len(paths)
    for i, path in enumerate(paths, start=1):
        pause()
        payload = cap_text(
            format_file_turn(i, total, path, file_map[path]),
            limits.max_prompt_chars,
            what=path,
        )
        reply = session.send(payload)
        if not reply_is_ack(reply, path):
            log.warn(f"file turn {i}/{total} ({path}) did not ACK; continuing")
    pause()
    final = prompt.rstrip()
    if REVIEW_NOW not in final:
        final = final + "\n\n" + REVIEW_NOW + "\n"
    return session.send(final)
