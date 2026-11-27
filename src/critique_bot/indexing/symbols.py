# SPDX-License-Identifier: Apache-2.0
"""L2: definitions (kind, signature, line range, container) and the calls inside them.

Three ways, best first:

- Python: the standard library ``ast`` (exact, always available).
- Tree-sitter (the ``index`` extra) for Java, Kotlin, Go, Rust, C, C++, C#, JavaScript,
  TypeScript: node-type tables per language, read with the field names the grammars share
  (``name``, ``function``, ``field``, ...), so one walker serves every grammar version.
- A regex pass per language when tree-sitter is missing (and always for AIDL and HIDL).

:func:`analyze` returns the definitions and, when a parser was available, the calls it saw
(``None`` otherwise, so :mod:`.facts` finds calls by regex instead).
"""

from __future__ import annotations

import ast
import importlib
import re
import threading
import warnings
from dataclasses import dataclass

#: Kinds that contain other definitions (their name becomes the container of what is inside).
CONTAINER_KINDS = frozenset({"class", "interface", "enum", "struct", "trait", "object", "impl", "union"})


@dataclass(frozen=True)
class Symbol:
    path: str
    line: int
    end_line: int
    kind: str
    name: str
    parent: str
    signature: str

    @property
    def qualified(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name


# --------------------------------------------------------------------------- regex fallback

_CLASS_RE = re.compile(r"\b(class|interface|enum|record|struct|trait|object|union)\s+([A-Za-z_]\w*)")
_FUNC_RE = {
    "java": re.compile(
        r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:public|private|protected|static|final|synchronized|native|abstract|default)\s+)+"
        r"(?!class\b|interface\b|enum\b|record\b)(?:<[^>]*>\s*)?[\w.<>,?\[\] ]+?\s+([A-Za-z_]\w*)\s*\(",
        re.M,
    ),
    "kotlin": re.compile(
        r"^\s*(?:(?:public|private|protected|internal|open|override|suspend|inline|operator|abstract|final|tailrec|infix|external)\s+)*"
        r"fun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?([A-Za-z_]\w*)\s*\(",
        re.M,
    ),
    "go": re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*[\[(]", re.M),
    "rust": re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"\w+\"\s+)?fn\s+([A-Za-z_]\w*)", re.M),
    "c": re.compile(r"^\s*(?:[\w*]+\s+)+\**([A-Za-z_]\w*)\s*\([^;{]*\)\s*\{", re.M),
    "cpp": re.compile(r"^\s*(?:[\w:*&<>,]+\s+)+[*&]*((?:[A-Za-z_]\w*::)*~?[A-Za-z_]\w*)\s*\([^;{]*\)\s*(?:const\s*)?(?:override\s*)?(?:noexcept\s*)?\{", re.M),
    "csharp": re.compile(
        r"^\s*(?:(?:public|private|protected|internal|static|async|virtual|override|sealed|abstract)\s+)+"
        r"(?!class\b|interface\b|enum\b|struct\b|record\b)[\w.<>,\[\]]+\s+([A-Za-z_]\w*)\s*\(",
        re.M,
    ),
    "javascript": re.compile(
        r"(?:function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"
        r"|^\s*(?:static\s+|async\s+|get\s+|set\s+)*(?!if\b|for\b|while\b|switch\b|catch\b|return\b)([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{)",
        re.M,
    ),
}
_FUNC_RE["python"] = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", re.M)
_FUNC_RE["typescript"] = re.compile(
    r"(?:function\s*\*?\s*([A-Za-z_$][\w$]*)\s*[<(]|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)"
    r"|^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+|get\s+|set\s+)*(?!if\b|for\b|while\b|switch\b|catch\b|return\b)([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\([^)]*\)\s*(?::\s*[^{]+)?\{)",
    re.M,
)
_NOT_NAMES = frozenset({"if", "for", "while", "switch", "catch", "return", "sizeof", "new", "else", "do", "when", "try"})
_BRACED = frozenset({"java", "kotlin", "go", "rust", "c", "cpp", "csharp", "javascript", "typescript", "aidl", "hidl"})
_CLASS_KIND = {"record": "class", "object": "object"}

