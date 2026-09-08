from __future__ import annotations

from pathlib import Path

from .context import annotate_finding, attach_source_window
from .models import Finding
from .parsers.regex_logs import analyze_regex

_SOURCE_PRIORITY = {"tree-sitter": 0, "javalang": 1, "regex": 2}
_AST_SOURCES = {"tree-sitter", "javalang"}


def _source_rank(finding: Finding) -> int:
    ranks = [_SOURCE_PRIORITY.get(src, 9) for src in finding.parse_sources]
    return min(ranks) if ranks else 9


def _safe_parse(label: str, fn) -> list[Finding]:
    try:
        return fn()
    except Exception:
        return []


def merge_findings(*groups: list[Finding]) -> list[Finding]:
    merged: dict[tuple[str, int, str, str], Finding] = {}
    for group in groups:
        for finding in group:
            key = finding.merge_key()
            existing = merged.get(key)
            if existing is None:
                merged[key] = finding
                continue
            primary, secondary = (
                (finding, existing)
                if _source_rank(finding) < _source_rank(existing)
                else (existing, finding)
            )
            sources = list(dict.fromkeys(primary.parse_sources + secondary.parse_sources))
            primary.parse_sources = sources
            if not primary.enclosing_function:
                primary.enclosing_function = secondary.enclosing_function
            if not primary.enclosing_class:
                primary.enclosing_class = secondary.enclosing_class
            primary_is_ast = bool(set(primary.parse_sources) & _AST_SOURCES)
            if not primary.contexts and not primary_is_ast:
                primary.contexts = secondary.contexts
                if not primary.context_reasons:
                    primary.context_reasons = secondary.context_reasons
            if not primary.ancestors:
                primary.ancestors = secondary.ancestors
            if not primary.snippet:
                primary.snippet = secondary.snippet
            merged[key] = annotate_finding(primary)
    return sorted(merged.values(), key=lambda f: (f.file, f.line, f.column, f.method))


def detect_source(relpath: str, source: bytes) -> list[Finding]:
    text = source.decode("utf-8", "replace")
    suffix = Path(relpath).suffix.lower()
    groups: list[list[Finding]] = []
    if suffix == ".java":
        from .parsers.java_ts import analyze_java_ts

        groups.append(_safe_parse("tree-sitter-java", lambda: analyze_java_ts(relpath, source)))
        from .parsers.javalang_java import analyze_java_javalang

        groups.append(_safe_parse("javalang", lambda: analyze_java_javalang(relpath, text)))
        groups.append(_safe_parse("regex", lambda: analyze_regex(relpath, text)))
    elif suffix in {".kt", ".kts"}:
        from .parsers.kotlin_ts import analyze_kotlin_ts

        groups.append(_safe_parse("tree-sitter-kotlin", lambda: analyze_kotlin_ts(relpath, source)))
        groups.append(_safe_parse("regex", lambda: analyze_regex(relpath, text)))
    else:
        return []
    findings = merge_findings(*groups)
    return [attach_source_window(finding, text) for finding in findings]
