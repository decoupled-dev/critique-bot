# SPDX-License-Identifier: Apache-2.0
"""Project memory in the engine: the hunch at task start, the episode at task end (Stage 8b).

Two hooks, both called from the task loop and both safe (a failure is logged, never raised):

- :func:`begin_task` -> the **hunch block** for the task's first message: memory candidates
  (:mod:`critique_bot.memory.recall`) ranked by similarity, recency, outcome, co-change strength
  and a penalty when the code changed since, cut to a token budget (600 by default) and labelled
  as a hunch with confidences and sources. It goes in the task's user message, never the cached
  prefix. A ``memory_recall`` session event records what was shown.
- :func:`end_task` writes what the task taught: the episode (from the event log), the change
  set of the session, file cards of the files read and edited (and module cards plus the
  module's top repo-map files), newly mined history patterns, and glossary entries.

Settings: ``memory``: ``off`` | ``recall`` (read only) | ``full`` (default); ``--no-memory``
sets ``off``. ``memory_budget`` (tokens), ``memory_untrusted`` (recall episodes from tainted
sessions too), ``memory_cards``: ``extractive`` (default) or ``model`` (one helper request per card).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from critique_bot import log, memory, taint

DEFAULT_BUDGET = 600
LABEL = "Project memory (hunch; verify before relying on it):"
FOOTER = "Open a source with the memory tool: {\"action\": \"show\", \"id\": \"e12\"}."
#: Positive outcomes weigh by how sure we are the change was right.
OUTCOME_WEIGHT = {"verified": 1.0, "accepted": 0.85, "completed": 0.6}
#: Episode recency half-life in days.
HALF_LIFE_DAYS = 180.0
#: Wrong-hunch rate above which likely files are withheld (plan 8b, Risks).
WRONG_HUNCH_LIMIT = 0.5
WRONG_HUNCH_WINDOW = 20
WRONG_HUNCH_MIN = 6
#: Cards written per task end, and the seconds that work may take.
MAX_CARDS_PER_TASK = 20
CARD_SECONDS = 3.0
MINE_COMMITS_PER_TASK = 2000

#: The last hunch's ranked files, for the wrong-hunch count at task end.
_last: dict[str, Any] = {"files": [], "task": ""}


def mode(options: dict[str, Any] | None) -> str:
    return memory.mode(options)


def _project_dir_exists(workspace: Path) -> bool:
    return memory.memory_dir(workspace).parent.is_dir()


def tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


# --------------------------------------------------------------------------- ranking


@dataclass
class LikelyFile:
    path: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


@dataclass
class Hunch:
    files: list[LikelyFile]
    lines: list[str]
    sources: list[str]
    text: str = ""
    withheld: bool = False


def _recency(created: int, now: float) -> float:
    age = max(0.0, (now - created) / 86400.0)
    return 0.5 + 0.5 * math.exp(-age / HALF_LIFE_DAYS)


def rank(found: Any, workspace: Path, *, now: float | None = None) -> list[LikelyFile]:
    """Likely files from the candidates, best first. Confidence combines independent signals
    as 1 - prod(1 - s), capped at 0.95; each signal is already weighted by its kind."""
    stamp = time.time() if now is None else now
    parts: dict[str, list[tuple[float, str, str]]] = {}

    def add(path: str, value: float, reason: str, source: str = "") -> None:
        if value > 0:
            parts.setdefault(path, []).append((min(value, 0.9), reason, source))

    for hit in found.episodes:
        episode = hit.episode
        if episode.kind != "positive":
            continue
        weight = hit.score * OUTCOME_WEIGHT.get(episode.outcome, 0.5) * _recency(episode.created, stamp)
        for rel in episode.files_changed[:8]:
            penalty = 0.7 if rel in hit.changed_since else 1.0
            add(rel, 0.75 * weight * penalty, f"past {episode.cls or 'change'} {episode.ref}", episode.ref)
    for path, score in found.seed_scores.items():
        add(path, score, "; ".join(found.seeds.get(path, [])))
    for path, (score, partner) in found.cochange.items():
        add(path, 0.5 * score, f"usually changes with {Path(partner).name}")
    for recipe in found.recipes:
        strength = min(1.0, recipe.count / 5.0) * 0.35
        for rel in recipe.files:
            add(rel, strength, f"recipe {recipe.ref} ({recipe.count}x)", recipe.ref)
    from critique_bot.memory.patterns import hotness

    for spot in found.hotspots:
        if spot.path in parts:
            add(spot.path, 0.15 * hotness(spot.fixes, spot.commits), f"hot spot ({spot.fixes} fix commits)", spot.ref)
    out = []
    for path, items in parts.items():
        if not (Path(workspace) / path).exists():
            continue
        keep = 1.0
        for value, _reason, _source in items:
            keep *= 1.0 - value
        confidence = round(min(0.95, 1.0 - keep), 2)
        items.sort(key=lambda item: -item[0])
        reasons = list(dict.fromkeys(reason for _v, reason, _s in items if reason))
        sources = list(dict.fromkeys(source for _v, _r, source in items if source))
        out.append(LikelyFile(path, confidence, reasons, sources))
    out.sort(key=lambda item: (-item.confidence, item.path))
    return [item for item in out if item.confidence >= 0.2]


def _short(path: str, others: list[str]) -> str:
    name = Path(path).name
    return name if sum(1 for other in others if Path(other).name == name) == 1 else path


def build_hunch(found: Any, workspace: Path, *, budget: int = DEFAULT_BUDGET, withhold_files: bool = False,
                now: float | None = None) -> Hunch:
    """The hunch block within ``budget`` tokens (label and footer included); "" when there is nothing."""
    files = rank(found, workspace, now=now)[:5]
    sections: list[tuple[list[str], list[str]]] = []  # (lines, sources)
    if files and not withhold_files:
        names = [item.path for item in files]
        line = "- Likely files: " + ", ".join(f"{item.path} ({item.confidence:.1f})" for item in files)
        grouped: dict[str, list[str]] = {}
        for item in files[:4]:
            if item.reasons:
                grouped.setdefault(item.reasons[0], []).append(_short(item.path, names))
        why = [f"{', '.join(paths)}: {reason}" for reason, paths in grouped.items()]
        lines = [line] + (["  why: " + "; ".join(why)] if why else [])
        sections.append((lines, [src for item in files for src in item.sources]))
    elif files and withhold_files:
        sections.append((["- (likely files withheld: recent hunches in this project were often wrong)"], []))
    shown = 0
    for hit in found.episodes:
        episode = hit.episode
        if episode.kind != "positive" or shown >= 2 or hit.score < 0.3:
            continue
        what = []
        if episode.root_cause:
            what.append(f"cause: {episode.root_cause[:160]}")
        if episode.fix_summary:
            what.append(f"fix: {episode.fix_summary[:140]}")
        if not what:
            what.append(f"task: {episode.task[:120]}; changed {', '.join(Path(f).name for f in episode.files_changed[:3])}")
        changed = ", code changed since" if hit.changed_since else ""
        sections.append(([f"- Similar past {episode.cls or 'task'} ({episode.ref}, {episode.outcome}{changed}): " + "; ".join(what)],
                         [episode.ref]))
        shown += 1
    negatives = [hit for hit in found.episodes if hit.episode.kind == "negative" and hit.score >= 0.25][:1]
    for hit in negatives:
        episode = hit.episode
        sections.append(([f"- Tried before without success ({episode.ref}, {episode.outcome}): {episode.fix_summary[:160] or episode.task[:120]}"],
                         [episode.ref]))
    for recipe in found.recipes[:1]:
        if recipe.count >= 2:
            sections.append(([f"- Change recipe ({recipe.ref}, {recipe.count}x): " + " -> ".join(recipe.files[:6])], [recipe.ref]))
    for card in found.modules[:1]:
        body = card.text.splitlines()
        sections.append(([f"- {body[0]} ({card.ref})"] + [f"  {line}" for line in body[1:4]], [card.ref]))
    for convention, _score in found.conventions[:2]:
        sections.append(([f"- Convention ({convention.ref}, approved): {convention.text[:200]}"], [convention.ref]))
    if not sections:
        return Hunch(files, [], [], "")
    used = tokens(LABEL) + tokens(FOOTER) + 2
    lines: list[str] = []
    sources: list[str] = []
    for section, refs in sections:
        cost = sum(tokens(line) + 1 for line in section)
        if used + cost > budget:
            continue
        lines += section
        sources += refs
        used += cost
    if not lines:
        return Hunch(files, [], [], "")
    text = "\n".join([LABEL, *lines, FOOTER])
    return Hunch(files, lines, list(dict.fromkeys(sources)), text, withheld=withhold_files)


# --------------------------------------------------------------------------- hooks


def _index_conn(workspace: Path, index_path: Path | None):
    """The first usable index database (no waiting for a build), or None."""
    if index_path is None:
        return None, None
    try:
        from critique_bot.memory.recall import index_targets

        for target in index_targets(workspace, index_path):
            conn = target.connect()
            if conn is not None:
                return target, conn
    except Exception as exc:  # no index: recall works from memory alone
        log.debug(f"memory: no index for recall: {exc}")
    return None, None


def _wrong_rate(store: Any) -> tuple[float, int]:
    history = json.loads(store.get_meta("hunch_history", "[]") or "[]")
    if not isinstance(history, list) or not history:
        return 0.0, 0
    return sum(1 for item in history if item) / len(history), len(history)


def begin_task(workspace: Path, index_path: Path | None, task: str, options: dict[str, Any] | None, slog: Any = None) -> str:
    """Engine hook (task start): the hunch block to append to the task's first message, or ""."""
    _last.update(files=[], task=task)
    try:
        from critique_bot.memory import tool as memory_tool

        memory_tool.set_session(getattr(slog, "id", "") if slog is not None and getattr(slog, "enabled", False) else "")
        if mode(options) == "off" or not task.strip():
            return ""
        path = memory.db_path(Path(workspace))
        if not path.is_file():
            return ""
        return _begin(Path(workspace), index_path, task, options or {}, slog, path)
    except Exception as exc:  # memory is a hint; it must never stop a task
        log.warn(f"project memory: recall failed: {exc}")
        return ""


