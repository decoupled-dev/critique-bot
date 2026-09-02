from __future__ import annotations

from tree_sitter import Language, Parser, Query, QueryCursor
import tree_sitter_kotlin as tskotlin

from ..classify import classify_call, snippet_from_text
from ..context import annotate_finding, contexts_from_ts_node
from ..models import Finding

_LANGUAGE: Language | None = None
_PARSER: Parser | None = None
_QUERY: Query | None = None


def _ensure() -> tuple[Parser, Query]:
    global _LANGUAGE, _PARSER, _QUERY
    if _LANGUAGE is None:
        _LANGUAGE = Language(tskotlin.language())
        _PARSER = Parser(_LANGUAGE)
        _QUERY = Query(
            _LANGUAGE,
            """
            (call_expression) @call
            """,
        )
    assert _PARSER is not None and _QUERY is not None
    return _PARSER, _QUERY


def _text(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None and node.text else ""


def _receiver_and_method(call) -> tuple[str, str]:
    named = call.named_children
    if not named:
        return "", ""
    first = named[0]
    if first.type == "identifier":
        return "", _text(first)
    if first.type == "navigation_expression":
        children = first.named_children
        if len(children) >= 2 and children[-1].type == "identifier":
            return _text(children[0]), _text(children[-1])
        identifiers = [c for c in children if c.type == "identifier"]
        if identifiers:
            method = _text(identifiers[-1])
            receiver = _text(first)
            if receiver.endswith("." + method):
                receiver = receiver[: -len(method) - 1]
            return receiver, method
    return "", ""


def analyze_kotlin_ts(relpath: str, source: bytes) -> list[Finding]:
    parser, query = _ensure()
    tree = parser.parse(source)
    findings: list[Finding] = []
    matches = QueryCursor(query).matches(tree.root_node)
    for _pattern, captures in matches:
        call_nodes = captures.get("call") or []
        if not call_nodes:
            continue
        call = call_nodes[0]
        receiver, method = _receiver_and_method(call)
        if not method:
            continue
        classified = classify_call(receiver, method)
        if classified is None:
            continue
        level, api = classified
        class_name, func_name, contexts = contexts_from_ts_node(call, "kotlin")
        finding = Finding(
            file=relpath,
            line=call.start_point[0] + 1,
            column=call.start_point[1] + 1,
            level=level,
            api=api,
            method=method,
            receiver=receiver,
            snippet=snippet_from_text(_text(call)),
            parse_sources=["tree-sitter"],
            enclosing_class=class_name,
            enclosing_function=func_name,
            contexts=contexts,
        )
        findings.append(annotate_finding(finding))
    return findings
