# SPDX-License-Identifier: Apache-2.0
"""Per-file facts beyond definitions: imports, likely calls, inheritance, and strings.

One pass over a file's text gives a :class:`Facts`:

- ``edges``: ``(kind, src_symbol, dst_name, line)`` with kind ``imports`` (the module or
  path as written), ``calls`` (the callee's short name; a name match, so "likely") or
  ``inherits`` (a base class or implemented interface, short name).
- ``strings``: ``(line, kind, value, tag, container)``. Kinds: ``log`` (the message of a
  logging call), ``error`` (an exception or error message), ``tag`` (a log tag constant),
  ``resource`` (Android string resources and their uses), ``property`` (system property
  keys in code, ``.rc``, ``.prop``, sysprop and ``property_contexts``), ``selinux``
  (types and rules in ``.te`` and ``*_contexts``), ``literal`` (any other string).

Bug reports and logs quote these strings exactly, so they find code whatever its names are.
"""

from __future__ import annotations

import ast
import re
import warnings
from dataclasses import dataclass, field

from critique_bot.indexing import symbols as _symbols
from critique_bot.indexing.symbols import CONTAINER_KINDS, Symbol

MAX_STRING_CHARS = 240
MIN_STRING_CHARS = 3
#: Most strings kept per file (generated tables can hold tens of thousands).
MAX_STRINGS_PER_FILE = 3000
MAX_CALLS_PER_FILE = 8000


