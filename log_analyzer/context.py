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
    "for",
    "while",
    "do",
    "for_statement",
    "enhanced_for_statement",
    "while_statement",
    "do_statement",
    "do_while_statement",
    "for_in_statement",
    "for_expression",
    "while_expression",
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

CALL_NODE_TYPES = {"method_invocation", "call_expression", "call"}

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


def _is_loop_node(ntype: str) -> bool:
    return ntype in LOOP_NODE_TYPES or ntype.lower() in LOOP_NODE_TYPES


def contexts_from_ts_node(node, flavor: str, ancestors: list | None = None) -> ContextInfo:
    info = ContextInfo()
    seen: set[str] = set()

    def add(tag: str, reason: str) -> None:
        if tag not in seen:
            seen.add(tag)
            info.contexts.append(tag)
        info.reasons.append(reason)

    chain: list = []
    if ancestors:
        chain = list(reversed(ancestors))
    else:
        current = getattr(node, "parent", None)
        while current is not None:
            chain.append(current)
            current = getattr(current, "parent", None)

    for current in chain:
        ntype = current.type
        if _is_loop_node(ntype):
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


def _line_col_to_offset(source: str, line: int, column: int) -> int:
    parts = source.splitlines(keepends=True)
    if line < 1 or not parts:
        return 0
    if line > len(parts):
        return len(source)
    return sum(len(part) for part in parts[: line - 1]) + max(0, min(column, len(parts[line - 1])) - 1)


def _header_before(source: str, index: int) -> str:
    start = source.rfind("\n", 0, index)
    line = source[start + 1 : index].strip()
    if not line or line in {")", "]", "->", "=", ","}:
        if start >= 0:
            prev = source.rfind("\n", 0, start)
            prev_line = source[prev + 1 : start]
            line = f"{prev_line} {line}".strip()
    return re.sub(r"\s+", " ", line)


def _call_name_before(source: str, paren_index: int) -> str:
    j = paren_index - 1
    while j >= 0 and source[j].isspace():
        j -= 1
    end = j + 1
    while j >= 0 and (source[j].isalnum() or source[j] == "_"):
        j -= 1
    return source[j + 1 : end]


def _call_name_from_header(header: str) -> str:
    """Last call/keyword attached to the '{' or '(' that opened this scope."""
    text = header.strip()
    while text.endswith(")"):
        depth = 0
        cut = None
        for i in range(len(text) - 1, -1, -1):
            if text[i] == ")":
                depth += 1
            elif text[i] == "(":
                depth -= 1
                if depth == 0:
                    cut = i
                    break
        if cut is None:
            break
        text = text[:cut].rstrip()
    tokens = re.findall(r"[A-Za-z_]\w*", text)
    return tokens[-1].lower() if tokens else ""


def classify_header(header: str) -> list[tuple[str, str]]:
    if not header:
        return []
    tags: list[tuple[str, str]] = []
    snippet = header[:120]
    name = _call_name_from_header(header)
    if (
        name in LOOP_CALL_NAMES
        or name in {"for", "while", "do"}
        or re.search(r"\bfor\s*\(", header)
        or re.search(r"\bwhile\s*\(", header)
        or re.search(r"\bdo\b", header)
    ):
        tags.append(("loop", f"loop ← enclosing `{snippet}`"))
    if name in OBSERVER_CALL_NAMES:
        tags.append(("observer", f"observer ← enclosing `{snippet}`"))
    if _is_listener_call(name):
        tags.append(("listener", f"listener ← enclosing `{snippet}`"))
    return tags


def _scan_enclosing_headers(source: str, offset: int) -> list[str]:
    """Forward-scan to offset and return headers of open { } and qualifying ( ) scopes."""
    brace_stack: list[str] = []
    paren_stack: list[str] = []
    pending = ""
    i = 0
    n = min(offset, len(source))
    in_line = in_block = False
    quote = ""
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if in_line:
            if ch == "\n":
                in_line = False
            i += 1
            continue
        if in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if quote:
            if ch == "\\" and nxt:
                i += 2
                continue
            if source.startswith(quote, i):
                i += len(quote)
                quote = ""
                continue
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        if source.startswith('"""', i) or source.startswith("'''", i):
            quote = source[i : i + 3]
            i += 3
            continue
        if ch in {'"', "'"}:
            quote = ch
            i += 1
            continue
        if ch == "{":
            pending = ""
            brace_stack.append(_header_before(source, i))
        elif ch == "}" and brace_stack:
            brace_stack.pop()
        elif ch == "(":
            paren_stack.append(_call_name_before(source, i))
        elif ch == ")" and paren_stack:
            pending = paren_stack.pop()
        elif ch == ";" and not paren_stack:
            pending = ""
        i += 1
    headers = list(brace_stack)
    if pending:
        headers.append(pending + "()")
    for name in paren_stack:
        if name.lower() in LOOP_CALL_NAMES | OBSERVER_CALL_NAMES or _is_listener_call(name):
            headers.append(name + "()")
        elif name.lower() in {"for", "while", "do"}:
            headers.append(name + "()")
    return headers


def contexts_from_source_structure(source: str, line: int, column: int) -> ContextInfo:
    """Brace/paren-accurate context. Same on Linux and Windows; does not use nearby-line guesses."""
    info = contexts_from_source_text(source, line)
    info.reasons = [r for r in info.reasons if "not inferred from nearby text" not in r]
    offset = _line_col_to_offset(source, line, column)
    for header in _scan_enclosing_headers(source, offset):
        for tag, reason in classify_header(header):
            if tag not in info.contexts:
                info.contexts.append(tag)
            info.reasons.append(reason)
            info.ancestors.append(header[:80])
    return info


def apply_structural_contexts(finding: Finding, source: str) -> Finding:
    extra = contexts_from_source_structure(source, finding.line, finding.column or 1)
    for tag in extra.contexts:
        if tag not in finding.contexts:
            finding.contexts.append(tag)
    if extra.reasons:
        finding.context_reasons = list(dict.fromkeys(finding.context_reasons + extra.reasons))
    if extra.ancestors and not finding.ancestors:
        finding.ancestors = extra.ancestors
    if extra.enclosing_function and not finding.enclosing_function:
        finding.enclosing_function = extra.enclosing_function
    if extra.enclosing_class and not finding.enclosing_class:
        finding.enclosing_class = extra.enclosing_class
    return annotate_finding(finding)


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
