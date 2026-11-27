# SPDX-License-Identifier: Apache-2.0
"""``crit memory``: look at, review and maintain the project memory (``.crit/memory/memory.db``).

``crit memory status``               what is stored, the setting, the wrong-hunch rate
``crit memory show <id>``            one item with its sources (``e12``, ``n3``, ``f40``, ``r2``, ``h5``, ``m1``)
``crit memory list [kind]``          items of one kind: episodes, conventions, cards, recipes, hotspots, glossary
``crit memory forget <id>``          delete one item
``crit memory review``               approve or reject proposed conventions; ``--stale`` checks approved ones
``crit memory export [-o FILE]``     episodes, approved conventions, recipes, hot spots, glossary as JSON
``crit memory import FILE``          add an export (conventions come in as proposals; ``--trust`` for episodes)
``crit memory prune``                remove old negative episodes, cards of deleted files, rejected notes
``crit memory mine``                 read new git history into recipes and hot spots now

See docs/memory.md.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from critique_bot import memory
from critique_bot.memory import conventions as conventions_mod
from critique_bot.memory.store import MemoryStore
from critique_bot.memory.tool import describe

KINDS = ("episodes", "conventions", "cards", "recipes", "hotspots", "glossary")
EXPORT_SCHEMA = 1


def _project(repo_dir: str) -> tuple[Path, Path | None, dict[str, Any]]:
    from critique_bot.project_home import find_bot_home

    home = find_bot_home(Path(repo_dir).resolve())
    if home is None:
        return Path(repo_dir).resolve(), None, {}
    return home.root, home.index_path, dict(home.settings)


def _open(root: Path, *, create: bool = False) -> MemoryStore | None:
    path = memory.db_path(root)
    if not path.is_file() and not create:
        return None
    return MemoryStore(path)


def _defined(index_path: Path | None) -> Callable[[str], bool] | None:
    if index_path is None or not Path(index_path).is_file():
        return None
    import sqlite3

    def defined(name: str) -> bool:
        short = name.replace("::", ".").rstrip("()").rpartition(".")[2]
        conn = sqlite3.connect(str(index_path))
        try:
            return conn.execute("SELECT 1 FROM symbols WHERE name = ? LIMIT 1", (short,)).fetchone() is not None
        except sqlite3.DatabaseError:
            return True
        finally:
            conn.close()

    return defined


def cmd_status(root: Path, settings: dict[str, Any], out: Any) -> int:
    store = _open(root)
    print(f"setting: memory = {memory.mode(settings)}", file=out)
    if store is None:
        print(f"No project memory yet ({memory.db_path(root)} is created by the first finished task).", file=out)
        return 0
    try:
        print(f"database: {store.path} ({store.path.stat().st_size // 1024} KB, full-text search: "
              f"{'yes' if store.fts else 'no'})", file=out)
        for name, count in store.counts().items():
            print(f"  {name:<22} {count}", file=out)
        history = json.loads(store.get_meta("hunch_history", "[]") or "[]")
        if isinstance(history, list) and history:
            rate = sum(1 for item in history if item) / len(history)
            print(f"wrong-hunch rate: {rate:.0%} over the last {len(history)} tasks with a hunch and edits", file=out)
    finally:
        store.close()
    return 0


def cmd_show(root: Path, ref: str, out: Any) -> int:
    store = _open(root)
    if store is None:
        print("No project memory yet.", file=out)
        return 1
    try:
        item = store.get(ref)
        if item is None:
            print(f"No memory item {ref!r} (ids look like e12, n3, f40, r2, h5, m1, g7).", file=out)
            return 1
        print(describe(item, root, full=True), file=out)
        return 0
    finally:
        store.close()


def cmd_list(root: Path, kind: str, limit: int, out: Any) -> int:
    store = _open(root)
    if store is None:
        print("No project memory yet.", file=out)
        return 0
    try:
        if kind == "episodes":
            items: list[Any] = store.episodes()[-limit:]
        elif kind == "conventions":
            items = store.conventions()
        elif kind == "cards":
            items = store.cards()[:limit]
        elif kind == "recipes":
            items = store.recipes(limit=limit)
        elif kind == "hotspots":
            items = store.hotspots(limit=limit)
        else:
            items = [dict(row) for row in store.glossary()][:limit]
        if not items:
            print(f"No {kind} yet.", file=out)
        for item in items:
            print(describe(item, root), file=out)
        return 0
    finally:
        store.close()


def cmd_forget(root: Path, ref: str, out: Any) -> int:
    store = _open(root)
    if store is None:
        print("No project memory yet.", file=out)
        return 1
    try:
        try:
            gone = store.forget(ref)
        except ValueError as exc:
            print(str(exc), file=out)
            return 1
        print(f"Forgot {ref}." if gone else f"No memory item {ref!r}.", file=out)
        return 0 if gone else 1
    finally:
        store.close()


def _ask(question: str) -> str:
    try:
        return input(question).strip().lower()
    except EOFError:
        return ""


def cmd_review(root: Path, index_path: Path | None, args: argparse.Namespace, out: Any,
               ask: Callable[[str], str] = _ask) -> int:
    """Approve or reject proposals (interactively, or with --approve/--reject); --stale checks approved notes."""
    from critique_bot.memory import glossary

    store = _open(root)
    if store is None:
        print("No project memory yet.", file=out)
        return 0
    try:
        changed = 0

        def approve(item: Any) -> None:
            store.set_convention_status(item.id, "approved")
            glossary.learn(store, [item.text], provenance=f"user:approved {item.ref}")
            print(f"Approved {item.ref}.", file=out)

        for ref in args.approve or []:
            item = store.get(ref)
            if item is None or not ref.lower().startswith("n"):
                print(f"No convention {ref!r}.", file=out)
                return 1
            approve(item)
            changed += 1
        for ref in args.reject or []:
            item = store.get(ref)
            if item is None or not ref.lower().startswith("n"):
                print(f"No convention {ref!r}.", file=out)
                return 1
            store.set_convention_status(item.id, "rejected", "rejected in review")
            print(f"Rejected {item.ref}.", file=out)
            changed += 1
        if args.stale:
            flagged = conventions_mod.stale_check(store, root, defined=_defined(index_path))
            for item, why in flagged:
                print(f"Stale: {item.ref} {item.text[:100]} ({why})", file=out)
            stale = store.conventions("stale")
            if not stale:
                print("No stale conventions.", file=out)
            for item in stale:
                if args.yes is None and not sys.stdin.isatty():
                    break
                answer = "r" if args.yes else ask(f"{item.ref} is stale ({item.note}). [r]emove, [k]eep, [s]kip? ")
                if answer.startswith("r"):
                    store.forget(item.ref)
                    print(f"Removed {item.ref}.", file=out)
                elif answer.startswith("k"):
                    store.set_convention_status(item.id, "approved", "kept after a stale check")
                    print(f"Kept {item.ref}.", file=out)
            return 0
        if changed:
            return 0
        pending = store.conventions("proposed")
        if not pending:
            print("No proposed conventions to review.", file=out)
            return 0
        for item in pending:
            print(describe(item, root, full=True), file=out)
            answer = ask("[a]pprove, [r]eject, [s]kip, [q]uit? ")
            if answer.startswith("a"):
                approve(item)
            elif answer.startswith("r"):
                store.set_convention_status(item.id, "rejected", "rejected in review")
                print(f"Rejected {item.ref}.", file=out)
            elif answer.startswith("q") or not answer:
                break
        return 0
    finally:
        store.close()


def export_data(store: MemoryStore, *, include_untrusted: bool = False, cards: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema": EXPORT_SCHEMA,
        "exported": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "episodes": [],
        "conventions": [],
        "recipes": [],
        "hotspots": [],
        "glossary": [],
    }
    for episode in store.episodes():
        if not episode.trusted and not include_untrusted:
            continue
        data["episodes"].append({
            "task": episode.task, "class": episode.cls, "kind": episode.kind, "outcome": episode.outcome,
            "trusted": episode.trusted, "files_changed": episode.files_changed, "symbols": episode.symbols,
            "root_cause": episode.root_cause, "fix_summary": episode.fix_summary, "evidence": episode.evidence,
            "session": episode.session, "commit": episode.commit, "files_hash": episode.files_hash,
            "provenance": episode.provenance, "created": episode.created,
        })
    for item in store.conventions("approved"):
        data["conventions"].append({"text": item.text, "evidence": item.evidence, "provenance": item.provenance})
    for item in store.recipes(limit=500):
        data["recipes"].append({"files": item.files, "count": item.count, "commits": item.commits, "subjects": item.subjects,
                                "sessions": item.sessions, "last_ts": item.last_ts, "provenance": item.provenance})
    for item in store.hotspots(limit=500):
        data["hotspots"].append({"path": item.path, "fixes": item.fixes, "commits": item.commits,
                                 "last_fix_ts": item.last_fix_ts, "fix_commits": item.fix_commits, "provenance": item.provenance})
    for row in store.glossary():
        data["glossary"].append({"term": row["term"], "expansion": row["expansion"], "provenance": row["provenance"]})
    if cards:
        data["cards"] = [{"kind": c.kind, "key": c.key, "hash": c.hash, "text": c.text, "symbols": c.symbols,
                          "builder": c.builder, "provenance": c.provenance} for c in store.cards()]
    return data


def import_data(store: MemoryStore, data: dict[str, Any], *, source: str, trust: bool = False) -> dict[str, int]:
    """Add an export. Episodes are untrusted unless ``trust``; conventions arrive as proposals;
    each row's provenance says where it came from."""
    if not isinstance(data, dict) or data.get("schema") != EXPORT_SCHEMA:
        raise ValueError("not a crit memory export (schema 1)")
    tag = f"import:{source}"
    counts = {"episodes": 0, "conventions": 0, "recipes": 0, "hotspots": 0, "glossary": 0, "cards": 0}
    for item in data.get("episodes") or []:
        if not isinstance(item, dict) or not item.get("task"):
            continue
        episode = dict(item)
        episode["trusted"] = bool(trust and item.get("trusted", True))
        episode["provenance"] = f"{tag} ({item.get('provenance') or 'no source'})"[:300]
        store.add_episode(episode)
        counts["episodes"] += 1
    for item in data.get("conventions") or []:
        if isinstance(item, dict) and item.get("text") and not conventions_mod.generic_reason(str(item["text"])):
            store.add_convention(str(item["text"]), [str(e) for e in item.get("evidence") or []] + [f"imported from {source}"],
                                 provenance=tag, status="proposed")
            counts["conventions"] += 1
    for item in data.get("recipes") or []:
        files = item.get("files") if isinstance(item, dict) else None
        if isinstance(files, list) and len(files) >= 2:
            for _ in range(max(1, min(int(item.get("count") or 1), 50))):
                store.add_change_set([str(f) for f in files], ts=int(item.get("last_ts") or 0), provenance=tag)
            counts["recipes"] += 1
    for item in data.get("hotspots") or []:
        if isinstance(item, dict) and item.get("path"):
            store.put_hotspot(str(item["path"]), fixes=int(item.get("fixes") or 0), commits=int(item.get("commits") or 0),
                              last_fix_ts=int(item.get("last_fix_ts") or 0),
                              fix_commits=[str(c) for c in item.get("fix_commits") or []], provenance=tag)
            counts["hotspots"] += 1
    for item in data.get("glossary") or []:
        if isinstance(item, dict) and store.add_glossary(str(item.get("term") or ""), str(item.get("expansion") or ""),
                                                         provenance=tag):
            counts["glossary"] += 1
    store.commit()
    return counts


