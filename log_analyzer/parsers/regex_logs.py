from __future__ import annotations

import re

from ..classify import classify_call, snippet_from_text
from ..context import annotate_finding, contexts_from_source_text
from ..models import Finding

_LOG_CALL = re.compile(
    r"(?P<recv>android\s*\.\s*util\s*\.\s*Log|Log|Timber)"
    r"(?:\s*\.\s*tag\s*\((?:[^()]|\([^()]*\))*\))?"
    r"\s*\.\s*(?P<method>v|d|i|w|e|wtf|println)\s*\(",
)

_SYSTEM_PRINT = re.compile(
    r"System\s*\.\s*(?P<stream>out|err)\s*\.\s*(?P<method>print(?:ln)?)\s*\(",
)

_KOTLIN_PRINT = re.compile(
    r"(?<![\w.])(?P<method>println|print)\s*\(",
)

_WRAPPER = re.compile(
    r"(?P<recv>\b\w*(?:[Ll]og|[Ll]ogger|[Tt]imber)\w*)\s*\.\s*"
    r"(?P<method>v|d|i|w|e|wtf|verbose|debug|info|warn|warning|error)\s*\(",
)


def _mask_strings(source: str) -> str:
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        if source.startswith('"""', i) or source.startswith("'''", i):
            quote = source[i : i + 3]
            out.append(quote)
            i += 3
            while i < n and not source.startswith(quote, i):
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append(quote)
                i += 3
            continue
        if source[i] in {'"', "'"}:
            quote = source[i]
            out.append(quote)
            i += 1
            while i < n:
                if source[i] == "\\":
                    out.append("  ")
                    i = min(n, i + 2)
                    continue
                if source[i] == quote:
                    out.append(quote)
                    i += 1
                    break
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            continue
        out.append(source[i])
        i += 1
    return "".join(out)


def _strip_comments(source: str) -> str:
    without_block = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), source, flags=re.S)
    lines = []
    for line in without_block.splitlines(keepends=True):
        in_string = False
        quote = ""
        chars: list[str] = []
        i = 0
        while i < len(line):
            ch = line[i]
            if in_string:
                chars.append(ch)
                if ch == "\\" and i + 1 < len(line):
                    chars.append(line[i + 1])
                    i += 2
                    continue
                if ch == quote:
                    in_string = False
                i += 1
                continue
            if ch in {'"', "'"}:
                in_string = True
                quote = ch
                chars.append(ch)
                i += 1
                continue
            if ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                chars.append(" " * (len(line) - i - (1 if line.endswith("\n") else 0)))
                if line.endswith("\n"):
                    chars.append("\n")
                break
            chars.append(ch)
            i += 1
        lines.append("".join(chars) if chars else line)
    return "".join(lines)


def _add(
    findings: list[Finding],
    relpath: str,
    source: str,
    original: str,
    match: re.Match[str],
    receiver: str,
    method: str,
) -> None:
    classified = classify_call(receiver, method)
    if classified is None:
        return
    level, api = classified
    line = source[: match.start()].count("\n") + 1
    last_nl = source.rfind("\n", 0, match.start())
    column = match.start() - last_nl
    info = contexts_from_source_text(original, line)
    line_end = original.find("\n", match.start())
    if line_end < 0:
        line_end = len(original)
    snippet = snippet_from_text(original[match.start() : line_end])
    finding = Finding(
        file=relpath,
        line=line,
        column=column,
        level=level,
        api=api,
        method=method,
        receiver=receiver.replace(" ", ""),
        snippet=snippet,
        parse_sources=["regex"],
        enclosing_class=info.enclosing_class,
        enclosing_function=info.enclosing_function,
        contexts=info.contexts,
        context_reasons=info.reasons,
        ancestors=info.ancestors,
    )
    findings.append(annotate_finding(finding))


def analyze_regex(relpath: str, source: str) -> list[Finding]:
    cleaned = _mask_strings(_strip_comments(source))
    findings: list[Finding] = []
    for match in _LOG_CALL.finditer(cleaned):
        recv = re.sub(r"\s+", "", match.group("recv"))
        method = match.group("method")
        _add(findings, relpath, cleaned, source, match, recv, method)
    for match in _SYSTEM_PRINT.finditer(cleaned):
        receiver = f"System.{match.group('stream')}"
        _add(findings, relpath, cleaned, source, match, receiver, match.group("method"))
    if relpath.endswith(".kt"):
        for match in _KOTLIN_PRINT.finditer(cleaned):
            _add(findings, relpath, cleaned, source, match, "", match.group("method"))
    for match in _WRAPPER.finditer(cleaned):
        recv = match.group("recv")
        if recv in {"Log", "Timber"}:
            continue
        _add(findings, relpath, cleaned, source, match, recv, match.group("method"))
    return findings
