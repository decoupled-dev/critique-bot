# SPDX-License-Identifier: Apache-2.0
"""``crit sessions``, ``crit resume`` and ``crit fork``: the logged sessions of a project.

``crit sessions``                    list them (id, when, status, turns, tokens, title)
``crit sessions show <id>``          a readable transcript rendered from the event log
``crit sessions export <id>``        the whole session as JSON (``--redacted`` for sharing)
``crit sessions prune``              remove old ones (30 days / 200 sessions, or the settings)
``crit resume [<id>|--last] ...``    continue one; the rest is an ordinary crit command line
``crit fork <id> [--at SEQ] ...``    a copy of one up to event SEQ, continued as a new session

An id may be shortened to any unique start (or its 4-character end). The logs live in
``.crit/sessions/<id>/`` (see docs/sessions.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from critique_bot import session_log

RESUME_FLAG = "--resume"


def _home(repo_dir: str) -> Path | None:
    from critique_bot.project_home import find_bot_home

    home = find_bot_home(Path(repo_dir).resolve())
    return home.sessions_dir if home is not None else None


def _settings_int(repo_dir: str, key: str, default: int) -> int:
    from critique_bot.project_home import find_bot_home

    home = find_bot_home(Path(repo_dir).resolve())
    raw = (home.settings if home is not None else {}).get(key)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else default


def _when(text: str) -> str:
    return text.replace("T", " ")[:16] if text else "?"


def print_list(sessions_dir: Path, *, limit: int = 30, out=None) -> int:
    out = out or sys.stdout
    rows = session_log.list_sessions(sessions_dir)
    if not rows:
        print("No sessions with a log in this project yet.", file=out)
        return 0
    for row in rows[:limit]:
        tokens = f"{row.tokens / 1000:.1f}k" if row.tokens >= 1000 else str(row.tokens)
        fork = f" (fork of {row.parent})" if row.parent else ""
        print(
            f"{row.id}  {_when(row.updated)}  {row.status:<11} {row.turns:>4} turns {tokens:>7} tok  "
            f"{row.title[:60]}{fork}",
            file=out,
        )
    if len(rows) > limit:
        print(f"... {len(rows) - limit} more (crit sessions --limit {len(rows)})", file=out)
    return 0


def main_sessions(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="crit sessions", description="List, show, export, and prune logged sessions.")
    parser.add_argument("--repo-dir", default=".", help="the project (default: this folder)")
    sub = parser.add_subparsers(dest="action")
    listing = sub.add_parser("list", help="list sessions, newest first (the default)")
    listing.add_argument("--limit", type=int, default=30)
    show = sub.add_parser("show", help="a readable transcript of one session")
    show.add_argument("id")
    show.add_argument("--full", action="store_true", help="include long tool outputs and file contents")
    export = sub.add_parser("export", help="one session as JSON (blobs inlined)")
    export.add_argument("id")
    export.add_argument("--redacted", action="store_true",
                        help="redact again and replace the workspace and home paths, for sharing")
    export.add_argument("-o", "--output", default=None, help="write to this file instead of stdout")
    prune = sub.add_parser("prune", help="remove old sessions")
    prune.add_argument("--days", type=int, default=None, help=f"keep this many days (default {session_log.KEEP_DAYS})")
    prune.add_argument("--keep", type=int, default=None, help=f"keep at most this many (default {session_log.KEEP_COUNT})")
    prune.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    sessions_dir = _home(args.repo_dir)
    if sessions_dir is None:
        print("No crit project here (no .crit folder).", file=sys.stderr)
        return 1
    action = args.action or "list"
    try:
        if action == "list":
            return print_list(sessions_dir, limit=getattr(args, "limit", 30))
        if action == "show":
            folder = session_log.find_session(sessions_dir, args.id)
            print(session_log.render_transcript(session_log.read_events(folder), folder, full=args.full), end="")
            return 0
        if action == "export":
            folder = session_log.find_session(sessions_dir, args.id)
            text = json.dumps(session_log.export_session(folder, redacted=args.redacted), indent=2, ensure_ascii=False)
            if args.output:
                Path(args.output).write_text(text + "\n", encoding="utf-8")
                print(f"wrote {args.output}", file=sys.stderr)
            else:
                print(text)
            return 0
        if action == "prune":
            days = args.days if args.days is not None else _settings_int(args.repo_dir, "sessions_keep_days", session_log.KEEP_DAYS)
            keep = args.keep if args.keep is not None else _settings_int(args.repo_dir, "sessions_keep", session_log.KEEP_COUNT)
            removed = session_log.prune_sessions(sessions_dir, keep_days=days, keep_count=keep, dry_run=args.dry_run)
            verb = "would remove" if args.dry_run else "removed"
            print(f"{verb} {len(removed)} session{'s' if len(removed) != 1 else ''}"
                  + (": " + ", ".join(removed) if removed else ""))
            return 0
    except session_log.SessionNotFound as exc:
        print(f"crit sessions: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


USAGE = {
    "resume": (
        "usage: crit resume [<id>|--last] [crit options] [task]\n\n"
        "Continue a logged session of this project (crit sessions lists them; no id: the newest).\n"
        "A model API session gets its conversation back up to the last complete turn; the web chat\n"
        "starts a new chat with a summary of the log. Options and the task are crit's usual ones."
    ),
    "fork": (
        "usage: crit fork [<id>|--last] [--at SEQ] [crit options] [task]\n\n"
        "Copy a logged session up to event SEQ (all of it without --at) into a new session and\n"
        "continue the copy; the original is left as it was. crit sessions show <id> numbers the events."
    ),
}


def wants_help(argv: list[str]) -> bool:
    return bool(argv) and argv[0] in {"-h", "--help"}


def split_resume(argv: list[str]) -> tuple[str, list[str]]:
    """``crit resume [<id>|--last] rest...`` -> (ref, rest). No id means the newest session."""
    rest = list(argv)
    ref = "last"
    if rest and rest[0] == "--last":
        rest.pop(0)
    elif rest and not rest[0].startswith("-"):
        ref = rest.pop(0)
    return ref, rest


def split_fork(argv: list[str]) -> tuple[str, int | None, list[str]]:
    """``crit fork <id> [--at SEQ] rest...`` -> (ref, seq, rest)."""
    rest = list(argv)
    ref = "last"
    if rest and rest[0] == "--last":
        rest.pop(0)
    elif rest and not rest[0].startswith("-"):
        ref = rest.pop(0)
    at: int | None = None
    for index, word in enumerate(rest):
        if word == "--at" and index + 1 < len(rest):
            at = int(rest[index + 1])
            del rest[index : index + 2]
            break
        if word.startswith("--at="):
            at = int(word.split("=", 1)[1])
            del rest[index]
            break
    return ref, at, rest


def resume_argv(argv: list[str], repo_dir: str = ".") -> list[str] | str:
    """The crit command line that continues the session (``--resume <id>``), or an error."""
    ref, rest = split_resume(argv)
    sessions_dir = _home(_repo_dir(rest, repo_dir))
    if sessions_dir is None:
        return "no crit project here (no .crit folder)"
    try:
        folder = session_log.find_session(sessions_dir, ref)
    except session_log.SessionNotFound as exc:
        return str(exc)
    return [*rest, RESUME_FLAG, folder.name]


def fork_argv(argv: list[str], repo_dir: str = ".") -> tuple[list[str], str] | str:
    """Fork the session and return (the command line that continues the fork, a line to print)."""
    try:
        ref, at, rest = split_fork(argv)
    except ValueError:
        return "--at takes an event number (seq); crit sessions show <id> lists them"
    sessions_dir = _home(_repo_dir(rest, repo_dir))
    if sessions_dir is None:
        return "no crit project here (no .crit folder)"
    try:
        fork = session_log.fork_session(sessions_dir, ref, at=at)
    except session_log.SessionNotFound as exc:
        return str(exc)
    line = f"Forked {fork.meta.get('parent')} at event {fork.meta.get('parent_seq')} into {fork.id}"
    return [*rest, RESUME_FLAG, fork.id], line


def _repo_dir(rest: list[str], default: str) -> str:
    for index, word in enumerate(rest):
        if word == "--repo-dir" and index + 1 < len(rest):
            return rest[index + 1]
        if word.startswith("--repo-dir="):
            return word.split("=", 1)[1]
    return default
