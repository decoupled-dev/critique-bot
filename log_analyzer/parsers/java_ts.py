from __future__ import annotations

from tree_sitter import Language, Parser, Query, QueryCursor
import tree_sitter_java as tsjava

from ..classify import classify_call, snippet_from_text
from ..context import annotate_finding, contexts_from_ts_node
from ..models import Finding

_LANGUAGE: Language | None = None
_PARSER: Parser | None = None
_QUERY: Query | None = None


def _ensure() -> tuple[Parser, Query]:
    global _LANGUAGE, _PARSER, _QUERY
    if _LANGUAGE is None:
        _LANGUAGE = Language(tsjava.language())
        _PARSER = Parser(_LANGUAGE)
        _QUERY = Query(
            _LANGUAGE,
            """
            (method_invocation
              name: (identifier) @method
            ) @call
            """,
        )
    assert _PARSER is not None and _QUERY is not None
    return _PARSER, _QUERY


def _text(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None and node.text else ""


def analyze_java_ts(relpath: str, source: bytes) -> list[Finding]:
    parser, query = _ensure()
    tree = parser.parse(source)
    findings: list[Finding] = []
    matches = QueryCursor(query).matches(tree.root_node)
    for _pattern, captures in matches:
        call_nodes = captures.get("call") or []
        method_nodes = captures.get("method") or []
        if not call_nodes or not method_nodes:
            continue
        call = call_nodes[0]
        method = _text(method_nodes[0])
        obj = call.child_by_field_name("object")
        receiver = _text(obj)
        classified = classify_call(receiver, method)
        if classified is None:
            continue
        level, api = classified
        info = contexts_from_ts_node(call, "java")
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
            enclosing_class=info.enclosing_class,
            enclosing_function=info.enclosing_function,
            contexts=info.contexts,
            context_reasons=info.reasons,
            ancestors=info.ancestors,
        )
        findings.append(annotate_finding(finding))
    return findings
