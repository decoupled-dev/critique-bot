from __future__ import annotations

import re
from typing import Iterable

from .models import Finding

LOOP_NODE_TYPES = {
    "for_statement",
    "enhanced_for_statement",
    "while_statement",
    "do_statement",
    "do_while_statement",
    "for_in_statement",
}

CLASS_NODE_TYPES = {
    "class_declaration",
    "object_declaration",
    "interface_declaration",
    "enum_declaration",
    "companion_object",
}

FUNCTION_NODE_TYPES = {
    "method_declaration",
    "function_declaration",
    "constructor_declaration",
    "secondary_constructor",
}

CALL_NODE_TYPES = {"method_invocation", "call_expression"}

LOOP_CALL_NAMES = {
    "foreach",
    "oneach",
    "repeat",
    "map",
    "flatmap",
}

OBSERVER_CALL_NAMES = {
    "observe",
    "observeforever",
    "collect",
    "collectlatest",
    "collectindexed",
    "subscribe",
    "addobserver",
    "observeon",
}

LISTENER_CALL_HINTS = (
    "listener",
    "watcher",
    "callback",
)

LISTENER_CALL_PREFIXES = ("seton", "addon")

LISTENER_CALL_NAMES = {
    "setonclicklistener",
    "addonscrolllistener",
    "addtextchangedlistener",
    "setontouchlistener",
    "addupdatelistener",
    "setonitemclicklistener",
    "setonlongclicklistener",
    "addonlayoutchangelistener",
    "setonscrollchangelistener",
    "addonpropertychangedcallback",
}

HOT_METHODS = {
    "onbindviewholder",
    "onbind",
    "getview",
    "ondraw",
    "dispatchdraw",
    "onscrolled",
    "onscroll",
    "ontouchevent",
    "onsensorchanged",
    "onlocationchanged",
    "ontextchanged",
    "aftertextchanged",
    "onmeasure",
    "onlayout",
    "onbindview",
}

BIND_DRAW_SCROLL = {
    "onbindviewholder",
    "onbind",
    "getview",
    "ondraw",
    "dispatchdraw",
    "onscrolled",
    "onscroll",
    "ontouchevent",
    "onmeasure",
    "onlayout",
    "onbindview",
}

LISTENER_METHODS = {
    "onclick",
    "onlongclick",
    "ontouch",
    "onscroll",
    "onscrolled",
    "onitemclick",
    "oncheckedchanged",
    "onprogresschanged",
    "onpagescrolled",
    "onpageselected",
    "onreceive",
    "onchanged",
    "beforetextchanged",
    "ontextchanged",
    "aftertextchanged",
}

BASE_SCORE = {
    "v": 3,
    "d": 3,
    "i": 2,
    "w": 1,
    "e": 1,
    "wtf": 1,
    "println": 3,
    "print": 3,
}

_METHOD_SIG_RE = re.compile(
    r"(?:fun|void|public|protected|private|override|static)[\s\w<>,\[\].?]*\b(\w+)\s*\(",
)


def node_name(node) -> str:
    named = node.child_by_field_name("name") if hasattr(node, "child_by_field_name") else None
    if named is not None and named.text:
        return named.text.decode("utf-8", "replace")
    for child in getattr(node, "named_children", []):
        if child.type == "identifier" and child.text:
            return child.text.decode("utf-8", "replace")
    return ""


def java_call_name(node) -> str:
    name = node.child_by_field_name("name") if node.type == "method_invocation" else None
    if name is not None and name.text:
        return name.text.decode("utf-8", "replace")
    return node_name(node)


def kotlin_call_name(node) -> str:
    if node.type != "call_expression" or not node.named_children:
        return node_name(node)
    first = node.named_children[0]
    if first.type == "identifier" and first.text:
        return first.text.decode("utf-8", "replace")
    if first.type == "navigation_expression":
        identifiers = [c for c in first.named_children if c.type == "identifier" and c.text]
        if identifiers:
            return identifiers[-1].text.decode("utf-8", "replace")
    if first.type == "call_expression":
        return kotlin_call_name(first)
    return node_name(node)


def _call_name(node, flavor: str) -> str:
    if flavor == "java":
        return java_call_name(node)
    return kotlin_call_name(node)


def _is_listener_call(name: str) -> bool:
    lowered = name.lower()
    if lowered in LISTENER_CALL_NAMES:
        return True
    if any(hint in lowered for hint in LISTENER_CALL_HINTS):
        return True
    return lowered.startswith(LISTENER_CALL_PREFIXES)


def contexts_from_ts_node(node, flavor: str) -> tuple[str, str, list[str]]:
    enclosing_class = ""
    enclosing_function = ""
    contexts: list[str] = []
    seen: set[str] = set()

    def add(tag: str) -> None:
        if tag not in seen:
            seen.add(tag)
            contexts.append(tag)

    current = node.parent
    while current is not None:
        ntype = current.type
        if ntype in LOOP_NODE_TYPES:
            add("loop")
        if ntype in CLASS_NODE_TYPES and not enclosing_class:
            enclosing_class = node_name(current)
        if ntype in FUNCTION_NODE_TYPES and not enclosing_function:
            enclosing_function = node_name(current)
        if ntype in CALL_NODE_TYPES:
            call = _call_name(current, flavor).lower()
            if call in LOOP_CALL_NAMES:
                add("loop")
            if call in OBSERVER_CALL_NAMES:
                add("observer")
            if _is_listener_call(call):
                add("listener")
        current = current.parent

    func_key = enclosing_function.lower()
    class_key = enclosing_class.lower()
    if func_key in HOT_METHODS or (
        func_key == "bind" and any(part in class_key for part in ("adapter", "holder", "viewholder"))
    ):
        add("hot_path")
    if func_key in LISTENER_METHODS or class_key.endswith("listener"):
        add("listener")
    return enclosing_class, enclosing_function, contexts


