#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Copy a code tree (or an eval task) with meaningless identifier names, keeping behavior.

The bad-naming test set (plan 8, work item 11): every class, function, method, field,
parameter and local that the project itself defines gets a name like ``Mgr3``, ``doIt7`` or
``a12``. Strings are kept (log messages, keys and UI text stay exact), file and package names
are kept (except Java files, which must match their public class), and names the code takes
from outside (the standard library, Android, imported modules) are never touched. Comments
are kept, or blanked with ``--strip-comments`` (line numbers stay the same, so stack traces
in a task still point at the right lines; Python docstrings count as comments).

Languages: Python (tokenize), Java, Kotlin, C, C++, Go, Rust, JavaScript, TypeScript.

Usage::

    obfuscate.py --tree SRC DEST [--strip-comments]
    obfuscate.py --task tests/eval_tasks/loc-py-thermostat-unit DEST_TASK [--strip-comments]
    obfuscate.py --suite tests/eval_tasks --tag localization [--check]
        writes <task>-obf and <task>-obf-nc next to every task with a "fault" in task.json

``--check`` compiles (or, for Python, runs the tests of) the original and the copy and
fails when they differ.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import keyword
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from critique_bot.indexing import facts  # noqa: E402
from critique_bot.indexing.langs import language_of  # noqa: E402

CODE = {"python", "java", "kotlin", "c", "cpp", "go", "rust", "javascript", "typescript"}
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

KEYWORDS = set(keyword.kwlist) | set(getattr(keyword, "softkwlist", [])) | set(
    """
    abstract assert boolean break byte case catch char class const continue default do double else enum
    extends final finally float for goto if implements import instanceof int interface long native new
    package private protected public return short static strictfp super switch synchronized this throw
    throws transient try void volatile while true false null var record yield sealed permits non
    fun val object companion data inner open override lateinit internal suspend inline reified crossinline
    noinline vararg when is as in out by init constructor get set field it typealias operator infix tailrec
    external annotation expect actual const
    auto bool signed unsigned struct union typedef sizeof extern register restrict inline nullptr namespace
    using template typename virtual explicit friend mutable operator public private protected delete new
    constexpr noexcept decltype static_cast dynamic_cast reinterpret_cast const_cast std size_t uint8_t
    int8_t int16_t uint16_t int32_t uint32_t int64_t uint64_t float double char wchar_t string vector map
    func go defer chan select range type fallthrough package import interface struct map make append len
    cap copy panic recover new nil iota error string rune byte uint int64 float64 bool true false
    fn let mut impl trait pub crate mod use self Self match loop where dyn ref move unsafe as async await
    Some None Ok Err Box Vec String Option Result usize isize u8 u16 u32 u64 i8 i16 i32 i64 f32 f64 str
    function var let const typeof instanceof undefined null this new class extends super export default
    import from require module exports async await yield of in delete void debugger arguments
    console Math Date JSON Object Array Map Set Promise Error Number Boolean Symbol RegExp process
    """.split()
)
#: Method and field names so common on library types that a project method of the same name
#: cannot be renamed safely by tokens (cache.get vs map.get).
COMMON_API = set(
    """
    get set put add has remove clear size length push pop map filter reduce forEach keys values items entries
    append extend update read write close open run start stop join split parse format trim replace strip lower
    upper toString equals hashCode compareTo iterator next hasNext apply call then catch resolve reject
    name value id type data key len count index find contains isEmpty first last sort sorted reverse copy
    print println printf log info warn error debug main init setUp tearDown setUpClass tearDownClass
    toUpperCase toLowerCase startsWith endsWith substring charAt indexOf trimEnd repeat chars lines collect
    iter unwrap expect unwrap_or is_empty to_string as_str starts_with ends_with push_str filter_map
    getOrDefault getOrNull firstOrNull coerceIn also let run apply with takeIf use invoke
    Println Printf Sprintf Errorf Fprintln Base Join MkdirAll WriteFile Split
    """.split()
)


# --------------------------------------------------------------------------- segmenting