def _begin(workspace: Path, index_path: Path | None, task: str, options: dict[str, Any], slog: Any, path: Path) -> str:
    from critique_bot.memory import glossary, recall

    started = time.monotonic()
    store = memory.MemoryStore(path)
    _target, conn = _index_conn(workspace, index_path)
    try:
        if conn is not None:
            glossary.sync_index(store, [conn])
        query = recall.parse(task)
        found = recall.gather(store, workspace, conn, query, include_untrusted=options.get("memory_untrusted") is True)
        budget = _budget(options)
        rate, seen = _wrong_rate(store)
        withhold = seen >= WRONG_HUNCH_MIN and rate > WRONG_HUNCH_LIMIT
        hunch = build_hunch(found, workspace, budget=budget, withhold_files=withhold)
    finally:
        if conn is not None:
            conn.close()
        store.close()
    if not hunch.text:
        return ""
    _last.update(files=[item.path for item in hunch.files] if not hunch.withheld else [])
    if slog is not None and getattr(slog, "enabled", False):
        slog.write("memory_recall", files=[{"path": item.path, "confidence": item.confidence} for item in hunch.files],
                   sources=hunch.sources, tokens=tokens(hunch.text), budget=budget, withheld=hunch.withheld or None,
                   seconds=round(time.monotonic() - started, 3), text=hunch.text)
    return hunch.text