def contexts_from_javalang_path(path: Iterable[object]) -> tuple[str, str, list[str]]:
    enclosing_class = ""
    enclosing_function = ""
    contexts: list[str] = []
    seen: set[str] = set()

    def add(tag: str) -> None:
        if tag not in seen:
            seen.add(tag)
            contexts.append(tag)

    for item in path:
        name = type(item).__name__
        if name in {"ForStatement", "WhileStatement", "DoStatement"}:
            add("loop")
        if name == "ClassDeclaration":
            enclosing_class = getattr(item, "name", "") or enclosing_class
        if name == "MethodDeclaration":
            enclosing_function = getattr(item, "name", "") or enclosing_function
        if name == "MethodInvocation":
            member = (getattr(item, "member", "") or "").lower()
            if member in LOOP_CALL_NAMES:
                add("loop")
            if member in OBSERVER_CALL_NAMES:
                add("observer")
            if _is_listener_call(member):
                add("listener")

    func_key = enclosing_function.lower()
    class_key = enclosing_class.lower()
    if func_key in HOT_METHODS or (
        func_key == "bind" and any(part in class_key for part in ("adapter", "holder", "viewholder"))
    ):
        add("hot_path")
    if func_key in LISTENER_METHODS or class_key.endswith("listener"):
        add("listener")
    return enclosing_class, enclosing_function, contexts


def contexts_from_source_text(source: str, line: int) -> tuple[str, str, list[str]]:
    """Best-effort context for regex-only hits using nearby source lines."""
    lines = source.splitlines()
    idx = max(0, min(line - 1, len(lines) - 1))
    window = lines[max(0, idx - 40) : idx + 1]
    enclosing_function = ""
    enclosing_class = ""
    for raw in reversed(window):
        stripped = raw.strip()
        if not enclosing_class:
            class_match = re.search(r"\b(class|object|interface|enum)\s+(\w+)", stripped)
            if class_match:
                enclosing_class = class_match.group(2)
        if not enclosing_function:
            sig = _METHOD_SIG_RE.search(stripped)
            if sig and not stripped.startswith("//"):
                enclosing_function = sig.group(1)
        if enclosing_class and enclosing_function:
            break

    nearby = "\n".join(window[-12:])
    contexts: list[str] = []
    if re.search(r"\b(for|while|do)\b", nearby) or re.search(
        r"\b(forEach|onEach|repeat)\s*[({]", nearby
    ):
        contexts.append("loop")
    if re.search(r"\b(observe|observeForever|collect|collectLatest|subscribe)\s*\(", nearby):
        contexts.append("observer")
    if re.search(r"(Listener|Watcher|setOn\w+|addOn\w+|addTextChangedListener)", nearby):
        contexts.append("listener")
    func_key = enclosing_function.lower()
    if func_key in HOT_METHODS:
        contexts.append("hot_path")
    return enclosing_class, enclosing_function, contexts


def chatty_score(level: str, contexts: list[str], enclosing_function: str) -> int:
    score = BASE_SCORE.get(level, 2)
    tags = set(contexts)
    func_key = (enclosing_function or "").lower()
    if "loop" in tags:
        score *= 5
    if func_key in BIND_DRAW_SCROLL or (
        "hot_path" in tags and func_key in BIND_DRAW_SCROLL | {"bind"}
    ):
        score *= 8
    elif "hot_path" in tags:
        score *= 4
    if "observer" in tags:
        score *= 4
    if "listener" in tags:
        score *= 3
    return score


def why_noisy(
    api: str,
    method: str,
    contexts: list[str],
    enclosing_function: str,
    enclosing_class: str,
) -> str:
    call = f"{api}.{method}" if api not in {"print", "println"} else method
    bits: list[str] = [call]
    location = enclosing_function or enclosing_class
    if location:
        bits.append(f"in {location}()")
    labels = {
        "loop": "inside a loop / forEach",
        "observer": "inside an observer / collector",
        "listener": "inside a listener / callback",
        "hot_path": "on a high-frequency UI/sensor path",
    }
    for tag in contexts:
        if tag in labels:
            bits.append(labels[tag])
    if len(bits) == 1:
        return f"{call} — review if this can run often"
    return " + ".join(bits)


def annotate_finding(finding: Finding) -> Finding:
    finding.contexts = list(dict.fromkeys(finding.contexts))
    finding.chatty_score = chatty_score(
        finding.level, finding.contexts, finding.enclosing_function
    )
    finding.why = why_noisy(
        finding.api,
        finding.method,
        finding.contexts,
        finding.enclosing_function,
        finding.enclosing_class,
    )
    return finding