def segments(text: str, language: str) -> list[tuple[str, str]]:
    """(kind, text) pieces: code, string, comment; concatenated they give ``text`` back."""
    out: list[tuple[str, str]] = []
    i, n = 0, len(text)
    start = 0
    backtick = language in {"javascript", "typescript", "go"}
    single_string = language in {"javascript", "typescript"}

    def flush(upto: int) -> None:
        if upto > start:
            out.append(("code", text[start:upto]))

    while i < n:
        c = text[i]
        if c == "/" and text.startswith("//", i):
            flush(i)
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(("comment", text[i:end]))
            i = start = end
            continue
        if c == "/" and text.startswith("/*", i):
            flush(i)
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append(("comment", text[i:end]))
            i = start = end
            continue
        if c == '"' and text.startswith('"""', i) and language in {"kotlin", "java"}:
            flush(i)
            end = text.find('"""', i + 3)
            end = n if end < 0 else end + 3
            out.append(("string", text[i:end]))
            i = start = end
            continue
        if language == "rust" and c == "r" and re.match(r'r#*"', text[i:]) and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            hashes = re.match(r"r(#*)\"", text[i:]).group(1)
            flush(i)
            close = '"' + hashes
            end = text.find(close, i + 2 + len(hashes))
            end = n if end < 0 else end + len(close)
            out.append(("string", text[i:end]))
            i = start = end
            continue
        if c == '"' or (c == "'" and single_string) or (c == "`" and backtick):
            flush(i)
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == "\\" and c != "`":
                    j += 1
                elif text[j] == "\n" and c != "`":
                    break
                j += 1
            end = min(n, j + 1)
            out.append(("string", text[i:end]))
            i = start = end
            continue
        if c == "'" and language not in {"rust"}:
            close = text.find("'", i + 1, i + 5)
            if close > 0 and "\n" not in text[i:close]:
                flush(i)
                out.append(("string", text[i : close + 1]))
                i = start = close + 1
                continue
        if c == "'" and language == "rust":
            match = re.match(r"'(?:\\.|[^\\'\n])'", text[i:])
            if match:
                flush(i)
                out.append(("string", match.group(0)))
                i = start = i + len(match.group(0))
                continue
        i += 1
    flush(n)
    return out


# --------------------------------------------------------------------------- collecting names

_DECL = {
    "java": [r"\b(?:final\s+)?(?:[A-Z]\w*(?:<[^>;=()]*>)?(?:\[\])*|int|long|short|byte|char|float|double|boolean|var)\s+([a-zA-Z_]\w*)\s*(?=[=;,):])"],
    "kotlin": [r"\b(?:val|var)\s+([a-zA-Z_]\w*)", r"[(,]\s*(?:private\s+|override\s+)?(?:val\s+|var\s+)?([a-z_]\w*)\s*:\s*[A-Z(]",
               r"\{\s*([a-z_]\w*)\s*->"],
    "c": [r"\b(?:int|long|short|char|float|double|unsigned|size_t|bool|struct\s+\w+|const\s+\w+)\s*\**\s*([a-zA-Z_]\w*)\s*(?=[=;,)\[])"],
    "cpp": [r"\b(?:int|long|short|char|float|double|unsigned|size_t|bool|auto|std::\w+(?:<[^;()]*>)?|[A-Z]\w*(?:<[^;()]*>)?)\s*[*&]*\s*([a-zA-Z_]\w*)\s*(?=[=;,)\[{(])"],
    "go": [r"\b([a-zA-Z_]\w*)\s*(?:,\s*[a-zA-Z_]\w*\s*)*:=", r"\bvar\s+([a-zA-Z_]\w*)", r"[(,]\s*([a-zA-Z_]\w*)\s+(?:\*|\[\]|map\[|[a-zA-Z])",
           r"^\s*([A-Z]\w*)\s+[a-zA-Z*\[]", r",\s*([a-zA-Z_]\w*)\s*:="],
    "rust": [r"\blet\s+(?:mut\s+)?([a-zA-Z_]\w*)", r"[(,]\s*(?:mut\s+)?([a-z_]\w*)\s*:\s*[&A-Za-z\[(]", r"\|\s*([a-z_]\w*)\s*[|,]",
             r"\(\s*([a-z_]\w*)\s*,\s*([a-z_]\w*)\s*\)\|", r"^\s*(?:pub\s+)?([a-z_]\w*)\s*:\s*[A-Z&]"],
    "javascript": [r"\b(?:const|let|var)\s+([a-zA-Z_$][\w$]*)", r"\b(?:const|let|var)\s*\{([^}]*)\}", r"\(([^()]*)\)\s*(?:=>|\{)",
                   r"\bthis\.([a-zA-Z_$][\w$]*)\s*=", r"\{\s*([a-zA-Z_$][\w$]*)\s*,\s*([a-zA-Z_$][\w$]*)\s*:"],
}
_DECL["typescript"] = _DECL["javascript"]