def cmd_prune(root: Path, args: argparse.Namespace, out: Any) -> int:
    from critique_bot.memory import cards

    store = _open(root)
    if store is None:
        print("No project memory yet.", file=out)
        return 0
    try:
        now = time.time()
        removed: list[str] = []
        for episode in store.episodes():
            age = (now - episode.created) / 86400
            gone = episode.files_changed and all(not (root / rel).exists() for rel in episode.files_changed)
            if (episode.kind == "negative" and age > args.negative_days) or (age > args.days and gone):
                removed.append(episode.ref)
        for item in store.conventions("rejected"):
            removed.append(item.ref)
        if args.dry_run:
            stale_cards = [card.ref for card in store.cards("file")
                           if not (root / card.key).is_file() or cards.file_hash(root / card.key) != card.hash]
            print(f"Would remove {len(removed)} item(s) and {len(stale_cards)} outdated card(s): "
                  + ", ".join((removed + stale_cards)[:30]), file=out)
            return 0
        for ref in removed:
            store.forget(ref)
        dropped = cards.invalidate(store, root)
        print(f"Removed {len(removed)} item(s) and {len(dropped)} outdated card(s).", file=out)
        return 0
    finally:
        store.close()


def cmd_mine(root: Path, index_path: Path | None, out: Any) -> int:
    from critique_bot.memory import patterns
    from critique_bot.memory.recall import index_targets

    targets = index_targets(root, index_path) if index_path is not None else []
    if not targets:
        print("No code index yet; run a crit task here first (it builds .crit/cache/index.db).", file=out)
        return 1
    store = _open(root, create=True)
    assert store is not None
    try:
        total = 0
        for target in targets:
            conn = target.connect()
            if conn is None:
                continue
            try:
                total += patterns.mine(store, conn, target.path)
            finally:
                conn.close()
        counts = store.counts()
        print(f"Mined {total} new commit(s): {counts['recipes']} recipes, {counts['hot spots']} hot spots.", file=out)
        return 0
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crit memory", description="Look at, review and maintain the project memory.")
    parser.add_argument("--repo-dir", default=".", help="the project (default: this folder)")
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("status", help="what is stored (the default)")
    show = sub.add_parser("show", help="one item with its sources")
    show.add_argument("id")
    listing = sub.add_parser("list", help="the items of one kind")
    listing.add_argument("kind", choices=KINDS, nargs="?", default="episodes")
    listing.add_argument("--limit", type=int, default=50)
    forget = sub.add_parser("forget", help="delete one item")
    forget.add_argument("id")
    review = sub.add_parser("review", help="approve or reject proposed conventions")
    review.add_argument("--approve", action="append", metavar="ID")
    review.add_argument("--reject", action="append", metavar="ID")
    review.add_argument("--stale", action="store_true", help="check approved conventions against the code and flag stale ones")
    review.add_argument("--yes", action="store_true", default=None, help="with --stale: remove every stale one without asking")
    export = sub.add_parser("export", help="memory as JSON, for sharing within a team")
    export.add_argument("-o", "--output", default=None)
    export.add_argument("--include-untrusted", action="store_true", help="include episodes from tainted sessions")
    export.add_argument("--cards", action="store_true", help="include file and module cards")
    imported = sub.add_parser("import", help="add a memory export")
    imported.add_argument("file")
    imported.add_argument("--trust", action="store_true", help="recall the imported episodes (default: stored untrusted)")
    prune = sub.add_parser("prune", help="remove old and outdated items")
    prune.add_argument("--days", type=int, default=365, help="episodes whose files are all gone, older than this")
    prune.add_argument("--negative-days", type=int, default=90, help="negative episodes older than this")
    prune.add_argument("--dry-run", action="store_true")
    sub.add_parser("mine", help="read new git history into recipes and hot spots")
    return parser


