"""tree-sitter Language/Parser helpers that work across 0.20–0.26 on Linux."""

from __future__ import annotations

from tree_sitter import Language, Parser, Node


def make_language(lang_module, name: str) -> Language:
    ptr = lang_module.language()
    errors: list[str] = []
    for args in ((ptr,), (ptr, name)):
        try:
            return Language(*args)
        except TypeError as exc:
            errors.append(str(exc))
    raise TypeError(
        f"Cannot construct tree-sitter Language for {name}: " + "; ".join(errors)
    )


def make_parser(language: Language) -> Parser:
    try:
        return Parser(language)
    except TypeError:
        parser = Parser()
        setter = getattr(parser, "set_language", None)
        if callable(setter):
            setter(language)
        else:
            parser.language = language
        return parser


def iter_named(root: Node):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        children = list(node.children)
        for child in reversed(children):
            stack.append(child)


def iter_with_ancestors(root: Node):
    """Walk the CST with an explicit ancestor stack.

    Some Linux tree-sitter builds leave ``node.parent`` as None. The stack
    is the reliable way to know whether a call sits inside a loop.
    """
    stack: list[tuple[Node, list[Node]]] = [(root, [])]
    while stack:
        node, ancestors = stack.pop()
        yield node, ancestors
        nxt = ancestors + [node]
        children = list(node.children)
        for child in reversed(children):
            stack.append((child, nxt))