def _python_names(text: str) -> set[str]:
    names: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
                names.add(arg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) and isinstance(node.value, ast.Name) \
                and node.value.id == "self":
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


def _python_external(text: str, project_modules: set[str]) -> set[str]:
    out: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            internal = node.level > 0 or (node.module or "").split(".")[0] in project_modules
            for alias in node.names:
                if not internal:
                    out.add(alias.asname or alias.name)
            if node.module:
                out.update(part for part in node.module.split("."))
    import builtins

    out.update(dir(builtins))
    return out


def _imported_names(text: str, language: str, project_names: set[str]) -> set[str]:
    """Names bound by imports from outside the project (and package / module names)."""
    out: set[str] = set()
    for value, _line in facts._imports(text, language):
        parts = re.split(r"[.:/@]+", value)
        out.update(part for part in parts if part)
    for match in re.finditer(r"^\s*package\s+([\w.]+)", text, re.M):
        out.update(match.group(1).split("."))
    if language in {"javascript", "typescript"}:
        for match in re.finditer(r"\b(?:const|let|var)\s+\{?([^=}]*)\}?\s*=\s*require\(\s*['\"]([^'\"]+)", text):
            if not match.group(2).startswith("."):
                out.update(IDENT.findall(match.group(1)))
        for match in re.finditer(r"\bimport\s+(.+?)\s+from\s+['\"]([^'\"]+)", text):
            if not match.group(2).startswith("."):
                out.update(IDENT.findall(match.group(1)))
    if language in {"java", "kotlin"}:
        for match in re.finditer(r"^\s*import\s+(?:static\s+)?([\w.]+)", text, re.M):
            last = match.group(1).rsplit(".", 1)[-1]
            if last not in project_names:
                out.add(last)
    if language == "rust":
        for match in re.finditer(r"^\s*(?:pub\s+)?mod\s+(\w+)", text, re.M):
            out.add(match.group(1))
    return out


def _protected_by_code(text: str, language: str) -> set[str]:
    """Overrides of library methods, test names, macros: kept as they are."""
    out: set[str] = set()
    if language == "java":
        for match in re.finditer(r"@Override\s+(?:public\s+|protected\s+)?(?:[\w<>\[\]]+\s+)+(\w+)\s*\(", text):
            out.add(match.group(1))
    if language == "kotlin":
        for match in re.finditer(r"\boverride\s+(?:fun|val|var)\s+(\w+)", text):
            out.add(match.group(1))
    if language in {"c", "cpp"}:
        for match in re.finditer(r"^\s*#\s*define\s+(\w+)", text, re.M):
            out.add(match.group(1))
    if language == "rust":
        for match in re.finditer(r"\bimpl\s+(\w+)\s+for\b", text):
            out.add(match.group(1))  # library traits (Display, Iterator) keep their methods
    return out