# AIDL: interface IFoo { void bar(in int x); oneway void baz(); } / parcelable Foo / enum E
_AIDL_TYPE = re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:oneway\s+)?(interface|parcelable|enum|union)\s+([A-Za-z_]\w*)", re.M)
_AIDL_METHOD = re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:oneway\s+)?[\w.<>\[\], ]+?\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*(?:=\s*\d+\s*)?;", re.M)
# HIDL: interface IFoo extends @1.0::IBar { get() generates (Result r); }; struct / enum / union / typedef
_HIDL_TYPE = re.compile(r"^\s*(interface|struct|enum|union|safe_union)\s+([A-Za-z_]\w*)\b", re.M)
_HIDL_METHOD = re.compile(r"^\s*(?:oneway\s+)?([A-Za-z_]\w*)\s*\([^)]*\)\s*(?:generates\s*\([^)]*\))?\s*;", re.M)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _in_comment(line_text: str, text: str, offset: int) -> bool:
    """True when a regex match sits in a comment (a doc line, or after ``//``)."""
    stripped = line_text.lstrip()
    if stripped.startswith(("*", "//", "/*", "#")) and not stripped.startswith(("#include", "#define")):
        return True
    start = text.rfind("\n", 0, offset) + 1
    return "//" in text[start:offset]


def _enclosing(found: list[Symbol], line: int) -> str:
    inner = ""
    for item in found:
        if item.kind in CONTAINER_KINDS and item.line < line <= item.end_line:
            inner = item.name
    return inner


def brace_end(text: str, start: int, line: int) -> int:
    """Line of the brace that closes the first ``{`` at or after ``start``."""
    open_at = text.find("{", start)
    newline = text.find("\n", start)
    if open_at < 0 or (newline >= 0 and open_at > newline + 200):
        return line
    semi = text.find(";", start, open_at)
    if semi >= 0 and text.count("(", start, semi) == text.count(")", start, semi):
        return line  # a declaration that ends before the brace (a prototype)
    depth = 0
    index = open_at
    length = len(text)
    while index < length:
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return _line_of(text, index)
        elif char in "\"'":
            quote = char
            index += 1
            while index < length and text[index] != quote:
                if text[index] == "\\":
                    index += 1
                if text[index : index + 1] == "\n":
                    break
                index += 1
        elif char == "/" and text[index : index + 2] == "//":
            index = text.find("\n", index)
            if index < 0:
                break
        elif char == "/" and text[index : index + 2] == "/*":
            index = text.find("*/", index + 2)
            if index < 0:
                break
            index += 1
        index += 1
    return line


def regex_symbols(text: str, language: str) -> list[Symbol]:
    if language == "aidl":
        return _idl_symbols(text, _AIDL_TYPE, _AIDL_METHOD)
    if language == "hidl":
        return _idl_symbols(text, _HIDL_TYPE, _HIDL_METHOD)
    found: list[Symbol] = []
    if language not in _FUNC_RE and language not in _BRACED and language != "python":
        return found
    lines = text.split("\n")
    brace = language in _BRACED
    for match in _CLASS_RE.finditer(text):
        line = _line_of(text, match.start())
        if _in_comment(lines[line - 1], text, match.start()):
            continue
        keyword = match.group(1)
        if language in {"c", "cpp", "rust", "go"} and keyword == "object":
            continue
        if language in {"c", "cpp"}:
            # a definition has a body: skip "struct foo;" / "struct foo *p" / "enum class X" uses
            after = text[match.end() : match.end() + 300]
            if not re.match(r"\s*(?:final\s*)?(?::[^;{()]*)?\{", after):
                continue
        end = brace_end(text, match.end(), line) if brace else _python_end(lines, line)
        kind = _CLASS_KIND.get(keyword, keyword)
        found.append(Symbol("", line, end, kind, match.group(2), "", lines[line - 1].strip()))
    if language == "go":
        for match in re.finditer(r"^[ \t]*type\s+([A-Za-z_]\w*)\s+(struct|interface)\b", text, re.M):
            line = _line_of(text, match.start(1))
            found.append(Symbol("", line, brace_end(text, match.end(), line), match.group(2), match.group(1), "", lines[line - 1].strip()))
    if language == "rust":
        for match in re.finditer(r"^[ \t]*impl(?:<[^>]*>)?\s+(?:([\w:]+)(?:<[^>]*>)?\s+for\s+)?([\w:]+)", text, re.M):
            line = _line_of(text, match.start(2))
            name = match.group(2).rpartition("::")[2]
            found.append(Symbol("", line, brace_end(text, match.end(), line), "impl", name, "", lines[line - 1].strip()))
    func_re = _FUNC_RE.get(language)
    if func_re is not None:
        containers = [item for item in found]
        for match in func_re.finditer(text):
            index = next((number for number, group in enumerate(match.groups(), start=1) if group), None)
            if index is None:
                continue
            name = match.group(index)
            if name in _NOT_NAMES:
                continue
            line = _line_of(text, match.start(index))
            row = lines[line - 1]
            if _in_comment(row, text, match.start(index)):
                continue
            if language == "kotlin" and re.search(r"\)\s*(?::\s*[\w<>?,. ]+)?\s*=", row):
                end = line  # an expression body: fun f() = ...
            else:
                end = brace_end(text, match.start(index), line) if brace else line
            if language == "python":
                end = _python_end(lines, line)
            parent = _enclosing(containers, line)
            if "::" in name:  # C++ out-of-class definition: Foo::bar
                parent, _, name = name.rpartition("::")
                parent = parent.rpartition("::")[2]
            if language == "go":
                receiver = re.match(r"\s*func\s+\(\s*\w*\s*\*?\s*([A-Za-z_]\w*)", row)
                if receiver:
                    parent = receiver.group(1)
            kind = "method" if parent else "function"
            found.append(Symbol("", line, end, kind, name, parent, row.strip()))
    found.sort(key=lambda item: (item.line, item.kind))
    return _dedupe(found)


