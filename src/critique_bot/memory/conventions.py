# SPDX-License-Identifier: Apache-2.0
"""M4: conventions. Notes the agent proposes and a person approves.

The agent may propose at most :data:`MAX_PER_TASK` notes per task (the ``memory`` tool's
``propose`` action). A note must be **specific**: it names a file, a symbol, a command or a
backticked value, and it carries evidence (the session id is added). Generic advice ("write
clean code", "make sure to test") is refused by :func:`generic_reason` at write time, which
answers the AGENTS.md study's main failure mode.

Nothing proposed is recalled until approved (``crit memory review``). :func:`stale_check`
flags an approved note whose evidence no longer holds: a file it names is gone, a symbol it
names is no longer defined, or its command's program cannot be found.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Callable

from critique_bot.memory.store import Convention, MemoryStore

MAX_PER_TASK = 2
MIN_CHARS = 20
MAX_CHARS = 400
#: Files a stale check looks through for a bare file name before it gives up.
WALK_LIMIT = 200_000

_GENERIC = re.compile(
    r"(?i)\b(clean code|best practices?|be careful|make sure|always (?:test|write|check|document)|write (?:good |more )?tests|"
    r"follow (?:the )?(?:conventions|style|guidelines)|good naming|readab(?:le|ility)|maintainab(?:le|ility)|"
    r"document (?:your|the) code|handle errors?(?: properly| gracefully)?|keep (?:it|things|code) simple|"
    r"meaningful names|consistent style|double[- ]check|think (?:carefully|step)|avoid bugs|code quality|"
    r"test thoroughly|comment your code|use descriptive|small functions)\b"
)
_PATH = re.compile(r"(?<![\w])(?:[\w.-]+/)+[\w.-]+|\b[\w-]+\.(?:py|java|kt|kts|go|rs|c|cc|cpp|h|hpp|ts|tsx|js|jsx|xml|bp|gradle|"
                   r"aidl|hal|rc|te|toml|json|yaml|yml|cfg|ini|mk|cmake|sh|md)\b")
_TICKS = re.compile(r"`([^`\n]{2,120})`")
_IDENT = re.compile(r"\b(?:[A-Za-z_]\w*\.[A-Za-z_]\w+|[a-z]+[A-Z]\w*|[A-Z][a-z0-9]+[A-Z]\w*|[a-z]+_[a-z0-9_]+|[A-Z_]{4,})\b")
_COMMAND = re.compile(r"(?i)\b(atest|m|mm|mma|make|gradlew?|pytest|python3? -m|npm|yarn|cargo|go (?:test|build|run)|ctest|cmake|"
                      r"adb|repo|bazel|mvn|tox|lunch|soong_ui|ninja|crit)\b")


def anchors(text: str) -> dict[str, list[str]]:
    """The specific things a note names: paths, backticked values, identifiers, commands."""
    return {
        "paths": list(dict.fromkeys(_PATH.findall(text))),
        "ticks": list(dict.fromkeys(_TICKS.findall(text))),
        "identifiers": list(dict.fromkeys(_IDENT.findall(text))),
        "commands": list(dict.fromkeys(m.group(0) for m in _COMMAND.finditer(text))),
    }


def generic_reason(text: str) -> str:
    """Why ``text`` is not a usable convention ("" when it is specific enough)."""
    stripped = re.sub(r"\s+", " ", text or "").strip()
    if len(stripped) < MIN_CHARS:
        return f"too short: a convention says what, where and how in at least {MIN_CHARS} characters"
    if len(stripped) > MAX_CHARS:
        return f"too long: keep a convention under {MAX_CHARS} characters, one fact"
    found = anchors(stripped)
    specific = found["paths"] or found["ticks"] or found["identifiers"] or found["commands"]
    if not specific:
        return ("generic: name the file, symbol, command or value it is about (in backticks), "
                "for example `atest CarServiceUnitTest`, not general advice")
    if _GENERIC.search(stripped) and len(stripped) < 120 and not (found["ticks"] or found["paths"]):
        return "generic advice: state the project-specific fact instead"
    return ""


def propose(store: MemoryStore, text: str, evidence: list[str], *, session: str, proposed_so_far: int) -> tuple[Convention | None, str]:
    """Queue a proposal for review. ``(convention, "")`` or ``(None, why it was refused)``."""
    if proposed_so_far >= MAX_PER_TASK:
        return None, f"at most {MAX_PER_TASK} proposals per task; this one was not saved"
    why = generic_reason(text)
    if why:
        return None, why
    evidence = [str(item).strip()[:300] for item in evidence if str(item).strip()][:5]
    if not evidence:
        return None, "evidence is required: the command output, file:line or test that shows it holds"
    normalized = re.sub(r"\W+", " ", text.lower()).strip()
    for existing in store.conventions():
        if existing.status in {"proposed", "approved"} and re.sub(r"\W+", " ", existing.text.lower()).strip() == normalized:
            return None, f"already {existing.status} as {existing.ref}"
    if session:
        evidence.append(f"session {session}")
    provenance = f"session:{session}" if session else "user:memory propose"
    return store.add_convention(text.strip(), evidence, session=session, provenance=provenance), ""


def _file_exists(workspace: Path, raw: str) -> bool:
    rel = raw.split(":", 1)[0]
    while rel.startswith("./"):
        rel = rel[2:]
    if (workspace / rel).exists():
        return True
    if "/" in rel:
        return False
    # a bare file name holds when some file of that name exists in the project (bounded walk;
    # when the walk is cut short the note is given the benefit of the doubt)
    seen = 0
    for folder, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in {".git", ".crit", "out", "node_modules", "build"}]
        if rel in files:
            return True
        seen += len(files)
        if seen > WALK_LIMIT:
            return True
    return False


def stale_reason(convention: Convention, workspace: Path, *, defined: Callable[[str], bool] | None = None) -> str:
    """Why an approved note no longer holds ("" while it does)."""
    workspace = Path(workspace)
    found = anchors(convention.text)
    for raw in found["paths"]:
        if not raw.startswith("/") and not _file_exists(workspace, raw):
            return f"{raw} no longer exists"
    for tick in found["ticks"]:
        words = tick.split()
        if not words:
            continue
        program = words[0]
        if _COMMAND.match(tick):
            if program in {"python", "python3"} or shutil.which(program) or (workspace / program).exists():
                continue
            if program in {"m", "mm", "mma", "lunch"} and (workspace / "build" / "envsetup.sh").exists():
                continue
            return f"the command {program!r} is not available here"
        symbol_like = len(words) == 1 and re.fullmatch(r"[A-Za-z_][\w.]*", tick) and not _PATH.fullmatch(tick)
        if defined is not None and symbol_like and (any(ch.isupper() for ch in tick) or "_" in tick or "." in tick):
            if not defined(tick):
                return f"{tick} is no longer defined in the index"
    return ""


def stale_check(store: MemoryStore, workspace: Path, *, defined: Callable[[str], bool] | None = None) -> list[tuple[Convention, str]]:
    """Flag approved notes whose evidence no longer holds (status ``stale``). Returns them with the reason."""
    flagged = []
    for convention in store.conventions("approved"):
        why = stale_reason(convention, workspace, defined=defined)
        if why:
            store.set_convention_status(convention.id, "stale", why)
            flagged.append((convention, why))
    return flagged