def collect(files: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """The rename map for a tree of ``{rel: text}`` and each name's kind (type/func/var)."""
    defined: dict[str, str] = {}
    protected: set[str] = set(KEYWORDS) | COMMON_API
    project_modules = {Path(rel).parts[0] for rel in files if rel.endswith(".py")}
    project_modules |= {Path(rel).stem for rel in files if rel.endswith(".py")}
    type_names: set[str] = set()
    for rel, text in files.items():
        language = language_of(rel)
        parsed = facts.parse_text(text, language, rel)
        for symbol in parsed.symbols:
            kind = "type" if symbol.kind in {"class", "interface", "enum", "struct", "trait", "object", "union", "type"} else "func"
            if symbol.kind == "impl":
                continue
            defined.setdefault(symbol.name, kind)
            if kind == "type":
                type_names.add(symbol.name)
    for rel, text in files.items():
        language = language_of(rel)
        if language == "python":
            for name in _python_names(text):
                defined.setdefault(name, "var")
            protected |= _python_external(text, project_modules)
            protected |= project_modules
        else:
            for pattern in _DECL.get(language, []):
                for match in re.finditer(pattern, text, re.M):
                    for group in match.groups():
                        if not group:
                            continue
                        for name in IDENT.findall(group):
                            defined.setdefault(name, "var")
            protected |= _imported_names(text, language, set(defined))
        protected |= _protected_by_code(text, language)
    # attributes of outside names stay too: os.path, json.load, filepath.Base, Math.round
    for _round in range(3):
        for text in files.values():
            for match in re.finditer(r"\b([A-Za-z_]\w*)\s*(?:\.|::)\s*([A-Za-z_]\w*)", text):
                if match.group(1) in protected and match.group(1) not in defined:
                    protected.add(match.group(2))
    if any(language_of(rel) == "go" for rel in files):
        protected |= {Path(rel).parent.name for rel in files}
    for rel in files:
        if language_of(rel) == "rust":
            protected.add(Path(rel).stem)
    names = sorted(
        name for name in defined
        if name not in protected
        and not name.startswith(("test", "Test", "__"))
        and not re.fullmatch(r"[A-Z][A-Z0-9_]+", name)  # constants and enum entries keep their names
        and len(name) > 1
        and name not in {"self", "cls", "this", "it", "main", "args", "kwargs", "_"}
    )
    taken = set()
    for text in files.values():
        taken.update(IDENT.findall(text))
    counters = {"type": 0, "func": 0, "var": 0}
    stems = {"type": "Mgr", "func": "doIt", "var": "a"}
    mapping: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for name in names:
        kind = defined[name]
        while True:
            counters[kind] += 1
            stem = stems[kind]
            fresh = f"{stem}{counters[kind]}"
            fresh = fresh[0].upper() + fresh[1:] if name[0].isupper() else fresh[0].lower() + fresh[1:]
            if fresh not in taken and fresh not in mapping.values():
                break
        mapping[name] = fresh
        kinds[name] = kind
    return mapping, kinds


# --------------------------------------------------------------------------- rewriting


def _rename_code(code: str, mapping: dict[str, str]) -> str:
    return IDENT.sub(lambda m: mapping.get(m.group(0), m.group(0)), code)


def _rename_template(string: str, language: str, mapping: dict[str, str]) -> str:
    """Rename inside string templates: Kotlin $name / ${expr}, JS/TS ${expr}."""
    if language == "kotlin":
        string = re.sub(r"\$\{([^}]*)\}", lambda m: "${" + _rename_code(m.group(1), mapping) + "}", string)
        return re.sub(r"\$([A-Za-z_]\w*)", lambda m: "$" + mapping.get(m.group(1), m.group(1)), string)
    if language in {"javascript", "typescript"} and string.startswith("`"):
        return re.sub(r"\$\{([^}]*)\}", lambda m: "${" + _rename_code(m.group(1), mapping) + "}", string)
    return string


def _blank(text: str) -> str:
    return re.sub(r"[^\n]", "", text) if "\n" in text else ""


def rewrite(text: str, language: str, mapping: dict[str, str], *, strip_comments: bool) -> str:
    if language == "python":
        return _rewrite_python(text, mapping, strip_comments=strip_comments)
    out: list[str] = []
    for kind, piece in segments(text, language):
        if kind == "code":
            out.append(_rename_code(piece, mapping))
        elif kind == "string":
            out.append(_rename_template(piece, language, mapping))
        elif strip_comments:
            out.append(_blank(piece))
        else:
            out.append(piece)
    return "".join(out)


def _rewrite_python(text: str, mapping: dict[str, str], *, strip_comments: bool) -> str:
    lines = text.splitlines(keepends=True)
    edits: list[tuple[int, int, int, int, str]] = []  # (row, col, end_row, end_col, new)
    docstrings: set[tuple[int, int]] = set()
    if strip_comments:
        try:
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                    first = node.body[0]
                    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                        docstrings.add((first.lineno, first.col_offset))
        except SyntaxError:
            pass
    previous = None
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.NAME and token.string in mapping:
            # attribute access on other objects (obj.name) is renamed too: the name is the
            # project's own (library names are protected), so every use must change together
            edits.append((*token.start, *token.end, mapping[token.string]))
        elif token.type == tokenize.COMMENT and strip_comments:
            edits.append((*token.start, *token.end, ""))
        elif token.type == tokenize.STRING and token.start in docstrings:
            body = token.string
            newlines = body.count("\n")
            edits.append((*token.start, *token.end, '""' + "" if not newlines else '"""' + "\n" * newlines + '"""'))
        previous = token
    del previous
    for row, col, end_row, end_col, new in sorted(edits, reverse=True):
        if row == end_row:
            line = lines[row - 1]
            lines[row - 1] = line[:col] + new + line[end_col:]
        else:
            head = lines[row - 1][:col]
            tail = lines[end_row - 1][end_col:]
            lines[row - 1 : end_row] = [head + new + tail]
    return "".join(lines)


def obfuscate_tree(src: Path, dest: Path, *, strip_comments: bool = False) -> dict:
    """Copy ``src`` to ``dest`` with renamed identifiers. Returns the mapping and file renames."""
    src, dest = Path(src), Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    code_files = {
        path.relative_to(dest).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(dest.rglob("*")) if path.is_file() and language_of(path.name) in CODE
    }
    mapping, _kinds = collect(code_files)
    renamed_files: dict[str, str] = {}
    for rel, text in code_files.items():
        language = language_of(rel)
        new_text = rewrite(text, language, mapping, strip_comments=strip_comments)
        path = dest / rel
        path.write_text(new_text, encoding="utf-8")
        if language == "java" and path.stem in mapping:
            target = path.with_name(mapping[path.stem] + path.suffix)
            path.rename(target)
            renamed_files[rel] = target.relative_to(dest).as_posix()
    return {"mapping": mapping, "files": renamed_files, "strip_comments": strip_comments}


# --------------------------------------------------------------------------- tasks


def _code_tokens(text: str, mapping: dict[str, str]) -> str:
    """Rename identifiers that look like code (CamelCase or snake_case) in a prompt."""
    def swap(match: re.Match[str]) -> str:
        word = match.group(0)
        if word in mapping and (re.search(r"[a-z][A-Z]|_|^[A-Z][a-z]+[A-Z]", word)):
            return mapping[word]
        return word

    return IDENT.sub(swap, text)


def obfuscate_task(task_dir: Path, dest_dir: Path, *, strip_comments: bool) -> dict:
    spec = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    fixture = spec.get("fixture", "files")
    info = obfuscate_tree(task_dir / fixture, dest_dir / fixture, strip_comments=strip_comments)
    mapping = info["mapping"]
    fault = spec["fault"]
    new_file = info["files"].get(fault["file"], fault["file"])
    new_function = mapping.get(fault["function"], fault["function"])
    variant = "obfuscated names, comments stripped" if strip_comments else "obfuscated names"
    spec["prompt"] = _code_tokens(spec["prompt"], mapping)
    spec["answer"] = f"(?=.*{re.escape(Path(new_file).name)})(?=.*\\b{re.escape(new_function)}\\b)"
    spec["reference_answer"] = f"{new_file}, {new_function}: the fault of {task_dir.name} ({variant})."
    spec["fault"] = {"file": new_file, "function": new_function, "original": fault}
    spec["tags"] = sorted(set(spec.get("tags", [])) | {"obfuscated"} | ({"no-comments"} if strip_comments else set()))
    spec["source"] = f"generated by scripts/eval/obfuscate.py from {task_dir.name}"
    (dest_dir / "task.json").write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return info


# --------------------------------------------------------------------------- checks


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=300, check=False)
    return proc.returncode, (proc.stdout + proc.stderr)[-1500:]