@dataclass
class Facts:
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    edges: list[tuple[str, str, str, int]] = field(default_factory=list)
    strings: list[tuple[int, str, str, str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------- lexing


def lex(text: str, language: str) -> tuple[str, list[tuple[int, str]]]:
    """Code with comments and string contents blanked (newlines kept), and the string
    literals as ``(line, value)``. Handles C-family, Kotlin, Go, Rust, JS/TS quoting."""
    out: list[str] = []
    found: list[tuple[int, str]] = []
    i = 0
    n = len(text)
    line = 1
    backtick = language in {"javascript", "typescript", "go"}
    single_is_string = language in {"javascript", "typescript"}
    hash_comment = language in {"python", "rc", "prop", "sepolicy", "cmake", "bazel", "make"}
    while i < n:
        c = text[i]
        if c == "\n":
            out.append(c)
            line += 1
            i += 1
            continue
        if hash_comment and c == "#":
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
            continue
        if not hash_comment and c == "/" and i + 1 < n and text[i + 1] == "/":
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
            continue
        if not hash_comment and c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            chunk = text[i:end]
            out.append(re.sub(r"[^\n]", " ", chunk))
            line += chunk.count("\n")
            i = end
            continue
        if c == '"' and text.startswith('"""', i) and language in {"kotlin", "java", "python"}:
            end = text.find('"""', i + 3)
            end = n if end < 0 else end + 3
            chunk = text[i:end]
            value = chunk[3:-3] if chunk.endswith('"""') else chunk[3:]
            if value.strip():
                found.append((line, value))
            out.append('"' + re.sub(r"[^\n]", " ", chunk[1:-1]) + '"' if len(chunk) >= 2 else chunk)
            line += chunk.count("\n")
            i = end
            continue
        if c == '"' or (c == "'" and single_is_string) or (c == "`" and backtick):
            quote = c
            j = i + 1
            buf: list[str] = []
            start_line = line
            while j < n:
                ch = text[j]
                if ch == "\\" and quote != "`" and j + 1 < n:
                    buf.append(text[j : j + 2])
                    j += 2
                    continue
                if ch == quote:
                    break
                if ch == "\n":
                    if quote != "`":
                        break  # unterminated: stop at the line end
                    line += 1
                buf.append(ch)
                j += 1
            value = "".join(buf)
            found.append((start_line, value))
            out.append(quote + re.sub(r"[^\n]", " ", value) + quote)
            i = j + 1 if j < n and text[j] == quote else j
            continue
        if c == "'":
            # a char literal ('x', '\n'); a Rust lifetime or a Kotlin label is left alone
            close = text.find("'", i + 1, i + 5)
            if close > 0 and "\n" not in text[i:close]:
                out.append("'" + " " * (close - i - 1) + "'")
                i = close + 1
                continue
        out.append(c)
        i += 1
    return "".join(out), found


# --------------------------------------------------------------------------- shared helpers

_LOG_CALL = re.compile(
    r"\b(?:Log|Slog|Rlog|VLog|EventLog|android\.util\.Log)\.(?:v|d|i|w|e|wtf|println)\s*\("
    r"|\bALOG[VDIWEF]\w*\s*\(|\bSLOG[VDIWEF]\w*\s*\(|\bRLOG[VDIWEF]\w*\s*\("
    r"|\bLOG\s*\(\s*\w+\s*\)|\bPLOG\s*\(\s*\w+\s*\)|\bLOG_(?:ALWAYS_FATAL|FATAL|ERROR|WARN|INFO|DEBUG)\w*\s*\("
    r"|\b(?:logger|log|logging|LOGGER|LOG|_log|_logger|Timber|Logger|console)\.(?:debug|info|warn|warning|error|critical|exception|fatal|trace|d|i|w|e|v|wtf|log)\s*\("
    r"|\b(?:println|print|printf|fprintf|puts|eprintln|println!|eprintln!|log::\w+!)\s*!?\("
    r"|\b(?:error|warn|info|debug|trace)!\s*\("
    r"|\bfmt\.(?:Print\w*|Fprint\w*)\s*\(|\blog\.(?:Print\w*|Fatal\w*|Panic\w*)\s*\("
)
_ERROR_CALL = re.compile(
    r"\bthrow\s+new\s+\w+|\bthrow\s+\w+\s*\(|\braise\s+\w+|\berrors\.New\s*\(|\bfmt\.Errorf\s*\("
    r"|\bpanic!?\s*\(|\.expect\s*\(|\bunreachable!\s*\(|\bbail!\s*\(|\banyhow!\s*\(|\bError\s*\(|\bException\s*\("
    r"|\bLOG_ALWAYS_FATAL\w*\s*\(|\bCHECK\w*\s*\(|\brequire\s*\(|\bcheck\s*\(|\berror\s*\("
)
_TAG_DEF = re.compile(
    r"\b(\w*TAG\w*)\s*(?::\s*String\s*)?=\s*\"([^\"\n]+)\"|#\s*define\s+(LOG_TAG)\s+\"([^\"\n]+)\""
)
_PROPERTY_KEY = re.compile(
    r"^(?:ro|persist|sys|vendor|debug|ctl|init|dev|log|service|net|wifi|bluetooth|car|vehicle|hw|dalvik|qemu|"
    r"camera|audio|media|gsm|telephony|odm|product|system|bootloader|selinux|security|apexd|aaudio|graphics|"
    r"external_storage|drm|keyguard|ril|setupwizard|cache_key|sendbug|apk_verity|config|nfc|tombstoned)\.[\w.\-@]+$"
)
_RESOURCE_USE = re.compile(r"\bR\.(string|plurals|array|bool|integer|dimen|color|drawable|layout|id|xml|raw)\.(\w+)")
_XML_RESOURCE_USE = re.compile(r"@(string|plurals|array|bool|integer|dimen|color|drawable|layout)/([\w.]+)")


def _innermost(symbols: list[Symbol], line: int, *, functions_only: bool = False) -> str:
    """Qualified name of the innermost definition around ``line``."""
    best: Symbol | None = None
    for item in symbols:
        if item.line <= line <= item.end_line:
            if functions_only and item.kind in CONTAINER_KINDS:
                continue
            if best is None or item.line >= best.line:
                best = item
    return best.qualified if best else ""


def _short(name: str) -> str:
    """``a.b.Base<T>`` / ``ns::Base`` / ``Base()`` -> ``Base``."""
    name = re.sub(r"<.*", "", name.strip())
    name = re.sub(r"\(.*", "", name)
    name = name.replace("::", ".").strip().strip("?")
    return name.rpartition(".")[2].strip()


def _split_bases(text: str) -> list[str]:
    parts, depth, current = [], 0, []
    for ch in text:
        if ch in "<(":
            depth += 1
        elif ch in ">)":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))
    return [item for item in (_short(part) for part in parts) if item and re.fullmatch(r"[A-Za-z_]\w*", item)]


def _header(code: str, lines: list[str], symbol: Symbol) -> str:
    """The declaration of a class from its first line to the opening brace."""
    start = symbol.line - 1
    chunk = "\n".join(lines[start : start + 6])
    brace = chunk.find("{")
    return chunk if brace < 0 else chunk[:brace]


