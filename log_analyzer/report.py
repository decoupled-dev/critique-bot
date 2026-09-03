from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .models import FileError, Finding, ScanStats

_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "report.html"

_AI_GUIDE = {
    "purpose": (
        "Investigation pack for an Android Java/Kotlin logging audit. "
        "Use this JSON (also embedded in the HTML) to decide which log calls "
        "are actually chatty and which context tags are trustworthy."
    ),
    "how_to_read": [
        "files[] is sorted most log calls → least.",
        "findings[] includes source_window (numbered lines, `>` marks the call).",
        "context_reasons[] explains every loop/observer/listener/hot_path tag.",
        "ancestors[] is the AST parent chain used for those tags.",
        "loop is applied only when the call is an AST descendant of for/while/do "
        "or of forEach/forEachIndexed/onEach/repeat. Nearby loops in the same "
        "method do NOT count.",
        "parse_sources lists which parsers found the call (tree-sitter, javalang, regex).",
        "chatty_score is ranking only; it is not a proof the call is wrong.",
    ],
    "schema": {
        "finding": [
            "file",
            "line",
            "column",
            "level",
            "api",
            "method",
            "receiver",
            "snippet",
            "parse_sources",
            "enclosing_class",
            "enclosing_function",
            "contexts",
            "context_reasons",
            "ancestors",
            "source_window",
            "chatty_score",
            "why",
        ]
    },
    "scoring": {
        "base": {"v": 3, "d": 3, "i": 2, "w": 1, "e": 1, "wtf": 1, "println": 3, "print": 3},
        "multipliers": {
            "loop": 5,
            "bind_draw_scroll": 8,
            "other_hot_path": 4,
            "observer": 4,
            "listener": 3,
        },
    },
}


def _payload(
    findings: list[Finding],
    errors: list[FileError],
    stats: ScanStats,
) -> dict:
    levels = ["v", "d", "i", "w", "e", "wtf", "println", "print"]
    by_level = {level: 0 for level in levels}
    by_context = {"loop": 0, "observer": 0, "listener": 0, "hot_path": 0}
    files: dict[str, dict] = {}
    high_freq = 0
    for finding in findings:
        by_level[finding.level] = by_level.get(finding.level, 0) + 1
        for ctx in finding.contexts:
            if ctx in by_context:
                by_context[ctx] += 1
        if finding.contexts:
            high_freq += 1
        bucket = files.setdefault(
            finding.file,
            {
                "path": finding.file,
                "count": 0,
                "high_freq": 0,
                "max_score": 0,
                "levels": {},
                "functions": {},
            },
        )
        bucket["count"] += 1
        if finding.contexts:
            bucket["high_freq"] += 1
        bucket["max_score"] = max(bucket["max_score"], finding.chatty_score)
        bucket["levels"][finding.level] = bucket["levels"].get(finding.level, 0) + 1
        func = finding.enclosing_function or "(unknown)"
        bucket["functions"][func] = bucket["functions"].get(func, 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "root": stats.root,
        "ai_guide": _AI_GUIDE,
        "stats": {
            "files_scanned": stats.files_scanned,
            "files_with_findings": stats.files_with_findings,
            "findings": stats.findings,
            "parse_failures": stats.parse_failures,
            "bytes_scanned": stats.bytes_scanned,
            "high_freq": high_freq,
            "by_level": by_level,
            "by_context": by_context,
        },
        "files": sorted(
            files.values(),
            key=lambda item: (-item["count"], -item["high_freq"], item["path"]),
        ),
        "findings": [finding.to_dict() for finding in findings],
        "errors": [error.to_dict() for error in errors],
    }


def render_html(
    findings: list[Finding],
    errors: list[FileError],
    stats: ScanStats,
    output: Path,
) -> Path:
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = _payload(findings, errors, stats)
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    pretty = json.dumps(payload, ensure_ascii=False, indent=2)
    data = data.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = template.replace("<<<LOG_ANALYZER_JSON>>>", data)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    sidecar = output.with_suffix(".investigation.json")
    sidecar.write_text(pretty + "\n", encoding="utf-8")
    return output
