"""``crit audit``: print the audit log (``~/.crit/logs/audit.jsonl``).

    crit audit                      the last 50 records
    crit audit --session ID         one session (a prefix of its id is enough)
    crit audit --since 2h           since a time or a span back (30m, 2h, 1d, 2026-11-27T10:00)
    crit audit --kind command       only some kinds (command, write, egress, permission, taint, model, sandbox)
    crit audit --all / --limit N    everything / the last N
    crit audit --json               raw JSON lines
    crit audit --sessions           list the sessions in the log

Apps layer.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from critique_bot import audit, log


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crit audit", description="Show what crit did: commands, writes, network, permissions, taint.")
    parser.add_argument("--session", help="only this session (an id or its prefix)")
    parser.add_argument("--since", help="only records since a time (2026-11-27, 2026-11-27T10:00) or a span (30m, 2h, 1d)")
    parser.add_argument("--kind", action="append", choices=audit.KINDS, help="only this kind (repeatable)")
    parser.add_argument("--limit", type=int, default=50, help="show the last N records (default 50)")
    parser.add_argument("--all", action="store_true", help="show every matching record")
    parser.add_argument("--json", action="store_true", help="print raw JSON lines")
    parser.add_argument("--sessions", action="store_true", help="list sessions with their first time and record count")
    return parser


def _when(entry: dict[str, Any]) -> str:
    ts = str(entry.get("ts") or "")
    return ts.replace("T", " ")[:19]


def describe(entry: dict[str, Any]) -> str:
    """One readable line for a record."""
    kind = str(entry.get("kind") or "?")
    if kind == "command":
        exit_text = entry.get("result") or (f"exit {entry.get('exit')}" if "exit" in entry else "started")
        if entry.get("background"):
            exit_text = f"background {entry['background']}"
        return f"[{entry.get('sandbox', 'full')}] {entry.get('command', '')}  ({exit_text}; in {entry.get('cwd', '')})"
    if kind == "write":
        return f"{entry.get('tool', '')}: {', '.join(entry.get('paths') or [])}"
    if kind == "egress":
        verdict = "allowed" if entry.get("allowed") else "DENIED"
        size = f"{entry.get('sent', 0)}B up, {entry.get('received', 0)}B down"
        why = f" ({entry['reason']})" if entry.get("reason") and not entry.get("allowed") else ""
        error = f" error: {entry['error']}" if entry.get("error") else ""
        return f"{verdict} {entry.get('method', '')} {entry.get('host', '')}:{entry.get('port', '')} {size}{why}{error}"
    if kind == "permission":
        verdict = "allowed" if entry.get("allowed") else "denied"
        extra = f" ({entry['reason']})" if entry.get("reason") else ""
        mode = f" [{entry['mode']}]" if entry.get("mode") else ""
        return f"{verdict}{mode} {entry.get('summary') or entry.get('tool', '')}{extra}"
    if kind == "taint":
        if entry.get("event") == "lifted":
            return f"lifted by {entry.get('reason', '/trust')}"
        detail = f": {entry['detail']}" if entry.get("detail") else ""
        return f"raised by {entry.get('source', '?')}{detail}"
    if kind == "model":
        return f"{entry.get('endpoint', '')} ({entry.get('backend', '')})"
    if kind == "sandbox":
        rest = {key: value for key, value in entry.items() if key not in {"ts", "kind", "session"}}
        return " ".join(f"{key}={value}" for key, value in rest.items())
    rest = {key: value for key, value in entry.items() if key not in {"ts", "kind", "session"}}
    return json.dumps(rest, ensure_ascii=False)


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        records = list(audit.read(session_id=args.session, since=args.since, kinds=tuple(args.kind or ())))
    except ValueError as exc:
        log.print_safe(f"crit audit: {exc}")
        return 2
    if args.sessions:
        seen: dict[str, list[Any]] = {}
        for entry in records:
            key = str(entry.get("session") or "")
            if key not in seen:
                seen[key] = [_when(entry), 0]
            seen[key][1] += 1
        if not seen:
            log.print_safe(f"No records in {audit.path()}.")
        for key, (first, count) in seen.items():
            log.print_safe(f"{key}  {first}  {count} records")
        return 0
    if not args.all and args.limit > 0:
        records = records[-args.limit:]
    if not records:
        log.print_safe(f"No matching records in {audit.path()}.")
        return 0
    for entry in records:
        if args.json:
            log.print_safe(json.dumps(entry, ensure_ascii=False))
        else:
            log.print_safe(f"{_when(entry)}  {str(entry.get('session') or '')[:22]:<22}  {entry.get('kind', '?'):<10}  {describe(entry)}")
    return 0
