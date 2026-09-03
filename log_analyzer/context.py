from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .models import Finding


@dataclass
class ContextInfo:
    enclosing_class: str = ""
    enclosing_function: str = ""
    contexts: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    ancestors: list[str] = field(default_factory=list)

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
    "foreachindexed",
    "oneach",
    "repeat",
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


def contexts_from_ts_node(node, flavor: str) -> ContextInfo:
    info = ContextInfo()
    seen: set[str] = set()

    def add(tag: str, reason: str) -> None:
        if tag not in seen:
            seen.add(tag)
            info.contexts.append(tag)
        info.reasons.append(reason)

    current = node.parent
    while current is not None:
        ntype = current.type
        if ntype in LOOP_NODE_TYPES:
            add("loop", f"loop ← AST ancestor `{ntype}`")
        if ntype in CLASS_NODE_TYPES and not info.enclosing_class:
            info.enclosing_class = node_name(current)
        if ntype in FUNCTION_NODE_TYPES and not info.enclosing_function:
            info.enclosing_function = node_name(current)
        if ntype in CALL_NODE_TYPES:
            call = _call_name(current, flavor).lower()
            info.ancestors.append(f"{ntype}:{call or '?'}")
            if call in LOOP_CALL_NAMES:
                add("loop", f"loop ← AST call `{call}()`")
            if call in OBSERVER_CALL_NAMES:
                add("observer", f"observer ← AST call `{call}()`")
            if _is_listener_call(call):
                add("listener", f"listener ← AST call `{call}()`")
        elif ntype in FUNCTION_NODE_TYPES or ntype in CLASS_NODE_TYPES:
            info.ancestors.append(f"{ntype}:{node_name(current) or '?'}")
        current = current.parent

    func_key = info.enclosing_function.lower()
    class_key = info.enclosing_class.lower()
    if func_key in HOT_METHODS or (
        func_key == "bind" and any(part in class_key for part in ("adapter", "holder", "viewholder"))
    ):
        add("hot_path", f"hot_path ← enclosing method `{info.enclosing_function}`")
    if func_key in LISTENER_METHODS or class_key.endswith("listener"):
        add("listener", f"listener ← enclosing `{info.enclosing_class}.{info.enclosing_function}`")
    return info


def contexts_from_javalang_path(path: Iterable[object]) -> ContextInfo:
    info = ContextInfo()
    seen: set[str] = set()

    def add(tag: str, reason: str) -> None:
        if tag not in seen:
            seen.add(tag)
            info.contexts.append(tag)
        info.reasons.append(reason)

    for item in path:
        name = type(item).__name__
        if name in {"ForStatement", "WhileStatement", "DoStatement"}:
            add("loop", f"loop ← javalang ancestor `{name}`")
        if name == "ClassDeclaration":
            info.enclosing_class = getattr(item, "name", "") or info.enclosing_class
            info.ancestors.append(f"{name}:{info.enclosing_class}")
        if name == "MethodDeclaration":
            info.enclosing_function = getattr(item, "name", "") or info.enclosing_function
            info.ancestors.append(f"{name}:{info.enclosing_function}")
        if name == "MethodInvocation":
            member = (getattr(item, "member", "") or "").lower()
            info.ancestors.append(f"{name}:{member or '?'}")
            if member in LOOP_CALL_NAMES:
                add("loop", f"loop ← javalang call `{member}()`")
            if member in OBSERVER_CALL_NAMES:
                add("observer", f"observer ← javalang call `{member}()`")
            if _is_listener_call(member):
                add("listener", f"listener ← javalang call `{member}()`")

    func_key = info.enclosing_function.lower()
    class_key = info.enclosing_class.lower()
    if func_key in HOT_METHODS or (
        func_key == "bind" and any(part in class_key for part in ("adapter", "holder", "viewholder"))
    ):
        add("hot_path", f"hot_path ← enclosing method `{info.enclosing_function}`")
    if func_key in LISTENER_METHODS or class_key.endswith("listener"):
        add("listener", f"listener ← enclosing `{info.enclosing_class}.{info.enclosing_function}`")
    return info


def contexts_from_source_text(source: str, line: int) -> ContextInfo:
    """Name/hot-path only. Do not guess loop/observer/listener from nearby text."""
    info = ContextInfo()
    lines = source.splitlines()
    idx = max(0, min(line - 1, len(lines) - 1))
    window = lines[max(0, idx - 40) : idx + 1]
    for raw in reversed(window):
        stripped = raw.strip()
        if not info.enclosing_class:
            class_match = re.search(r"\b(class|object|interface|enum)\s+(\w+)", stripped)
            if class_match:
                info.enclosing_class = class_match.group(2)
        if not info.enclosing_function:
            sig = _METHOD_SIG_RE.search(stripped)
            if sig and not stripped.startswith("//"):
                info.enclosing_function = sig.group(1)
        if info.enclosing_class and info.enclosing_function:
            break

    func_key = info.enclosing_function.lower()
    if func_key in HOT_METHODS:
        info.contexts.append("hot_path")
        info.reasons.append(f"hot_path ← enclosing method `{info.enclosing_function}` (regex)")
    info.reasons.append("loop/observer/listener not inferred from nearby text")
    return info


def attach_source_window(finding: Finding, source: str, radius: int = 10) -> Finding:
    lines = source.splitlines()
    idx = finding.line - 1
    if idx < 0 or idx >= len(lines):
        return finding
    start = max(0, idx - radius)
    end = min(len(lines), idx + radius + 1)

    def numbered(i: int) -> str:
        mark = ">" if i == idx else " "
        return f"{mark}{i + 1:>5}|{lines[i]}"

    finding.source_before = "\n".join(numbered(i) for i in range(start, idx))
    finding.source_line = numbered(idx)
    finding.source_after = "\n".join(numbered(i) for i in range(idx + 1, end))
    finding.source_window = "\n".join(
        numbered(i) for i in range(start, end)
    )
    return finding


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