def _bases(header: str, language: str, name: str) -> list[str]:
    head = header.split(name, 1)[1] if name in header else header
    head = re.sub(r"\s+", " ", head)
    out: list[str] = []
    if language in {"java", "typescript", "javascript", "aidl"}:
        match = re.search(r"\bextends\s+(.+?)(?=\bimplements\b|$)", head)
        if match:
            out += _split_bases(match.group(1))
        match = re.search(r"\bimplements\s+(.+)$", head)
        if match:
            out += _split_bases(match.group(1))
    elif language == "kotlin":
        # class Foo(...) : Bar(), Baz  -- the colon after the constructor parameters
        depth = 0
        for index, ch in enumerate(head):
            if ch in "(<":
                depth += 1
            elif ch in ")>":
                depth -= 1
            elif ch == ":" and depth == 0:
                rest = re.split(r"\bwhere\b", head[index + 1 :])[0]
                out += _split_bases(rest)
                break
    elif language in {"cpp", "csharp"}:
        match = re.search(r"(?<!:):(?!:)\s*(.+)$", head)
        if match:
            cleaned = re.sub(r"\b(public|protected|private|virtual|internal)\b", " ", match.group(1))
            out += _split_bases(cleaned)
    elif language == "hidl":
        match = re.search(r"\bextends\s+@?[\d.]*:{0,2}([\w.:]+)", head)
        if match:
            out.append(_short(match.group(1)))
    return [item for item in out if item != name]


# --------------------------------------------------------------------------- imports

