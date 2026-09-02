from __future__ import annotations

import javalang
from javalang.tree import MethodInvocation

from ..classify import classify_call, snippet_from_text
from ..context import annotate_finding, contexts_from_javalang_path
from ..models import Finding


def _qualifier_text(qualifier: object | None) -> str:
    if qualifier is None:
        return ""
    if isinstance(qualifier, str):
        return qualifier
    return str(qualifier)


def _line_col(node) -> tuple[int, int]:
    pos = getattr(node, "position", None)
    if pos is None:
        return 1, 1
    return int(pos.line or 1), int(pos.column or 1)


def _source_snippet(source: str, line: int, column: int) -> str:
    lines = source.splitlines()
    if 1 <= line <= len(lines):
        text = lines[line - 1]
        if column > 1:
            text = text[column - 1 :]
        return snippet_from_text(text)
    return ""


def _emit(
    relpath: str,
    source: str,
    path,
    receiver: str,
    method: str,
    line: int,
    column: int,
) -> Finding | None:
    classified = classify_call(receiver, method)
    if classified is None:
        return None
    level, api = classified
    class_name, func_name, contexts = contexts_from_javalang_path(path)
    finding = Finding(
        file=relpath,
        line=line,
        column=column,
        level=level,
        api=api,
        method=method,
        receiver=receiver,
        snippet=_source_snippet(source, line, column),
        parse_sources=["javalang"],
        enclosing_class=class_name,
        enclosing_function=func_name,
        contexts=contexts,
    )
    return annotate_finding(finding)


def analyze_java_javalang(relpath: str, source: str) -> list[Finding]:
    try:
        tree = javalang.parse.parse(source)
    except (javalang.parser.JavaSyntaxError, javalang.tokenizer.LexerError, IndexError, TypeError):
        return []

    findings: list[Finding] = []
    for path, node in tree.filter(MethodInvocation):
        receiver = _qualifier_text(node.qualifier)
        method = node.member or ""
        line, column = _line_col(node)
        hit = _emit(relpath, source, path, receiver, method, line, column)
        if hit is not None:
            findings.append(hit)
        for selector in node.selectors or []:
            if not isinstance(selector, MethodInvocation):
                continue
            chained_recv = receiver
            if method.lower() == "tag" and (
                receiver == "Timber" or receiver.endswith(".Timber")
            ):
                chained_recv = f"{receiver}.tag"
            sel_line, sel_col = _line_col(selector)
            if sel_line == 1 and sel_col == 1:
                sel_line, sel_col = line, column
            chained = _emit(
                relpath,
                source,
                path,
                chained_recv,
                selector.member or "",
                sel_line,
                sel_col,
            )
            if chained is not None:
                findings.append(chained)
    return findings