def check_tree(path: Path) -> list[str]:
    """Problems compiling (or testing) the tree with the tools on PATH; skipped when missing."""
    problems: list[str] = []
    path = Path(path).resolve()
    files = [p for p in path.rglob("*") if p.is_file()]
    langs = {language_of(p.name) for p in files}
    with tempfile.TemporaryDirectory(prefix="obf-check-") as tmp:
        if "python" in langs:
            code, out = _run([sys.executable, "-m", "compileall", "-q", "."], path)
            if code:
                problems.append("python compile: " + out)
            if (path / "tests").is_dir():
                code, out = _run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], path)
                if code:
                    problems.append("python tests: " + out)
            for cache in path.rglob("__pycache__"):
                shutil.rmtree(cache, ignore_errors=True)
        if "java" in langs and shutil.which("javac"):
            stubs = Path(tmp) / "stubs"
            _android_stubs(stubs)
            sources = [str(p) for p in files if p.suffix == ".java"] + [str(p) for p in stubs.rglob("*.java")]
            (Path(tmp) / "classes").mkdir()
            code, out = _run(["javac", "-d", str(Path(tmp) / "classes"), *sources], path)
            if code:
                problems.append("javac: " + out)
        if "go" in langs and shutil.which("go") and (path / "go.mod").is_file():
            code, out = _run(["go", "vet", "./..."], path)
            if code:
                problems.append("go vet: " + out)
        if any(p.suffix == ".c" for p in files) and shutil.which("gcc"):
            code, out = _run(["gcc", "-fsyntax-only", *[str(p) for p in files if p.suffix == ".c"]], path)
            if code:
                problems.append("gcc: " + out)
        if "cpp" in langs and shutil.which("g++"):
            code, out = _run(["g++", "-std=c++17", "-fsyntax-only", *[str(p) for p in files if p.suffix == ".cpp"]], path)
            if code:
                problems.append("g++: " + out)
        if "javascript" in langs and shutil.which("node"):
            for p in files:
                if p.suffix == ".js":
                    code, out = _run(["node", "--check", str(p)], path)
                    if code:
                        problems.append(f"node {p.name}: {out}")
        if "rust" in langs and shutil.which("cargo") and (path / "Cargo.toml").is_file():
            code, out = _run(["cargo", "check", "-q", "--offline", "--target-dir", str(Path(tmp) / "target")], path)
            if code:
                problems.append("cargo: " + out)
            (path / "Cargo.lock").unlink(missing_ok=True)
    return problems