def _budget(options: dict[str, Any]) -> int:
    raw = options.get("memory_budget")
    try:
        value = int(raw) if raw is not None and not isinstance(raw, bool) else DEFAULT_BUDGET
    except (TypeError, ValueError):
        value = DEFAULT_BUDGET
    return max(100, min(value, 4000))


def _card_builder(options: dict[str, Any], side_factory: Callable[[], Any] | None):
    from critique_bot.memory import cards

    if str(options.get("memory_cards") or "").lower() != "model" or side_factory is None:
        return cards.ExtractiveBuilder()
    return cards.ModelBuilder(side_ask(side_factory))


def side_ask(side_factory: Callable[[], Any]) -> Callable[[str], str]:
    """``ask(prompt) -> reply`` on a fresh second session (one request each)."""

    def ask(prompt: str) -> str:
        handle = side_factory()
        enter = getattr(handle, "__enter__", None)
        side = enter() if callable(enter) and not hasattr(handle, "send") else handle
        try:
            return str(side.send(prompt) or "")
        finally:
            close = getattr(handle, "__exit__", None) if handle is not side else None
            if callable(close):
                close(None, None, None)
            elif callable(getattr(side, "close", None)):
                side.close()

    return ask


def end_task(workspace: Path, index_path: Path | None, task: str, code: str, changed: list[str], reads: list[str],
             options: dict[str, Any] | None, slog: Any = None, *, side_factory: Callable[[], Any] | None = None) -> None:
    """Engine hook (after ``task_finished``): learn from the task. Never raises.

    Only a logged session writes memory (every row needs its provenance, and the episode is
    read from the log); with ``"session_log": false`` nothing is recorded."""
    try:
        logged = slog is not None and getattr(slog, "enabled", False) and getattr(slog, "id", "")
        if mode(options) != "full" or not logged or not _project_dir_exists(Path(workspace)):
            return
        _end(Path(workspace), index_path, task, code, list(changed or []), list(reads or []), options or {}, slog, side_factory)
    except Exception as exc:  # memory is a hint; it must never stop the session
        log.warn(f"project memory: could not record the task: {exc}")


