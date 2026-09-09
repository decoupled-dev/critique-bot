from __future__ import annotations

import tree_sitter_java as tsjava

from ..classify import classify_call, snippet_from_text
from ..context import annotate_finding, contexts_from_ts_node
from ..models import Finding
from .tscompat import iter_with_ancestors, make_language, make_parser

_PARSER = None


def _ensure():
    global _PARSER
    if _PARSER is None:
        language = make_language(tsjava, "java")
        _PARSER = make_parser(language)
    return _PARSER


def _text(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None and node.text else ""


def analyze_java_ts(relpath: str, source: bytes) -> list[Finding]:
    parser = _ensure()
    tree = parser.parse(source)
    findings: list[Finding] = []
    for call, ancestors in iter_with_ancestors(tree.root_node):
        if call.type != "method_invocation":
            continue
        name_node = call.child_by_field_name("name")
        method = _text(name_node)
        if not method:
            continue
        receiver = _text(call.child_by_field_name("object"))
        classified = classify_call(receiver, method)
        if classified is None:
            continue
        level, api = classified
        info = contexts_from_ts_node(call, "java", ancestors=ancestors)
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