def _android_stubs(folder: Path) -> None:
    """Just enough of android.* for javac to check the AOSP-style fixtures."""
    stubs = {
        "android/util/Log.java": "package android.util; public class Log { public static int i(String t, String m) { return 0; } "
                                 "public static int d(String t, String m) { return 0; } public static int w(String t, String m) { return 0; } "
                                 "public static int e(String t, String m) { return 0; } }",
        "android/util/Slog.java": "package android.util; public class Slog { public static int i(String t, String m) { return 0; } "
                                  "public static int d(String t, String m) { return 0; } public static int w(String t, String m) { return 0; } }",
        "android/util/SparseArray.java": "package android.util; public class SparseArray<E> { public E get(int k) { return null; } "
                                         "public void put(int k, E v) {} }",
        "android/os/SystemProperties.java": "package android.os; public class SystemProperties { public static String get(String k, String d) { return d; } "
                                            "public static boolean getBoolean(String k, boolean d) { return d; } }",
    }
    for rel, text in stubs.items():
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


#: Python's tokenizer sees names inside f-strings only from 3.12 (PEP 701); before that an
#: f-string is one STRING token, so its names would stay unrenamed and the generated tasks
#: would differ by Python version.
MIN_PYTHON = (3, 12)


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        print("obfuscate.py needs Python 3.12 or newer (names inside f-strings); run it with python3.12",
              file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tree", nargs=2, metavar=("SRC", "DEST"))
    parser.add_argument("--task", nargs=2, metavar=("TASK", "DEST"))
    parser.add_argument("--suite", type=Path)
    parser.add_argument("--tag", default="localization")
    parser.add_argument("--strip-comments", action="store_true")
    parser.add_argument("--check", action="store_true", help="compile/test the original and the copy")
    args = parser.parse_args(argv)
    failures = 0
    if args.tree:
        info = obfuscate_tree(Path(args.tree[0]), Path(args.tree[1]), strip_comments=args.strip_comments)
        print(json.dumps({"renamed": len(info["mapping"]), "files": info["files"]}, indent=1))
        if args.check:
            for problem in check_tree(Path(args.tree[1])):
                print("PROBLEM", problem)
                failures += 1
    elif args.task:
        obfuscate_task(Path(args.task[0]), Path(args.task[1]), strip_comments=args.strip_comments)
    elif args.suite:
        for task in sorted(args.suite.iterdir()):
            spec_path = task / "task.json"
            if not spec_path.is_file() or "-obf" in task.name:
                continue
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            if args.tag not in spec.get("tags", []) or "fault" not in spec:
                continue
            for strip, suffix in ((False, "-obf"), (True, "-obf-nc")):
                dest = args.suite / f"{task.name}{suffix}"
                info = obfuscate_task(task, dest, strip_comments=strip)
                line = f"{dest.name}: {len(info['mapping'])} names renamed"
                if args.check:
                    before = check_tree(task / spec.get("fixture", "files"))
                    after = check_tree(dest / spec.get("fixture", "files"))
                    if after != before:
                        failures += 1
                        line += f"\n  PROBLEM: original {before or 'ok'}; copy {after}"
                    else:
                        line += " (checked: same as the original)"
                print(line)
    else:
        parser.print_help()
        return 2
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