def main(argv: list[str], *, out: Any = None, ask: Callable[[str], str] = _ask) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    root, index_path, settings = _project(args.repo_dir)
    action = args.action or "status"
    if action == "status":
        return cmd_status(root, settings, out)
    if action == "show":
        return cmd_show(root, args.id, out)
    if action == "list":
        return cmd_list(root, args.kind, args.limit, out)
    if action == "forget":
        return cmd_forget(root, args.id, out)
    if action == "review":
        return cmd_review(root, index_path, args, out, ask)
    if action == "export":
        store = _open(root)
        if store is None:
            print("No project memory yet.", file=out)
            return 1
        try:
            data = export_data(store, include_untrusted=args.include_untrusted, cards=args.cards)
        finally:
            store.close()
        text = json.dumps(data, indent=2, ensure_ascii=False)
        if args.output:
            Path(args.output).write_text(text + "\n", encoding="utf-8")
            print(f"Wrote {args.output}.", file=out)
        else:
            print(text, file=out)
        return 0
    if action == "import":
        try:
            data = json.loads(Path(args.file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"Could not read {args.file}: {exc}", file=out)
            return 1
        store = _open(root, create=True)
        assert store is not None
        try:
            counts = import_data(store, data, source=Path(args.file).name, trust=args.trust)
        except ValueError as exc:
            print(str(exc), file=out)
            return 1
        finally:
            store.close()
        print("Imported " + ", ".join(f"{n} {k}" for k, n in counts.items() if n) + "."
              + (" Conventions wait for crit memory review." if counts["conventions"] else ""), file=out)
        return 0
    if action == "prune":
        return cmd_prune(root, args, out)
    return cmd_mine(root, index_path, out)
