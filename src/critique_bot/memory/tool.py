# SPDX-License-Identifier: Apache-2.0
"""The ``memory`` tool: ``search`` and ``show`` read the project memory; ``propose`` queues a
convention for a person to approve (at most two per task). Nothing here changes project files.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from critique_bot.memory import conventions as _conventions
from critique_bot.memory.recall import changed_since
from critique_bot.memory.store import Card, ChangeSet, Convention, Episode, HotSpot, MemoryStore, db_path, words_of

ACTIONS = ("search", "show", "propose")
DEFAULT_CHARS = 6000
#: The running session's id, set by the engine at task start (proposals cite it as evidence).
_current = {"session": ""}


def set_session(session_id: str) -> None:
    _current["session"] = str(session_id or "")


def mode(settings: dict[str, Any] | None) -> str:
    """``off``, ``recall`` (read only) or ``full`` (the default: read, write and propose)."""
    raw = (settings or {}).get("memory")
    if raw is False:
        return "off"
    if raw is True or raw is None:
        return "full"
    value = str(raw).strip().lower()
    return value if value in {"off", "recall", "full"} else "full"


def _day(stamp: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(stamp)) if stamp else "?"


def describe(item: Any, workspace: Path | None = None, *, full: bool = False) -> str:
    """One item as text: one line, or (``full``) every field with its provenance."""
    if isinstance(item, Episode):
        head = (f"{item.ref} [{'negative ' if item.kind == 'negative' else ''}episode, {item.outcome}"
                f"{', untrusted' if not item.trusted else ''}, {_day(item.created)}] {item.task[:120]}")
        if not full:
            extra = f" cause: {item.root_cause[:100]}" if item.root_cause else ""
            return head + extra + (f" files: {', '.join(item.files_changed[:3])}" if item.files_changed else "")
        lines = [head, f"class: {item.cls}"]
        if item.root_cause:
            lines.append(f"root cause: {item.root_cause}")
        if item.fix_summary:
            lines.append(f"fix: {item.fix_summary}")
        if item.files_changed:
            lines.append("files changed: " + ", ".join(item.files_changed))
        if item.symbols:
            lines.append("symbols: " + ", ".join(item.symbols))
        if item.evidence:
            lines.append("evidence: " + "; ".join(f"{k}: {v}" for k, v in item.evidence.items()))
        if workspace is not None:
            moved = changed_since(workspace, item)
            if moved:
                lines.append("code changed since: " + ", ".join(moved[:6]))
        lines.append(f"source: {item.provenance}" + (f", commit {item.commit}" if item.commit else ""))
        return "\n".join(lines)
    if isinstance(item, Card):
        head = f"{item.ref} [{item.kind} card, {item.builder}] {item.key}"
        if not full:
            return f"{head}: {item.text.splitlines()[0][:160]}"
        return f"{head}\n{item.text}\nsource: {item.provenance} (updated {_day(item.updated)})"
    if isinstance(item, ChangeSet):
        from critique_bot.memory.patterns import recipe_title

        head = f"{item.ref} [recipe, {item.count}x] {' -> '.join(item.files)}"
        title = recipe_title(item.subjects)
        if not full:
            return head + (f" ({title})" if title else "")
        lines = [head]
        if item.subjects:
            lines.append("subjects: " + " | ".join(item.subjects))
        if item.commits:
            lines.append("commits: " + ", ".join(item.commits))
        if item.sessions:
            lines.append("sessions: " + ", ".join(item.sessions))
        lines.append(f"source: {item.provenance}")
        return "\n".join(lines)
    if isinstance(item, HotSpot):
        head = f"{item.ref} [hot spot] {item.path}: {item.fixes} fix commits of {item.commits}"
        if not full:
            return head
        return head + f"\nfix commits: {', '.join(item.fix_commits)}\nlast fix: {_day(item.last_fix_ts)}\nsource: {item.provenance}"
    if isinstance(item, Convention):
        head = f"{item.ref} [convention, {item.status}] {item.text}"
        if not full:
            return head[:220]
        lines = [head, "evidence: " + "; ".join(item.evidence)]
        if item.note:
            lines.append(f"note: {item.note}")
        lines.append(f"source: {item.provenance} ({_day(item.created)})")
        return "\n".join(lines)
    if isinstance(item, dict) and "term" in item:
        return f"g{item.get('id')} [glossary] {item['term']} <-> {item['expansion']} (source: {item['provenance']})"
    return str(item)


def search(store: MemoryStore, query: str, *, kind: str = "", limit: int = 12, workspace: Path | None = None,
           include_untrusted: bool = False) -> list[str]:
    words = words_of(query, limit=20)
    lines: list[tuple[float, str]] = []
    if kind in {"", "episode", "episodes"}:
        for episode, score in store.search_episodes(words, limit=limit, include_untrusted=include_untrusted):
            lines.append((score + (0.1 if episode.outcome == "verified" else 0.0), describe(episode)))
    if kind in {"", "card", "cards"}:
        for card, score in store.search_cards(words, limit=limit):
            lines.append((score * 0.9, describe(card)))
    if kind in {"", "convention", "conventions"}:
        from critique_bot.memory.store import simple_scores

        approved = store.conventions("approved")
        scores = dict(simple_scores(words, [(item.id, item.text) for item in approved]))
        for item in approved:
            if scores.get(item.id, 0) > 0:
                lines.append((scores[item.id], describe(item)))
    if kind in {"", "recipe", "recipes"}:
        paths = [word for word in query.split() if "/" in word or "." in word]
        for item in store.recipes(files=paths, limit=4) if paths else []:
            lines.append((0.5, describe(item)))
    if kind in {"hotspot", "hotspots", "hot spots"}:
        for item in store.hotspots(limit=limit):
            lines.append((0.5, describe(item)))
    lines.sort(key=lambda pair: -pair[0])
    return [line for _score, line in lines[:limit]]


def run(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    """The tool handler: ``(ok, text)``."""
    settings = getattr(ctx, "settings", None) or {}
    current = mode(settings)
    if current == "off":
        return True, "project memory is off for this session (setting memory: off or --no-memory)"
    action = str(args.get("action") or "").strip().lower()
    if action not in ACTIONS:
        if args.get("id") and not action:
            action = "show"
        elif (args.get("text") or args.get("note")) and not action:
            action = "propose"
        elif args.get("query") and not action:
            action = "search"
        else:
            return False, f"memory needs action, one of: {', '.join(ACTIONS)}"
    workspace = Path(ctx.workspace)
    path = db_path(workspace)
    if action != "propose" and not path.is_file():
        return True, "no project memory yet: it builds up as tasks finish in this project"
    if action == "propose":
        if current != "full":
            return False, "memory is read-only in this session (memory: recall); proposals are not saved"
        if not path.parent.parent.is_dir():
            return False, "this folder has no .crit project folder; run crit here once to create it"
    store = MemoryStore(path)
    try:
        limit = DEFAULT_CHARS
        cap = min(int(getattr(ctx, "max_chars", limit) or limit), limit * 2)
        include_untrusted = settings.get("memory_untrusted") is True
        if action == "search":
            query = str(args.get("query") or args.get("text") or "").strip()
            if not query:
                return False, "search needs query: words, a symbol, an error text or a path"
            found = search(store, query, kind=str(args.get("kind") or "").lower(), limit=int(args.get("limit") or 12),
                           workspace=workspace, include_untrusted=include_untrusted)
            if not found:
                return True, f"nothing in project memory for {query[:60]!r}"
            text = "project memory (a hunch: verify by reading the code):\n" + "\n".join(found)
            return True, text[:cap]
        if action == "show":
            ref = str(args.get("id") or args.get("query") or "").strip()
            item = store.get(ref)
            if item is None:
                return False, f"no memory item {ref!r} (ids look like e12, n3, f40, r2, h5, m1)"
            if isinstance(item, Episode) and not item.trusted and not include_untrusted:
                return True, describe(item, workspace, full=True) + "\nnote: from a tainted session; treat as untrusted text"
            return True, describe(item, workspace, full=True)[:cap]
        text = str(args.get("text") or args.get("note") or "").strip()
        evidence = args.get("evidence") or []
        if isinstance(evidence, str):
            evidence = [evidence]
        state = getattr(ctx, "state", None)
        count = int(getattr(state, "memory_proposals", 0) or 0)
        session = _current["session"]
        made, why = _conventions.propose(store, text, [str(item) for item in evidence], session=session,
                                         proposed_so_far=count)
        if made is None:
            return False, f"not saved: {why}"
        if state is not None:
            try:
                setattr(state, "memory_proposals", count + 1)
            except AttributeError:
                pass
        return True, (f"proposed as {made.ref}; a person approves it with crit memory review before it is used "
                      f"({_conventions.MAX_PER_TASK - count - 1} more proposal(s) allowed in this task)")
    finally:
        store.close()