def _python_end(lines: list[str], line: int) -> int:
    """Last line of an indented Python block that starts at ``line``."""
    indent = len(lines[line - 1]) - len(lines[line - 1].lstrip())
    end = line
    for number in range(line, len(lines)):
        row = lines[number]
        if not row.strip():
            continue
        if len(row) - len(row.lstrip()) <= indent:
            break
        end = number + 1
    return end


def _idl_symbols(text: str, type_re: re.Pattern[str], method_re: re.Pattern[str]) -> list[Symbol]:
    lines = text.split("\n")
    found: list[Symbol] = []
    for match in type_re.finditer(text):
        line = _line_of(text, match.start(1))
        if _in_comment(lines[line - 1], text, match.start(1)):
            continue
        keyword = match.group(1)
        kind = {"parcelable": "class", "typedef": "class"}.get(keyword, keyword)
        end = brace_end(text, match.end(), line)
        found.append(Symbol("", line, end, kind, match.group(2), "", lines[line - 1].strip()))
    for match in method_re.finditer(text):
        name = match.group(1)
        if name in _NOT_NAMES:
            continue
        line = _line_of(text, match.start(1))
        if _in_comment(lines[line - 1], text, match.start(1)):
            continue
        parent = _enclosing(found, line)
        if not parent:
            continue  # a field or constant outside an interface
        container = next((item for item in found if item.name == parent), None)
        if container is not None and container.kind != "interface":
            continue  # parcelable / struct fields are not methods
        found.append(Symbol("", line, line, "method", name, parent, lines[line - 1].strip()))
    found.sort(key=lambda item: item.line)
    return found