_IMPORT_RES = {
    "java": [re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+?)(?:\.\*)?\s*;", re.M)],
    "aidl": [re.compile(r"^\s*import\s+([\w.]+)\s*;", re.M)],
    "hidl": [re.compile(r"^\s*import\s+([\w.@:]+)\s*;", re.M)],
    "kotlin": [re.compile(r"^\s*import\s+([\w.`]+?)(?:\.\*)?(?:\s+as\s+\w+)?\s*$", re.M)],
    "c": [re.compile(r"^\s*#\s*(?:include|import)\s*[<\"]([^>\"]+)[>\"]", re.M)],
    "go": [re.compile(r"^\s*import\s+(?:[\w.]+\s+)?\"([^\"]+)\"", re.M)],
    "rust": [
        re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+((?:crate|super|self|\w+)(?:::\w+)*)", re.M),
        re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;", re.M),
    ],
    "javascript": [
        re.compile(r"\bimport\s+(?:[^'\"]*?\s+from\s+)?['\"]([^'\"]+)['\"]"),
        re.compile(r"\bexport\s+[^'\";]*?\s+from\s+['\"]([^'\"]+)['\"]"),
        re.compile(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        re.compile(r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    ],
    "csharp": [re.compile(r"^\s*using\s+(?:static\s+)?([\w.]+)\s*;", re.M)],
}
_IMPORT_RES["cpp"] = _IMPORT_RES["c"]
_IMPORT_RES["typescript"] = _IMPORT_RES["javascript"]
_GO_BLOCK = re.compile(r"^\s*import\s*\((.*?)\)", re.M | re.S)


def _imports(text: str, language: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for regex in _IMPORT_RES.get(language, []):
        for match in regex.finditer(text):
            value = match.group(1).replace("`", "")
            if language == "rust" and regex.pattern.find("mod\\s+") >= 0:
                value = "mod:" + value
            out.append((value, _symbols._line_of(text, match.start())))
    if language == "go":
        for block in _GO_BLOCK.finditer(text):
            base = _symbols._line_of(text, block.start(1))
            for offset, row in enumerate(block.group(1).split("\n")):
                found = re.search(r"\"([^\"]+)\"", row)
                if found:
                    out.append((found.group(1), base + offset))
    return out


# --------------------------------------------------------------------------- calls (regex)

_CALL_RE = re.compile(r"(?<![\w$])([A-Za-z_$][\w$]*)\s*(?:<[\w<>, ?.]*>)?\s*\(")
_NOT_CALLS = frozenset(
    """if for while switch catch return sizeof new else do when try synchronized assert typeof
    function fun func fn def class struct enum interface defined alignof decltype static_assert
    super this self throw throws elif not and or in is lambda print yield await async match
    case with except int long short char float double bool void unsigned signed""".split()
)


def _regex_calls(code: str, symbols: list[Symbol]) -> list[tuple[int, str]]:
    defs = {(item.line, item.name) for item in symbols}
    out: list[tuple[int, str]] = []
    for number, row in enumerate(code.split("\n"), start=1):
        stripped = row.lstrip()
        if stripped.startswith(("#include", "#define", "import ", "package ")):
            continue
        for match in _CALL_RE.finditer(row):
            name = match.group(1)
            if name in _NOT_CALLS or (number, name) in defs:
                continue
            out.append((number, name))
            if len(out) >= MAX_CALLS_PER_FILE:
                return out
    return out


# --------------------------------------------------------------------------- strings


def _code_strings(
    text: str, code: str, literals: list[tuple[int, str]], symbols: list[Symbol], language: str
) -> list[tuple[int, str, str, str, str]]:
    lines = text.split("\n")
    code_lines = code.split("\n")
    tags: dict[str, str] = {}
    out: list[tuple[int, str, str, str, str]] = []
    for match in _TAG_DEF.finditer(text):
        name = match.group(1) or match.group(3)
        value = match.group(2) or match.group(4)
        tags[name] = value
        line = _symbols._line_of(text, match.start())
        out.append((line, "tag", value, value, _innermost(symbols, line)))
    tag_lines = {item[0] for item in out}
    import_lines = {line for _value, line in _imports(text, language)}
    default_tag = tags.get("LOG_TAG") or tags.get("TAG") or (next(iter(tags.values())) if len(tags) == 1 else "")
    for line, value in literals:
        if len(value) < MIN_STRING_CHARS or not value.strip() or line in tag_lines or line in import_lines:
            continue
        row = code_lines[line - 1] if line - 1 < len(code_lines) else ""
        raw_row = lines[line - 1] if line - 1 < len(lines) else ""
        if raw_row.lstrip().startswith(("#include", "import ", "package ", "#import")):
            continue
        value = value[:MAX_STRING_CHARS]
        container = _innermost(symbols, line, functions_only=True) or _innermost(symbols, line)
        kind, tag = "literal", ""
        log_match = _LOG_CALL.search(row)
        if _PROPERTY_KEY.match(value):
            kind, tag = "property", value
        elif log_match:
            kind = "log"
            args = row[log_match.end() :]
            first = re.match(r"\s*([A-Za-z_][\w.]*)\s*,", args)
            java_style = re.match(r"(?:android\.util\.)?[A-Z]\w*Log\.|Log\.", log_match.group(0)) is not None
            if first:
                tag = tags.get(first.group(1).rpartition(".")[2], first.group(1))
            elif java_style:
                open_at = raw_row.find("(", log_match.start()) + 1
                first_literal = re.match(r"\s*\"((?:[^\"\\]|\\.)*)\"\s*,", raw_row[open_at:])
                if first_literal is not None:
                    if first_literal.group(1) == value:
                        out.append((line, "tag", value, value, container))  # Log.e("Tag", "message")
                        continue
                    tag = first_literal.group(1)
            if not tag:
                tag = default_tag
        elif _ERROR_CALL.search(row):
            kind = "error"
        out.append((line, kind, value, tag, container))
        if len(out) >= MAX_STRINGS_PER_FILE:
            break
    for number, row in enumerate(code_lines, start=1):
        for match in _RESOURCE_USE.finditer(row):
            out.append((number, "resource", f"R.{match.group(1)}.{match.group(2)}", match.group(2),
                        _innermost(symbols, number, functions_only=True)))
    return out


def _python_facts(text: str, tree: ast.AST, symbols: list[Symbol]) -> tuple[list, list]:
    edges: list[tuple[str, str, str, int]] = []
    strings: list[tuple[int, str, str, str, str]] = []
    lines = text.split("\n")
    by_line_class = {(item.line, item.name): item for item in symbols if item.kind == "class"}
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                edges.append(("imports", "", alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            base = "." * (node.level or 0) + (node.module or "")
            for alias in node.names:
                if alias.name == "*":
                    edges.append(("imports", "", base, node.lineno))
                else:
                    joiner = "" if base.endswith(".") or not base else "."
                    edges.append(("imports", "", f"{base}{joiner}{alias.name}", node.lineno))
        elif isinstance(node, ast.ClassDef):
            item = by_line_class.get((node.lineno, node.name))
            qualified = item.qualified if item else node.name
            for base in node.bases:
                name = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else ""
                if name and name != "object":
                    edges.append(("inherits", qualified, name, node.lineno))
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                docstrings.add(id(body[0].value))
    tags: dict[str, str] = {}
    for match in _TAG_DEF.finditer(text):
        tags[match.group(1) or match.group(3)] = match.group(2) or match.group(4)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in docstrings:
            continue
        value = node.value
        if len(value) < MIN_STRING_CHARS or not value.strip():
            continue
        line = node.lineno
        row = lines[line - 1] if line - 1 < len(lines) else ""
        container = _innermost(symbols, line, functions_only=True) or _innermost(symbols, line)
        kind, tag = "literal", ""
        tag_def = _TAG_DEF.search(row)
        if tag_def and (tag_def.group(2) or tag_def.group(4)) == value:
            kind, tag = "tag", value
        elif _PROPERTY_KEY.match(value):
            kind, tag = "property", value
        elif _LOG_CALL.search(row):
            kind = "log"
        elif _ERROR_CALL.search(row):
            kind = "error"
        strings.append((line, kind, value[:MAX_STRING_CHARS], tag, container))
        if len(strings) >= MAX_STRINGS_PER_FILE:
            break
    return edges, strings


# --------------------------------------------------------------------------- config formats

_XML_STRING = re.compile(r"<(string|plurals|string-array|item)\s+[^>]*?name=\"([^\"]+)\"[^>]*>(.*?)</\1>", re.S)
_XML_ANDROID_NAME = re.compile(r"android:name=\"([^\"]+)\"")
_RC_SERVICE = re.compile(r"^\s*service\s+(\S+)\s+(\S+)", re.M)
_RC_SETPROP = re.compile(r"^\s*setprop\s+(\S+)\s+(.*)$", re.M)
_RC_ON_PROP = re.compile(r"property:([\w.\-@]+)=(\S*)")
_PROP_LINE = re.compile(r"^\s*([\w.\-@]+)\s*=\s*(.*)$", re.M)
_SYSPROP = re.compile(r"\b(prop_name|api_name)\s*:\s*\"([^\"]+)\"")
_TE_TYPE = re.compile(r"^\s*(type|typeattribute|attribute)\s+(\w+)", re.M)
_TE_RULE = re.compile(r"^\s*(allow|neverallow|dontaudit|auditallow|allowxperm)\s+([^;]+);", re.M)
_TE_MACRO = re.compile(r"^\s*(\w+)\(\s*(\w+)\s*(?:,\s*([^)]*))?\)", re.M)
_CONTEXT_LINE = re.compile(r"^\s*(\S+)\s+(?:(?:--|-[a-z])\s+)?(u:object_r:(\w+):s0\S*)", re.M)


def _config_strings(text: str, language: str, rel: str) -> list[tuple[int, str, str, str, str]]:
    out: list[tuple[int, str, str, str, str]] = []
    line_of = _symbols._line_of
    if language == "xml":
        for match in _XML_STRING.finditer(text):
            value = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(3))).strip().strip('"')
            out.append((line_of(text, match.start()), "resource", value[:MAX_STRING_CHARS], match.group(2), ""))
        for match in _XML_ANDROID_NAME.finditer(text):
            out.append((line_of(text, match.start()), "literal", match.group(1), "", ""))
        for match in _XML_RESOURCE_USE.finditer(text):
            out.append((line_of(text, match.start()), "resource", f"@{match.group(1)}/{match.group(2)}", match.group(2), ""))
    elif language == "rc":
        for match in _RC_SERVICE.finditer(text):
            out.append((line_of(text, match.start()), "literal", f"service {match.group(1)} {match.group(2)}", match.group(1), ""))
        for match in _RC_SETPROP.finditer(text):
            out.append((line_of(text, match.start()), "property", match.group(2).strip()[:MAX_STRING_CHARS], match.group(1), ""))
        for match in _RC_ON_PROP.finditer(text):
            out.append((line_of(text, match.start()), "property", match.group(2), match.group(1), ""))
    elif language == "prop":
        for match in _PROP_LINE.finditer(text):
            if match.group(1).startswith("#"):
                continue
            out.append((line_of(text, match.start()), "property", match.group(2).strip()[:MAX_STRING_CHARS], match.group(1), ""))
    elif language == "sysprop":
        for match in _SYSPROP.finditer(text):
            kind = "property" if match.group(1) == "prop_name" else "literal"
            out.append((line_of(text, match.start()), kind, match.group(2), match.group(2), ""))
    elif language == "sepolicy":
        name = rel.rsplit("/", 1)[-1]
        if name.endswith("_contexts"):
            for match in _CONTEXT_LINE.finditer(text):
                key, context, type_name = match.group(1), match.group(2), match.group(3)
                kind = "property" if name == "property_contexts" else "selinux"
                out.append((line_of(text, match.start()), kind, f"{key} {context}"[:MAX_STRING_CHARS],
                            key if kind == "property" else type_name, ""))
        else:
            for match in _TE_TYPE.finditer(text):
                out.append((line_of(text, match.start()), "selinux", match.group(0).strip()[:MAX_STRING_CHARS], match.group(2), ""))
            for match in _TE_RULE.finditer(text):
                rule = re.sub(r"\s+", " ", match.group(0).strip())
                source = match.group(2).split()[0] if match.group(2).split() else ""
                out.append((line_of(text, match.start()), "selinux", rule[:MAX_STRING_CHARS], source, ""))
            for match in _TE_MACRO.finditer(text):
                out.append((line_of(text, match.start()), "selinux", match.group(0).strip()[:MAX_STRING_CHARS], match.group(2), ""))
    return out[:MAX_STRINGS_PER_FILE]


# --------------------------------------------------------------------------- entry point


def parse_text(text: str, language: str, rel: str = "") -> Facts:
    """Everything the index stores for one file."""
    facts = Facts(language)
    if language == "python":
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            tree = None
        if tree is not None:
            analyzed = _symbols.python_analyze(text, tree)
            symbols, calls = analyzed if analyzed is not None else ([], [])
            facts.symbols = symbols
            edges, strings = _python_facts(text, tree, symbols)
            facts.edges.extend(edges)
            facts.strings.extend(strings)
            _add_calls(facts, calls, symbols)
            return facts
        facts.symbols = _symbols.regex_symbols(text, "python")
        return facts
    if language in _symbols.TS_MODULES or language in {"aidl", "hidl"}:
        symbols, calls = _symbols.analyze(text, language)
        facts.symbols = symbols
        code, literals = lex(text, language)
        if calls is None and language not in {"aidl", "hidl"}:
            calls = _regex_calls(code, symbols)
        _add_calls(facts, calls or [], symbols)
        for value, line in _imports(text, language):
            facts.edges.append(("imports", "", value, line))
        lines = text.split("\n")
        code_lines = code.split("\n")
        for item in symbols:
            if item.kind in CONTAINER_KINDS - {"impl"} or (language == "rust" and item.kind == "impl"):
                if language == "rust" and item.kind == "impl":
                    match = re.match(r"\s*impl(?:<[^>]*>)?\s+([\w:]+)(?:<[^>]*>)?\s+for\s+([\w:]+)", lines[item.line - 1])
                    if match:
                        facts.edges.append(("inherits", _short(match.group(2)), _short(match.group(1)), item.line))
                    continue
                for base in _bases(_header(code, code_lines, item), language, item.name):
                    facts.edges.append(("inherits", item.qualified, base, item.line))
        if language not in {"aidl", "hidl"}:
            facts.strings = _code_strings(text, code, literals, symbols, language)
        return facts
    if language in {"xml", "rc", "prop", "sysprop", "sepolicy"}:
        facts.strings = _config_strings(text, language, rel)
    return facts


def _add_calls(facts: Facts, calls: list[tuple[int, str]], symbols: list[Symbol]) -> None:
    functions = sorted((item for item in symbols if item.kind not in CONTAINER_KINDS), key=lambda item: item.line)
    seen: set[tuple[str, str]] = set()
    for line, name in calls[:MAX_CALLS_PER_FILE]:
        owner = ""
        for item in functions:
            if item.line <= line <= item.end_line:
                owner = item.qualified  # sorted by start: the last match is the innermost
            elif item.line > line:
                break
        key = (owner, name)
        if key in seen:
            continue  # one edge per caller/callee pair (the first line)
        seen.add(key)
        facts.edges.append(("calls", owner, name, line))