def _end(workspace: Path, index_path: Path | None, task: str, code: str, changed: list[str], reads: list[str],
         options: dict[str, Any], slog: Any, side_factory: Callable[[], Any] | None) -> None:
    from critique_bot.memory import cards, episodes, glossary, patterns

    store = memory.MemoryStore(memory.db_path(workspace))
    target, conn = _index_conn(workspace, index_path)
    try:
        def defined(name: str) -> bool:
            if conn is None:
                return True
            short = name.replace("::", ".").rstrip("()").rpartition(".")[2]
            try:
                return conn.execute("SELECT 1 FROM symbols WHERE name = ? LIMIT 1", (short,)).fetchone() is not None
            except Exception:
                return False

        session = getattr(slog, "id", "") if slog is not None and getattr(slog, "enabled", False) else ""
        if session:
            events = slog.events()
            tainted = taint.tainted()
            episode = episodes.build(events, workspace=workspace, session=session, code=code, task=task, changed=changed,
                                     tainted=tainted, load=slog.load, defined=defined)
            if episode is not None:
                saved = store.add_episode(episode)
                if saved.kind == "positive":
                    patterns.add_session_change(store, changed, session=session, task=task, ts=saved.created)
                if saved.trusted:
                    glossary.learn(store, [saved.root_cause, saved.fix_summary], provenance=f"session:{session}",
                                   defined=defined if conn is not None else None, index_conns=[conn] if conn is not None else [])
                log.info(f"project memory: wrote {saved.ref} ({saved.kind}, {saved.outcome})")
        if _last.get("files") and changed and _last.get("task") == task:
            history = json.loads(store.get_meta("hunch_history", "[]") or "[]")
            history = (history if isinstance(history, list) else []) + [0 if _last["files"][0] in changed else 1]
            store.set_meta("hunch_history", json.dumps(history[-WRONG_HUNCH_WINDOW:]))
        _last.update(files=[], task="")
        builder = _card_builder(options, side_factory)
        started = time.monotonic()
        touched = list(dict.fromkeys(changed + reads))
        built = 0
        for rel in touched[:MAX_CARDS_PER_TASK]:
            if time.monotonic() - started > CARD_SECONDS:
                break
            _card, made = cards.ensure_file_card(store, workspace, rel, conn, builder)
            built += int(made)
        if conn is not None:
            _module_cards(store, workspace, conn, touched, builder, started)
            if target is not None:
                patterns.mine(store, conn, target.path, limit=MINE_COMMITS_PER_TASK)
    finally:
        if conn is not None:
            conn.close()
        store.close()


def _module_cards(store: Any, workspace: Path, conn: Any, touched: list[str], builder: Any, started: float) -> None:
    """Module cards for the modules the task touched, and cards for their top repo-map files."""
    from critique_bot.memory import cards
    from critique_bot.memory.recall import repo_map_files

    names = []
    for rel in touched[:10]:
        name = cards.module_of(conn, rel)
        if name and name not in names:
            names.append(name)
    for name in names[:3]:
        if time.monotonic() - started > CARD_SECONDS * 2:
            return
        for rel in repo_map_files(conn, name, limit=6):
            if time.monotonic() - started > CARD_SECONDS * 2:
                break
            cards.ensure_file_card(store, workspace, rel, conn, builder)
        cards.ensure_module_card(store, workspace, conn, name)