def _dedupe(found: list[Symbol]) -> list[Symbol]:
    seen: set[tuple[int, str]] = set()
    out = []
    for item in found:
        key = (item.line, item.name)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# --------------------------------------------------------------------------- python (ast)


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def python_analyze(text: str, tree: ast.AST | None = None) -> tuple[list[Symbol], list[tuple[int, str]]] | None:
    if tree is None:
        try:
            with warnings.catch_warnings():  # odd escapes in parsed files are not ours to report
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return None
    lines = text.split("\n")
    found: list[Symbol] = []
    calls: list[tuple[int, str]] = []

    def visit(node: ast.AST, parent: str, in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                found.append(Symbol("", child.lineno, int(getattr(child, "end_lineno", None) or child.lineno), "class",
                                    child.name, parent, lines[child.lineno - 1].strip()))
                visit(child, child.name, True)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                line = child.lineno
                kind = "method" if in_class else "function"
                found.append(Symbol("", line, int(getattr(child, "end_lineno", None) or line), kind, child.name,
                                    parent, lines[line - 1].strip()))
                visit(child, parent, False)
            else:
                if isinstance(child, ast.Call):
                    name = _call_name(child.func)
                    if name:
                        calls.append((child.lineno, name))
                visit(child, parent, in_class)

    visit(tree, "", False)
    found.sort(key=lambda item: item.line)
    return found, calls


# --------------------------------------------------------------------------- tree-sitter

TS_MODULES = {
    "python": "tree_sitter_python",
    "java": "tree_sitter_java",
    "kotlin": "tree_sitter_kotlin",
    "go": "tree_sitter_go",
    "rust": "tree_sitter_rust",
    "c": "tree_sitter_c",
    "cpp": "tree_sitter_cpp",
    "csharp": "tree_sitter_c_sharp",
    "javascript": "tree_sitter_javascript",
    "typescript": "tree_sitter_typescript",
}
#: node type -> kind of definition, per language
TS_DEFS: dict[str, dict[str, str]] = {
    "python": {"class_definition": "class", "function_definition": "function"},
    "java": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "class",
        "annotation_type_declaration": "interface",
        "method_declaration": "function",
        "constructor_declaration": "constructor",
    },
    "kotlin": {"class_declaration": "class", "object_declaration": "object", "function_declaration": "function"},
    "go": {"function_declaration": "function", "method_declaration": "method", "type_spec": "struct"},
    "rust": {
        "function_item": "function",
        "function_signature_item": "function",
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
        "impl_item": "impl",
        "union_item": "union",
    },
    "c": {"function_definition": "function", "struct_specifier": "struct", "union_specifier": "union", "enum_specifier": "enum"},
    "cpp": {
        "function_definition": "function",
        "class_specifier": "class",
        "struct_specifier": "struct",
        "union_specifier": "union",
        "enum_specifier": "enum",
    },
    "csharp": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "struct_declaration": "struct",
        "enum_declaration": "enum",
        "record_declaration": "class",
        "method_declaration": "function",
        "constructor_declaration": "constructor",
    },
    "javascript": {"class_declaration": "class", "function_declaration": "function", "method_definition": "method",
                   "generator_function_declaration": "function"},
    "typescript": {
        "class_declaration": "class",
        "abstract_class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "function_declaration": "function",
        "method_definition": "method",
        "method_signature": "method",
    },
}
#: node types that are calls (or constructions), per language
TS_CALLS: dict[str, frozenset[str]] = {
    "java": frozenset({"method_invocation", "object_creation_expression"}),
    "kotlin": frozenset({"call_expression"}),
    "go": frozenset({"call_expression"}),
    "rust": frozenset({"call_expression"}),
    "c": frozenset({"call_expression"}),
    "cpp": frozenset({"call_expression", "new_expression"}),
    "csharp": frozenset({"invocation_expression", "object_creation_expression"}),
    "javascript": frozenset({"call_expression", "new_expression"}),
    "typescript": frozenset({"call_expression", "new_expression"}),
    "python": frozenset({"call"}),
}
_IDENT_TYPES = frozenset({
    "identifier", "field_identifier", "property_identifier", "simple_identifier", "type_identifier",
    "shorthand_property_identifier", "private_property_identifier", "constant", "name",
})
_ts_cache: dict[str, object] = {}
_ts_lock = threading.Lock()


def treesitter_available(language: str = "python") -> bool:
    return _ts_language(language) is not None


def _ts_language(language: str):
    with _ts_lock:
        if language in _ts_cache:
            return _ts_cache[language]
        result = None
        module_name = TS_MODULES.get(language)
        if module_name:
            try:
                from tree_sitter import Language, Parser  # type: ignore

                module = importlib.import_module(module_name)
                raw = module.language_typescript() if language == "typescript" else module.language()
                lang = Language(raw)
                try:
                    parser = Parser(lang)
                except TypeError:  # tree-sitter < 0.22
                    parser = Parser()
                    parser.set_language(lang)
                result = (parser, lang)
            except Exception:
                result = None
        _ts_cache[language] = result
        return result


def _text(node) -> str:
    try:
        return node.text.decode("utf-8", "replace")
    except Exception:
        return ""


def _first_ident(node, depth: int = 0) -> str:
    if node is None or depth > 6:
        return ""
    if node.type in _IDENT_TYPES:
        return _text(node)
    for child in node.named_children:
        if child.type in _IDENT_TYPES:
            return _text(child)
    return ""


def _def_name(node) -> tuple[str, str]:
    """(name, explicit container) of a definition node; the container is set for ``Foo::bar``."""
    field = node.child_by_field_name("name")
    if field is None and node.type == "impl_item":
        field = node.child_by_field_name("type")
    if field is None and node.type in {"function_definition", "function_declaration"}:
        declarator = node.child_by_field_name("declarator")
        hops = 0
        while declarator is not None and hops < 8 and declarator.type not in {
            "identifier", "field_identifier", "qualified_identifier", "destructor_name", "operator_name"
        }:
            nxt = declarator.child_by_field_name("declarator")
            if nxt is None:
                break
            declarator = nxt
            hops += 1
        if declarator is not None and declarator.type == "qualified_identifier":
            text = _text(declarator)
            parent, _, short = text.rpartition("::")
            return short, parent.rpartition("::")[2]
        field = declarator
    if field is None:
        return _first_ident(node), ""
    if field.type not in _IDENT_TYPES and field.type not in {"destructor_name", "operator_name"}:
        name = _first_ident(field) or _text(field)
    else:
        name = _text(field)
    name = name.split("<", 1)[0].strip()
    return name, ""


def _callee(node) -> str:
    fn = (
        node.child_by_field_name("function")
        or node.child_by_field_name("name")
        or node.child_by_field_name("constructor")
        or node.child_by_field_name("type")
    )
    if fn is None and node.named_children:
        fn = node.named_children[0]
    hops = 0
    while fn is not None and hops < 8:
        if fn.type in _IDENT_TYPES:
            return _text(fn)
        nxt = fn.child_by_field_name("field") or fn.child_by_field_name("property") or fn.child_by_field_name("name")
        if nxt is None:
            named = fn.named_children
            if fn.type in {"generic_type", "generic_function", "template_function"} and named:
                nxt = named[0]
            else:
                nxt = named[-1] if named else None
        fn = nxt
        hops += 1
    return ""


def treesitter_analyze(text: str, language: str) -> tuple[list[Symbol], list[tuple[int, str]]] | None:
    loaded = _ts_language(language)
    if loaded is None:
        return None
    parser, _lang = loaded
    kinds = TS_DEFS.get(language, {})
    call_types = TS_CALLS.get(language, frozenset())
    try:
        tree = parser.parse(text.encode("utf-8"))
    except Exception:
        return None
    lines = text.split("\n")
    found: list[Symbol] = []
    calls: list[tuple[int, str]] = []
    # iterative walk: deep files must not hit the recursion limit
    stack: list[tuple[object, str, bool]] = [(tree.root_node, "", False)]
    while stack:
        node, parent, in_class = stack.pop()
        for child in reversed(node.children):
            kind = kinds.get(child.type)
            if child.type in call_types:
                name = _callee(child)
                if name:
                    calls.append((child.start_point[0] + 1, name))
            if kind:
                name, explicit = _def_name(child)
                if kind == "struct" and language == "go":
                    inner = child.child_by_field_name("type")
                    inner_type = inner.type if inner is not None else ""
                    kind = {"interface_type": "interface", "struct_type": "struct"}.get(inner_type, "type")
                if kind in {"struct", "union", "enum"} and child.child_by_field_name("body") is None and language in {"c", "cpp"}:
                    stack.append((child, parent, in_class))
                    continue  # a use of the type ("struct foo *p"), not its definition
                if name:
                    line = child.start_point[0] + 1
                    end = child.end_point[0] + 1
                    owner = explicit or parent
                    if kind == "function" and (in_class or explicit or (language == "go" and child.type == "method_declaration")):
                        kind = "method"
                    if language == "go" and child.type == "method_declaration":
                        receiver = child.child_by_field_name("receiver")
                        rtext = _text(receiver) if receiver is not None else ""
                        match = re.search(r"\*?\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*\)$", rtext)
                        owner = match.group(1) if match else owner
                    signature = lines[line - 1].strip() if line - 1 < len(lines) else name
                    found.append(Symbol("", line, end, kind, name, owner, signature))
                    is_container = kind in CONTAINER_KINDS
                    stack.append((child, name if is_container else parent, is_container))
                    continue
            # a function body is not a class body: nested defs there are functions
            body_of_function = child.type in {"block", "compound_statement", "function_body", "statement_block"}
            stack.append((child, parent, in_class and not body_of_function))
    found.sort(key=lambda item: item.line)
    calls.sort()
    return found, calls


# --------------------------------------------------------------------------- entry points


def analyze(text: str, language: str) -> tuple[list[Symbol], list[tuple[int, str]] | None]:
    """Definitions, and the calls ``(line, name)`` when a parser saw them (else None)."""
    if language == "python":
        result = python_analyze(text)
        if result is not None:
            return result
    if language in TS_MODULES and language != "python":
        result = treesitter_analyze(text, language)
        if result is not None and result[0]:
            return result
    return regex_symbols(text, language), None


def extract_symbols(text: str, language: str) -> list[Symbol]:
    """Definitions in ``text``. Path is left empty; the caller fills it."""
    return analyze(text, language)[0]
