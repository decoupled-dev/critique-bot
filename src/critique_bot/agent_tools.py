"""Tool handlers for the local agent and the per-task working memory.

Every handler takes the parsed arguments and a :class:`ToolContext` and
returns ``{"tool", "ok", "output"/"error"}``. Output is bounded so one call
cannot push the task out of the chat's view.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from critique_bot import agent_edit, agent_shell, code_index
from critique_bot.code_index import SKIP_DIR_NAMES, refresh_path
from critique_bot.patch import looks_binary_bytes, looks_binary_path

ALLOWED_TOOLS = (
    "list_files",
    "find_files",
    "read_files",
    "search_code",
    "write_files",
    "edit_file",
    "delete_file",
    "run_command",
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
    "apply_patch",
    "todo",
    "skill",
)
MUTATING = frozenset({"write_files", "edit_file", "delete_file", "apply_patch"})

DEFAULT_TOOL_CHARS = 16_000
DEFAULT_COMMAND_TIMEOUT = 120
MAX_COMMAND_TIMEOUT = 600
DEFAULT_READ_LINES = 200
MAX_READ_LINES = 400
MAX_READ_CHARS = 12_000
OUTLINE_MIN_LINES = 400
MAX_LIST_ENTRIES = 2_000
SNIPPET_CHARS = 200
HITS_PER_FILE = 5
_SCAN_MAX_BYTES = 1_000_000
_CASE_FOLD = sys.platform == "win32"


@dataclass
class ReadRecord:
    version: str
    step: int
    spans: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class TaskState:
    """What the harness knows about the current task, repeated to the model."""

    task: str = ""
    step: int = 0
    reads: dict[str, ReadRecord] = field(default_factory=dict)
    edits: dict[str, int] = field(default_factory=dict)
    last_command: str = ""
    last_exit: str = ""
    failures_in_row: int = 0
    failed_calls: dict[str, int] = field(default_factory=dict)
    last_path: str = ""
    todos: list[dict[str, str]] = field(default_factory=list)

    @property
    def mutated(self) -> bool:
        return bool(self.edits)

    def note_read(self, rel: str, version: str, start: int, end: int) -> None:
        record = self.reads.get(rel)
        if record is None or record.version != version:
            record = ReadRecord(version=version, step=self.step)
            self.reads[rel] = record
        record.spans.append((start, end))
        self.last_path = rel

    def seen_span(self, rel: str, version: str, start: int, end: int) -> int | None:
        """Step of an earlier read that already covered ``start``-``end`` unchanged."""
        record = self.reads.get(rel)
        if record is None or record.version != version:
            return None
        for begin, finish in record.spans:
            if begin <= start and end <= finish:
                return record.step
        return None

    def note_write(self, rel: str, version: str) -> None:
        self.edits[rel] = self.edits.get(rel, 0) + 1
        record = self.reads.get(rel)
        if record is not None:
            record.version = version
            record.spans = []
        self.last_path = rel

    def render(self, template: str) -> str:
        reads = ", ".join(
            f"{path} ({_spans_text(record.spans) or 'outline'})"
            for path, record in list(self.reads.items())[-8:]
        ) or "none"
        edits = ", ".join(f"{path} x{count}" for path, count in self.edits.items()) or "none"
        command = f"{_one_line(self.last_command, 80)} -> {self.last_exit}" if self.last_command else "none"
        todos = ", ".join(
            f"[{item['status']}] {item['content']}" for item in self.todos[:8]
        ) or "none"
        return fill(
            template,
            task=_one_line(self.task, 600),
            step=str(self.step),
            reads=reads,
            edits=edits,
            command=command,
            todos=todos,
            failures=str(self.failures_in_row),
            path=self.last_path or "path/to/file",
        )


def fill(template: str, **values: str) -> str:
    """Replace ``{name}`` markers. Prompt text holds JSON braces, so not ``str.format``."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def _spans_text(spans: list[tuple[int, int]]) -> str:
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return ",".join(f"{a}-{b}" for a, b in merged[:4])


@dataclass
class ToolContext:
    workspace: Path
    index_path: Path | None = None
    cache_dir: Path | None = None
    max_chars: int = DEFAULT_TOOL_CHARS
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT
    runner: Callable[..., Any] | None = None
    state: TaskState | None = None
    checkpoints: agent_edit.Checkpoints | None = None
    shell: agent_shell.Shell | None = None

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).resolve()


def execute(name: str, arguments: dict[str, Any] | None, ctx: ToolContext) -> dict[str, Any]:
    canonical = canonical_tool(name)
    if not canonical:
        hint = _did_you_mean(name)
        return {
            "tool": name,
            "ok": False,
            "error": "unknown tool" + (f"; did you mean {hint}?" if hint else ""),
            "allowed": list(ALLOWED_TOOLS),
        }
    args = normalize_args(canonical, arguments if isinstance(arguments, dict) else {})
    try:
        result = _HANDLERS[canonical](args, ctx)
    except Exception as exc:  # a tool bug must come back as a result, not end the task
        return {"tool": canonical, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result.setdefault("tool", canonical)
    return result


_ALIASES = {
    "grep": "search_code",
    "rg": "search_code",
    "search": "search_code",
    "glob": "find_files",
    "glob_files": "find_files",
    "find": "find_files",
    "ls": "list_files",
    "list": "list_files",
    "read": "read_files",
    "read_file": "read_files",
    "cat": "read_files",
    "write": "write_files",
    "write_file": "write_files",
    "create_file": "write_files",
    "edit": "edit_file",
    "search_replace": "edit_file",
    "multi_edit": "edit_file",
    "str_replace": "edit_file",
    "bash": "run_command",
    "shell": "run_command",
    "powershell": "run_command",
    "terminal": "run_command",
    "todowrite": "todo",
    "todoread": "todo",
    "todos": "todo",
    "patch": "apply_patch",
}


def canonical_tool(name: str) -> str:
    """The tool that actually runs. OpenCode names map onto these."""
    key = str(name).strip()
    if key in ALLOWED_TOOLS:
        return key
    return _ALIASES.get(key.lower(), "")


def normalize_args(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Accept the argument names OpenCode and similar agents send."""
    out = dict(args)
    for src, dest in (
        ("filePath", "path"),
        ("file_path", "path"),
        ("workdir", "cwd"),
        ("working_directory", "cwd"),
    ):
        if dest not in out and src in out:
            out[dest] = out[src]
    if tool == "edit_file":
        for src, dest in (("oldString", "old_string"), ("newString", "new_string"), ("replaceAll", "replace_all")):
            if dest not in out and src in out:
                out[dest] = out[src]
    if tool == "write_files" and "contents" not in out and "content" in out:
        out["contents"] = out["content"]
    if tool == "search_code":
        if "glob" not in out and isinstance(out.get("include"), str):
            out["glob"] = out["include"]
        if "case_insensitive" not in out and "caseSensitive" in out:
            out["case_insensitive"] = not bool(out["caseSensitive"])
    if tool == "run_command":
        timeout = out.get("timeout")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > MAX_COMMAND_TIMEOUT:
            out["timeout"] = float(timeout) / 1000.0
    if tool == "git_show" and "rev" not in out and "commit" in out:
        out["rev"] = out["commit"]
    if tool == "skill" and "name" not in out and "skill" in out:
        out["name"] = out["skill"]
    return out


def _did_you_mean(name: str) -> str:
    import difflib

    pool = list(ALLOWED_TOOLS) + list(_ALIASES)
    matches = difflib.get_close_matches(str(name).strip().lower(), [item.lower() for item in pool], n=1, cutoff=0.8)
    if not matches:
        return ""
    return canonical_tool(matches[0]) or matches[0]


# --------------------------------------------------------------------------- paths


def resolve(workspace: Path, raw: str) -> Path:
    path = Path(str(raw).strip().strip('"').strip("'"))
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


def rel(workspace: Path, path: Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(workspace).as_posix()
    except ValueError:
        pass
    if _CASE_FOLD:
        left = os.path.normcase(str(resolved))
        root = os.path.normcase(str(workspace))
        if left.startswith(root.rstrip("\\/") + os.sep):
            return str(resolved)[len(str(workspace).rstrip("\\/")) + 1 :].replace("\\", "/")
    return str(resolved)


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Glob to regex. ``**`` spans directories; ``*`` and ``?`` stay inside one.

    A pattern without a slash matches the file name at any depth, the way
    ripgrep's ``-g`` and ``.gitignore`` do.
    """
    text = pattern.replace("\\", "/").strip()
    if text.startswith("./"):
        text = text[2:]
    anywhere = "/" not in text.rstrip("/")
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if text.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
            continue
        if text.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            close = text.find("]", index + 1)
            if close < 0:
                out.append(re.escape(char))
            else:
                body = text[index + 1 : close].replace("!", "^", 1) if text[index + 1 : index + 2] == "!" else text[index + 1 : close]
                out.append("[" + body + "]")
                index = close
        elif char == "{":
            close = text.find("}", index + 1)
            if close < 0:
                out.append(re.escape(char))
            else:
                options = text[index + 1 : close].split(",")
                out.append("(?:" + "|".join(re.escape(item) for item in options) + ")")
                index = close
        else:
            out.append(re.escape(char))
        index += 1
    body = "".join(out)
    prefix = "(?:.*/)?" if anywhere else ""
    flags = re.IGNORECASE if _CASE_FOLD else 0
    return re.compile("^" + prefix + body + "$", flags)


def glob_match(rel_path: str, pattern: str | None) -> bool:
    if not pattern:
        return True
    return glob_regex(pattern).match(rel_path) is not None


def walk_files(workspace: Path, root: Path) -> list[str]:
    """Workspace-relative file paths under ``root``, skipping generated trees."""
    if root.is_file():
        return [rel(workspace, root)]
    prefix = rel(workspace, root)
    if prefix in {".", ""} or root == workspace:
        prefix = ""
    listed = code_index._git_files(workspace)
    if listed is not None:
        out = []
        for item in listed:
            if prefix and not (item == prefix or item.startswith(prefix + "/")):
                continue
            if any(part in SKIP_DIR_NAMES for part in item.split("/")[:-1]):
                continue
            out.append(item)
        return out
    rows: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIR_NAMES)
        for name in sorted(filenames):
            rows.append(rel(workspace, Path(dirpath) / name))
    return rows


# --------------------------------------------------------------------------- results


def _cap(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    note = "\n...[truncated; narrow with path, offset, or a tighter pattern]"
    return text[: max(0, limit - len(note))] + note


def ok(name: str, output: str, ctx: ToolContext) -> dict[str, Any]:
    return {"tool": name, "ok": True, "output": _cap(output, ctx.max_chars)}


def err(name: str, error: str, *, output: str = "", ctx: ToolContext | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"tool": name, "ok": False, "error": error}
    if output:
        result["output"] = _cap(output, ctx.max_chars) if ctx else output
    return result


def _one_line(text: str, limit: int = 120) -> str:
    compact = " ".join(str(text).split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def _int_arg(args: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = args.get(name)
        if value is None or value == "":
            continue
        return int(value)
    return None


# --------------------------------------------------------------------------- list / find


def _list_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw = args.get("path") or "."
    if not isinstance(raw, str):
        return err("list_files", "path must be a string")
    target = resolve(ctx.workspace, raw.strip() or ".")
    if not target.exists():
        return err("list_files", f"not found: {raw}" + _path_hint(ctx, raw))
    glob = args.get("glob")
    if glob is not None and not isinstance(glob, str):
        return err("list_files", "glob must be a string")
    try:
        max_entries = int(args.get("max_entries") or 200)
        depth = int(args.get("depth") or 1)
    except (TypeError, ValueError):
        return err("list_files", "max_entries and depth must be integers")
    max_entries = min(max(max_entries, 1), MAX_LIST_ENTRIES)
    depth = min(max(depth, 1), 4)
    if target.is_file():
        return ok("list_files", f"f {rel(ctx.workspace, target)}", ctx)
    recursive = bool(glob and ("**" in glob or "/" in glob))
    if recursive:
        files = [item for item in walk_files(ctx.workspace, target) if glob_match(item, glob)]
        rows = [f"f {item}" for item in files[:max_entries]]
        if len(files) > max_entries:
            rows.append(f"... {len(files) - max_entries} more; narrow path or glob")
        return ok("list_files", "\n".join(rows) if rows else "(no files match)", ctx)
    rows: list[str] = []
    total = 0
    for line in _tree(ctx.workspace, target, depth, glob):
        total += 1
        if len(rows) < max_entries:
            rows.append(line)
    if total > max_entries:
        rows.append(f"... {total - max_entries} more; narrow path, glob, or depth")
    rows.append("Generated trees (out, build, prebuilts, intermediates, node_modules) are skipped.")
    return ok("list_files", "\n".join(rows) if len(rows) > 1 else "(empty)", ctx)


def _tree(workspace: Path, folder: Path, depth: int, glob: str | None, level: int = 0):
    try:
        children = sorted(folder.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    except OSError as exc:
        yield f"! {exc}"
        return
    pad = "  " * level
    for child in children:
        if child.is_dir():
            if child.name in SKIP_DIR_NAMES:
                continue
            count = _count_files(child)
            yield f"{pad}d {rel(workspace, child)}/ ({count} files)"
            if level + 1 < depth:
                yield from _tree(workspace, child, depth, glob, level + 1)
        else:
            if glob and not fnmatch.fnmatch(child.name.lower() if _CASE_FOLD else child.name, glob.lower() if _CASE_FOLD else glob):
                continue
            yield f"{pad}f {rel(workspace, child)}"


def _count_files(folder: Path, limit: int = 10_000) -> str:
    count = 0
    for _dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        count += len(filenames)
        if count >= limit:
            return f"{limit}+"
    return str(count)


def _find_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pattern = args.get("glob") or args.get("pattern") or args.get("name")
    if not isinstance(pattern, str) or not pattern.strip():
        return err("find_files", "glob is required, for example **/*Service.java or *.kt")
    raw = args.get("path") or "."
    if not isinstance(raw, str):
        return err("find_files", "path must be a string")
    root = resolve(ctx.workspace, raw)
    if not root.exists():
        return err("find_files", f"not found: {raw}")
    try:
        limit = min(max(int(args.get("limit") or args.get("head_limit") or 100), 1), 500)
    except (TypeError, ValueError):
        return err("find_files", "limit must be an integer")
    files = [item for item in walk_files(ctx.workspace, root) if glob_match(item, pattern.strip())]
    if not files and "*" not in pattern and "?" not in pattern:
        needle = pattern.strip().lower()
        files = [item for item in walk_files(ctx.workspace, root) if needle in item.lower()]

    def mtime(item: str) -> float:
        try:
            return (ctx.workspace / item).stat().st_mtime
        except OSError:
            return 0.0

    files.sort(key=lambda item: -mtime(item))
    rows = files[:limit]
    if len(files) > limit:
        rows.append(f"... {len(files) - limit} more; narrow the glob or path")
    return ok("find_files", "\n".join(rows) if rows else "(no files match)", ctx)


def _path_hint(ctx: ToolContext, raw: str) -> str:
    name = Path(str(raw)).name
    if not name:
        return ""
    regex = glob_regex(name)
    hits = [item for item in walk_files(ctx.workspace, ctx.workspace) if regex.match(item)][:5]
    if not hits:
        lowered = name.lower()
        hits = [item for item in walk_files(ctx.workspace, ctx.workspace) if lowered in item.lower()][:5]
    return ("; files with that name: " + ", ".join(hits)) if hits else ""


# --------------------------------------------------------------------------- read


def _read_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    symbol = args.get("symbol")
    paths = args.get("paths")
    if paths is None and args.get("path"):
        paths = [args.get("path")]
    if isinstance(paths, str):
        paths = [paths]
    if symbol and isinstance(symbol, str):
        return _read_symbol(symbol.strip(), paths[0] if paths else None, args, ctx)
    if not isinstance(paths, list) or not paths:
        return err("read_files", "paths must be a list of files, or pass symbol")
    try:
        offset = _int_arg(args, "offset", "start_line", "line")
        limit = _int_arg(args, "limit", "count")
        end_line = _int_arg(args, "end_line")
    except (TypeError, ValueError):
        return err("read_files", "offset and limit must be integers")
    start = offset if offset is not None else 1
    if start < 1:
        return err("read_files", "offset starts at 1")
    if limit is None and end_line is not None:
        limit = end_line - start + 1
    if limit is not None and limit < 1:
        return err("read_files", "limit must be at least 1")
    force = bool(args.get("force"))
    budget = max(1500, min(MAX_READ_CHARS, ctx.max_chars // max(len(paths), 1)))
    parts: list[str] = []
    for raw in paths[:8]:
        if not isinstance(raw, str):
            return err("read_files", "each path must be a string")
        parts.append(_read_one(raw, start, limit, offset is not None, budget, force, ctx))
    if len(paths) > 8:
        parts.append(f"... {len(paths) - 8} more paths not read; read at most 8 per call")
    return ok("read_files", "\n".join(parts), ctx)


def _read_one(raw: str, start: int, limit: int | None, explicit: bool, budget: int, force: bool, ctx: ToolContext) -> str:
    path = resolve(ctx.workspace, raw)
    name = rel(ctx.workspace, path)
    if path.is_dir():
        return f"--- {name} ---\nthat path is a folder; use list_files"
    if not path.is_file():
        return f"--- {name} ---\nnot found: {raw}{_path_hint(ctx, raw)}"
    if looks_binary_path(str(path)):
        return f"--- {name} ---\nbinary file omitted"
    try:
        head = path.read_bytes()[:8192]
    except OSError as exc:
        return f"--- {name} ---\n{exc}"
    if looks_binary_bytes(head):
        return f"--- {name} ---\nbinary file omitted"
    loaded = agent_edit.load_text(path)
    lines = loaded.text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    total = len(lines)
    version = agent_edit.file_version(path)
    window = min(limit if limit is not None else DEFAULT_READ_LINES, MAX_READ_LINES)
    capped_note = ""
    if limit is not None and limit > MAX_READ_LINES:
        capped_note = f" (limit capped at {MAX_READ_LINES})"
    end = min(total, start + window - 1)
    state = ctx.state
    if state is not None and not force and total:
        seen = state.seen_span(name, version, start, end)
        if seen is not None:
            return (
                f"--- {name} ---\nlines {start}-{end} are unchanged since step {seen}; "
                "use that text. Pass force true if you no longer have it."
            )
    eol = "crlf" if loaded.eol == "\r\n" else "lf"
    header = f"--- {name} (lines {start}-{end} of {total}, eol {eol}, version {version}) ---"
    out: list[str] = [header]
    if not explicit and total > OUTLINE_MIN_LINES and ctx.index_path is not None:
        items = code_index.outline(ctx.index_path, name, limit=80)
        if items:
            out.append("outline (line-end kind name):")
            out.extend(f"  {item.line}-{item.end_line} {item.kind} {item.qualified}" for item in items)
            out.append("first lines:")
    numbered: list[str] = []
    used = 0
    for index in range(start - 1, end):
        piece = f"{index + 1}|{lines[index]}"
        if numbered and used + len(piece) + 1 > budget:
            break
        numbered.append(piece)
        used += len(piece) + 1
    if start > total and total:
        out.append(f"offset {start} is past the end; the file has {total} lines")
    elif total == 0:
        out.append("(empty file)")
    out.extend(numbered)
    shown_end = start + len(numbered) - 1
    if numbered and state is not None:
        state.note_read(name, version, start, shown_end)
    if numbered and shown_end < total:
        out.append(
            f"... {total} lines; next offset={shown_end + 1} limit={DEFAULT_READ_LINES}{capped_note}. "
            "Do not edit a span you have not seen."
        )
    return "\n".join(out)


def _read_symbol(symbol: str, raw_path: Any, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.index_path is None:
        return err("read_files", "symbol reads need the index; run bot-agent init, or pass paths and offset")
    hit: code_index.Symbol | None = None
    if isinstance(raw_path, str) and raw_path.strip():
        name = rel(ctx.workspace, resolve(ctx.workspace, raw_path))
        hit = code_index.symbol_at(ctx.index_path, name, symbol)
    if hit is None:
        short = symbol.rpartition(".")[2]
        pattern = "^" + re.escape(short) + "$"
        found = code_index.find_symbols(ctx.index_path, pattern, limit=20)
        if "." in symbol:
            parent = symbol.rpartition(".")[0].rpartition(".")[2]
            narrowed = [item for item in found if item.parent == parent]
            found = narrowed or found
        if len(found) > 1:
            listing = "\n".join(
                f"{item.path}:{item.line}-{item.end_line} {item.kind} {item.qualified}" for item in found[:15]
            )
            return err("read_files", f"symbol {symbol} is defined {len(found)} times; pass path", output=listing)
        hit = found[0] if found else None
    if hit is None:
        close = code_index.close_symbols(ctx.index_path, symbol.rpartition(".")[2])
        hint = f"; did you mean {', '.join(close)}" if close else ""
        return err("read_files", f"symbol not found: {symbol}{hint}")
    start = max(1, hit.line - 2)
    count = min(MAX_READ_LINES, hit.end_line - start + 1)
    text = _read_one(hit.path, start, count, True, MAX_READ_CHARS, bool(args.get("force")), ctx)
    return ok("read_files", f"{hit.kind} {hit.qualified} at {hit.path}:{hit.line}-{hit.end_line}\n{text}", ctx)


# --------------------------------------------------------------------------- search


def _search_code(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pattern = args.get("pattern") or args.get("query")
    if not isinstance(pattern, str) or not pattern:
        return err("search_code", "pattern is required")
    raw_path = args.get("path") or "."
    if not isinstance(raw_path, str):
        return err("search_code", "path must be a string")
    root = resolve(ctx.workspace, raw_path)
    if not root.exists():
        return err("search_code", f"not found: {raw_path}")
    glob = args.get("glob")
    if glob is not None and not isinstance(glob, str):
        return err("search_code", "glob must be a string")
    try:
        limit = min(max(int(args.get("head_limit") or args.get("limit") or 50), 1), 200)
        context = min(max(int(args.get("context") or 0), 0), 3)
    except (TypeError, ValueError):
        return err("search_code", "head_limit and context must be integers")
    literal = bool(args.get("literal") or args.get("fixed_strings"))
    notes: list[str] = []
    source = re.escape(pattern) if literal else pattern
    case = args.get("case_insensitive")
    ignore_case = bool(case) if case is not None else pattern == pattern.lower()
    try:
        matcher = re.compile(source, re.IGNORECASE if ignore_case else 0)
    except re.error:
        source = re.escape(pattern)
        literal = True
        matcher = re.compile(source, re.IGNORECASE if ignore_case else 0)
        notes.append("pattern was not a valid regular expression; searched it as literal text")
    prefix = "" if root == ctx.workspace else rel(ctx.workspace, root)
    lines: list[str] = []
    seen: set[tuple[str, int]] = set()
    if ctx.index_path is not None and (not literal or re.fullmatch(r"[\w.]+", pattern)):
        symbol_pattern = re.escape(pattern) if literal else pattern
        for item in code_index.find_symbols(ctx.index_path, symbol_pattern, limit=min(limit, 20), path_prefix=prefix):
            if glob and not glob_match(item.path, glob):
                continue
            signature = _one_line(item.signature, SNIPPET_CHARS)
            lines.append(f"{item.path}:{item.line}: {item.kind} {item.qualified} (lines {item.line}-{item.end_line}) {signature}".rstrip())
            seen.add((item.path, item.line))
    hits, total, files_hit = _text_hits(ctx, root, prefix, source, matcher, ignore_case, glob, literal, pattern)
    touched = set(ctx.state.reads) | set(ctx.state.edits) if ctx.state else set()
    order = sorted(hits, key=lambda item: (item not in touched, item.count("/"), item))
    shown = 0
    for path in order:
        for line_no, text in hits[path][:HITS_PER_FILE]:
            if (path, line_no) in seen:
                continue
            if len(lines) >= limit:
                break
            lines.append(f"{path}:{line_no}: {_one_line(text, SNIPPET_CHARS)}")
            shown += 1
            if context:
                lines.extend(_context_lines(ctx.workspace / path, line_no, context))
    remaining = total - shown
    if remaining > 0:
        notes.append(f"{remaining} more hits in {files_hit} files; narrow with path, glob, or a longer pattern")
    body = "\n".join(lines) if lines else "(no matches)"
    if notes:
        body += "\n" + "\n".join(notes)
    return ok("search_code", body, ctx)


def _context_lines(path: Path, line_no: int, context: int) -> list[str]:
    try:
        text = agent_edit.load_text(path).text.split("\n")
    except OSError:
        return []
    out = []
    for number in range(max(1, line_no - context), min(len(text), line_no + context) + 1):
        if number != line_no:
            out.append(f"    {number}| {_one_line(text[number - 1], SNIPPET_CHARS)}")
    return out


def _text_hits(ctx, root, prefix, source, matcher, ignore_case, glob, literal, pattern):
    """``{path: [(line, text)]}``, total hit count, and number of files hit."""
    via_rg = _rg_hits(ctx, root, source, ignore_case, glob, literal, pattern)
    if via_rg is not None:
        return via_rg
    files: list[str] | None = None
    if ctx.index_path is not None:
        needle = pattern if literal else code_index.longest_literal(pattern)
        files = code_index.candidate_files(ctx.index_path, needle, path_prefix=prefix)
    if files is None:
        files = walk_files(ctx.workspace, root)
    hits: dict[str, list[tuple[int, str]]] = {}
    total = 0
    for item in files:
        if glob and not glob_match(item, glob):
            continue
        if looks_binary_path(item):
            continue
        path = ctx.workspace / item
        try:
            if path.stat().st_size > _SCAN_MAX_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if looks_binary_bytes(data[:8192]):
            continue
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), start=1):
            if matcher.search(line) is None:
                continue
            total += 1
            bucket = hits.setdefault(item, [])
            if len(bucket) < HITS_PER_FILE:
                bucket.append((number, line.strip()))
        if total > 5_000:
            break
    return hits, total, len(hits)


def _rg_hits(ctx, root, source, ignore_case, glob, literal, pattern):
    exe = shutil.which("rg")
    if exe is None or ctx.runner is not None:
        return None
    argv = [exe, "--json", "--hidden", "--max-columns", "400", "--max-filesize", "1M"]
    if ignore_case:
        argv.append("-i")
    if literal:
        argv += ["-F", "-e", pattern]
    else:
        argv += ["-e", source]
    for name in sorted(SKIP_DIR_NAMES):
        argv += ["-g", f"!{name}/"]
    if glob:
        argv += ["-g", glob]
    target = rel(ctx.workspace, root) if root != ctx.workspace else "."
    argv += ["--", target]
    try:
        proc = subprocess.run(argv, cwd=str(ctx.workspace), capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):
        return None
    hits: dict[str, list[tuple[int, str]]] = {}
    total = 0
    for raw in proc.stdout.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data", {})
        path_text = (data.get("path") or {}).get("text", "")
        if not path_text:
            continue
        item = rel(ctx.workspace, ctx.workspace / path_text)
        text = (data.get("lines") or {}).get("text", "")
        total += 1
        bucket = hits.setdefault(item, [])
        if len(bucket) < HITS_PER_FILE:
            bucket.append((int(data.get("line_number") or 0), text.strip()))
    return hits, total, len(hits)


# --------------------------------------------------------------------------- write / edit / delete


def _after_write(ctx: ToolContext, path: Path) -> str:
    name = rel(ctx.workspace, path)
    version = agent_edit.file_version(path)
    if ctx.state is not None:
        ctx.state.note_write(name, version)
    if ctx.index_path is not None:
        refresh_path(ctx.workspace, ctx.index_path, path)
    return version


def _write_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    files = args.get("files")
    if files is None and args.get("path") is not None:
        files = [{"path": args.get("path"), "contents": args.get("contents", args.get("content", ""))}]
    if not isinstance(files, list) or not files:
        return err("write_files", "files must be a list of {path, contents}")
    overwrite = bool(args.get("overwrite"))
    written: list[str] = []
    errors: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            errors.append("each file must be an object")
            continue
        raw = item.get("path")
        contents = item.get("contents", item.get("content"))
        if not isinstance(raw, str) or not raw:
            errors.append("file path is required")
            continue
        if not isinstance(contents, str):
            errors.append(f"contents must be a string: {raw}")
            continue
        path = resolve(ctx.workspace, raw)
        name = rel(ctx.workspace, path)
        if path == ctx.workspace:
            errors.append("refusing to write the workspace root")
            continue
        if path.is_dir():
            errors.append(f"path is a directory: {raw}")
            continue
        existed = path.is_file()
        if (
            existed
            and ctx.state is not None
            and not (overwrite or item.get("overwrite"))
            and name not in ctx.state.reads
            and name not in ctx.state.edits
        ):
            errors.append(
                f"{name} already exists and was not read in this task. Read it and use edit_file, "
                "or pass overwrite true to replace the whole file"
            )
            continue
        like = agent_edit.load_text(path) if existed else None
        before = like.text if like is not None else ""
        if ctx.checkpoints is not None:
            ctx.checkpoints.save(path)
        agent_edit.save_text(path, contents, like)
        _after_write(ctx, path)
        line_count = contents.count("\n") + (0 if contents.endswith("\n") or not contents else 1)
        summary = f"{'replaced' if existed else 'created'} {name} ({line_count} lines)"
        check = agent_edit.syntax_check(name, contents)
        if check:
            summary += f"; syntax {check}"
        if existed:
            diff = agent_edit.hunk_diff(before, contents.replace("\r\n", "\n"), name)
            if diff:
                summary += "\n" + diff
        written.append(summary)
    output = "\n".join(written) if written else "wrote nothing"
    if errors:
        return err("write_files", "; ".join(errors), output=output, ctx=ctx)
    return ok("write_files", output, ctx)


def _edit_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw = args.get("path") or args.get("file")
    if not isinstance(raw, str) or not raw:
        return err("edit_file", "path is required")
    edits = args.get("edits")
    if edits is None:
        edits = [
            {
                "old_string": args.get("old_string", args.get("old")),
                "new_string": args.get("new_string", args.get("new")),
                "replace_all": args.get("replace_all"),
            }
        ]
    if not isinstance(edits, list) or not edits:
        return err("edit_file", "edits must be a list of {old_string, new_string}")
    path = resolve(ctx.workspace, raw)
    name = rel(ctx.workspace, path)
    if not path.is_file():
        return err(
            "edit_file",
            f"not found: {raw}. Use write_files to create a new file{_path_hint(ctx, raw)}",
        )
    expected = args.get("version")
    if isinstance(expected, str) and expected and expected != agent_edit.file_version(path):
        return err("edit_file", f"{name} changed since you read it (version {expected}); read it again")
    loaded = agent_edit.load_text(path)
    text = loaded.text
    notes: list[str] = []
    total = 0
    for number, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            return err("edit_file", f"edit {number} must be an object")
        old = edit.get("old_string")
        new = edit.get("new_string")
        if not isinstance(old, str) or old == "":
            return err("edit_file", f"edit {number}: old_string is empty. Use write_files for a new file")
        if not isinstance(new, str):
            return err("edit_file", f"edit {number}: new_string must be a string")
        result = agent_edit.replace_span(text, old, new, replace_all=bool(edit.get("replace_all")))
        if result.text is None:
            label = f"edit {number} of {len(edits)}: " if len(edits) > 1 else ""
            output = ""
            if result.candidate:
                output = (
                    "closest text in the file. Copy old_string from it exactly, without the N| prefix:\n"
                    + result.candidate
                )
            suffix = "; no edit was applied" if len(edits) > 1 else ""
            return err("edit_file", label + result.note + suffix, output=output, ctx=ctx)
        text = result.text
        total += result.count
        if result.note:
            notes.append(result.note)
    before_check = agent_edit.syntax_check(name, loaded.text)
    after_check = agent_edit.syntax_check(name, text)
    diff = agent_edit.hunk_diff(loaded.text, text, name)
    strict = Path(name).suffix.lower() in {".py", ".json"}
    if strict and before_check == "ok" and after_check.startswith("error"):
        return err(
            "edit_file",
            f"edit not applied: it would leave {name} with a syntax {after_check}",
            output=diff,
            ctx=ctx,
        )
    if ctx.checkpoints is not None:
        ctx.checkpoints.save(path)
    agent_edit.save_text(path, text, loaded)
    version = _after_write(ctx, path)
    message = f"updated {name} ({total})"
    if notes:
        message += "; " + "; ".join(dict.fromkeys(notes))
    if after_check:
        message += f"; syntax {after_check}"
    message += f"; version {version}"
    if diff:
        message += "\n" + diff
    return ok("edit_file", message, ctx)


def _delete_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw = args.get("path")
    if not isinstance(raw, str) or not raw:
        return err("delete_file", "path is required")
    path = resolve(ctx.workspace, raw)
    if path == ctx.workspace:
        return err("delete_file", "refusing to delete the workspace root")
    if path.is_dir():
        return err("delete_file", f"path is a directory: {raw}")
    if not path.is_file():
        return err("delete_file", f"not found: {raw}")
    if ctx.checkpoints is not None:
        ctx.checkpoints.save(path)
    path.unlink()
    _after_write(ctx, path)
    return ok("delete_file", f"deleted {rel(ctx.workspace, path)}", ctx)


def _apply_patch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    patch = args.get("patch") or args.get("diff")
    if not isinstance(patch, str) or not patch.strip():
        return err("apply_patch", "patch is required")
    patch = patch.replace("\r\n", "\n")
    if not patch.endswith("\n"):
        patch += "\n"
    folder = ctx.cache_dir or Path(tempfile.mkdtemp(prefix="bot-patch-"))
    folder.mkdir(parents=True, exist_ok=True)
    patch_path = folder / "apply.patch"
    patch_path.write_text(patch, encoding="utf-8")
    paths = patched_paths(patch)
    attempts = (
        ["apply", "--whitespace=nowarn", "--unsafe-paths"],
        ["apply", "--whitespace=nowarn", "--unsafe-paths", "--ignore-whitespace", "--recount"],
    )
    check_error = ""
    for flags in attempts:
        checked = _git(ctx, [*flags, "--check", str(patch_path)], tool="apply_patch")
        if checked.get("ok"):
            break
        check_error = checked.get("error") or checked.get("output") or ""
    else:
        return err(
            "apply_patch",
            "patch does not apply: " + _one_line(check_error, 400)
            + ". Read the file again and fix the context lines, or use edit_file",
        )
    if ctx.checkpoints is not None:
        for item in paths:
            ctx.checkpoints.save(ctx.workspace / item)
    result = _git(ctx, [*flags, str(patch_path)], tool="apply_patch")
    if result.get("ok"):
        checks = []
        for item in paths:
            target = ctx.workspace / item
            _after_write(ctx, target)
            if target.is_file():
                check = agent_edit.syntax_check(item, agent_edit.load_text(target).text)
                if check:
                    checks.append(f"{item}: syntax {check}")
        if checks or paths:
            result["output"] = (
                "applied to " + ", ".join(paths) + ("\n" + "\n".join(checks) if checks else "")
            )
    return result


def patched_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ "):
            raw = line[4:].split("\t", 1)[0].strip()
            if raw.startswith("b/"):
                raw = raw[2:]
            if raw and raw != "/dev/null" and raw not in paths:
                paths.append(raw)
    return paths


# --------------------------------------------------------------------------- commands and git


def _run_command(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    command = args.get("command") or args.get("cmd")
    if not isinstance(command, str) or not command.strip():
        return err("run_command", "command is required")
    try:
        timeout = float(args.get("timeout") or ctx.command_timeout)
    except (TypeError, ValueError):
        return err("run_command", "timeout must be a number")
    timeout = min(max(timeout, 1), MAX_COMMAND_TIMEOUT)
    shell = ctx.shell or agent_shell.detect_shell()
    problem = agent_shell.lint_command(command, shell)
    if problem:
        return err("run_command", problem)
    cwd = ctx.workspace
    raw_cwd = args.get("cwd")
    if isinstance(raw_cwd, str) and raw_cwd.strip():
        cwd = resolve(ctx.workspace, raw_cwd)
        if not cwd.is_dir():
            return err("run_command", f"cwd is not a folder: {raw_cwd}")
    code, stdout, stderr, seconds = agent_shell.run(
        command, cwd=cwd, timeout=timeout, shell=shell, runner=ctx.runner
    )
    out = agent_shell.tidy(stdout)
    errs = agent_shell.tidy(stderr)
    if ctx.state is not None:
        ctx.state.last_command = command
        ctx.state.last_exit = "timeout" if code is None else f"exit {code}"
    if code is None:
        body = "\n".join(part for part in (f"timed out after {timeout:.0f}s", f"cwd: {cwd}", out, errs) if part)
        hint = ""
        if agent_shell.waiting_for_input(out + "\n" + errs):
            hint = "; the command was waiting for input. Pass a flag such as -y, --yes, or -Force"
        return err(
            "run_command",
            f"timed out after {timeout:.0f}s{hint}",
            output=agent_shell.head_tail(body, max_chars=ctx.max_chars),
            ctx=ctx,
        )
    parts = [f"exit {code} ({seconds:.1f}s)", f"cwd: {cwd}"]
    if out:
        parts.append(out)
    if errs:
        parts.append(errs)
    body = agent_shell.head_tail("\n".join(parts), max_chars=ctx.max_chars)
    if code == 0:
        return ok("run_command", body, ctx)
    return err("run_command", "command failed", output=body, ctx=ctx)


def _git_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    del args
    return _git(ctx, ["status", "--short", "--branch"], tool="git_status")


def _git_diff(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    git_args = ["diff"]
    if args.get("staged"):
        git_args.append("--cached")
    if args.get("stat"):
        git_args.append("--stat")
    raw = args.get("path")
    if raw:
        if not isinstance(raw, str):
            return err("git_diff", "path must be a string")
        git_args.extend(["--", raw])
    return _git(ctx, git_args, tool="git_diff")


def _git(ctx: ToolContext, git_args: list[str], *, tool: str) -> dict[str, Any]:
    run = ctx.runner or subprocess.run
    try:
        proc = run(
            ["git", "-c", "core.quotepath=off", "-C", str(ctx.workspace), *git_args],
            capture_output=True,
            check=False,
            shell=False,
            **({} if ctx.runner else {"env": agent_shell.environment(), "timeout": 120}),
        )
    except FileNotFoundError:
        return err(tool, "git is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        return err(tool, "git timed out")
    code = int(getattr(proc, "returncode", 1) or 0)
    stdout = agent_shell.tidy(agent_shell.decode(getattr(proc, "stdout", b"")))
    stderr = agent_shell.tidy(agent_shell.decode(getattr(proc, "stderr", b"")))
    output = "\n".join(part for part in (f"exit {code}", stdout, stderr) if part)
    if code != 0:
        return err(tool, stderr or stdout or "git failed", output=output, ctx=ctx)
    return ok(tool, output if stdout or stderr else "exit 0\n(empty)", ctx)


def is_git_repo(workspace: Path) -> bool:
    return (Path(workspace) / ".git").exists()


_REV_RE = re.compile(r"^[A-Za-z0-9_./~^@{}:-]+$")
_TODO_STATUS = {"pending", "in_progress", "completed", "cancelled"}
_SKILL_ROOTS = (".bot/skills", ".agents/skills", ".opencode/skills")


def _git_log(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        limit = min(max(int(args.get("limit") or 20), 1), 50)
    except (TypeError, ValueError):
        return err("git_log", "limit must be an integer")
    git_args = ["log", f"-n{limit}", "--date=short", "--pretty=format:%h %ad %s"]
    raw = args.get("path")
    if raw:
        shown = _git_path(ctx, raw)
        if shown is None:
            return err("git_log", "path must be a file inside the workspace")
        git_args.extend(["--", shown])
    return _git(ctx, git_args, tool="git_log")


def _git_show(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    rev = args.get("rev") or "HEAD"
    if not isinstance(rev, str) or not _REV_RE.fullmatch(rev) or rev.startswith("-"):
        return err("git_show", "rev must be a commit, branch, or HEAD")
    git_args = ["show", "--stat", "--format=medium", "--no-color", rev]
    if args.get("patch"):
        git_args = ["show", "--format=medium", "--no-color", rev]
    raw = args.get("path")
    if raw:
        shown = _git_path(ctx, raw)
        if shown is None:
            return err("git_show", "path must be a file inside the workspace")
        git_args.extend(["--", shown])
    return _git(ctx, git_args, tool="git_show")


def _git_path(ctx: ToolContext, raw: object) -> str | None:
    if not isinstance(raw, str) or not raw.strip() or raw.startswith("-") or "\n" in raw:
        return None
    path = resolve(ctx.workspace, raw)
    try:
        return path.resolve().relative_to(ctx.workspace).as_posix()
    except ValueError:
        return None


def _todo(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    items = args.get("todos")
    if items is None:
        items = args.get("items")
    if items is None:
        current = ctx.state.todos if ctx.state is not None else []
        return ok("todo", _format_todos(current), ctx)
    if not isinstance(items, list):
        return err("todo", "todos must be a list of {content, status}")
    cleaned: list[dict[str, str]] = []
    in_progress = 0
    for index, item in enumerate(items[:20]):
        if isinstance(item, str):
            item = {"content": item, "status": "pending"}
        if not isinstance(item, dict):
            return err("todo", "each todo must be an object or a string")
        content = str(item.get("content") or item.get("text") or "").strip()
        if not content:
            return err("todo", "each todo needs content")
        status = str(item.get("status") or "pending")
        if status not in _TODO_STATUS:
            return err("todo", "status must be pending, in_progress, completed, or cancelled")
        if status == "in_progress":
            in_progress += 1
        cleaned.append({"id": str(item.get("id") or index + 1), "content": content[:200], "status": status})
    if in_progress > 1:
        return err("todo", "only one todo can be in_progress")
    if ctx.state is not None:
        ctx.state.todos = cleaned
    return ok("todo", _format_todos(cleaned), ctx)


def _format_todos(items: list[dict[str, str]]) -> str:
    if not items:
        return "no todos"
    return "\n".join(f"{item['id']}. [{item['status']}] {item['content']}" for item in items)


def _skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    found = _discover_skills(ctx.workspace)
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        if not found:
            return ok("skill", "no skills. Add SKILL.md under .bot/skills/<name>/.", ctx)
        return ok("skill", "\n".join(f"{item['name']}: {item['description']}" for item in found), ctx)
    key = name.strip()
    match = next((item for item in found if item["name"] == key), None)
    if match is None:
        names = ", ".join(item["name"] for item in found) or "none"
        return err("skill", f"unknown skill {key}. Available: {names}")
    try:
        text = Path(match["path"]).read_text(encoding="utf-8")
    except OSError as exc:
        return err("skill", str(exc))
    return ok("skill", text, ctx)


def _discover_skills(workspace: Path) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for root_rel in _SKILL_ROOTS:
        root = workspace / root_rel
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError:
                continue
            found.append(
                {
                    "name": skill_md.parent.name,
                    "description": _skill_description(text),
                    "path": str(skill_md),
                }
            )
    return found


def _skill_description(text: str) -> str:
    match = re.search(r"(?m)^description:\s*(.+)$", text)
    if match:
        return match.group(1).strip().strip("\"'")[:160]
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("---") and not line.startswith("#"):
            return line[:160]
    return ""


_HANDLERS: dict[str, Callable[[dict[str, Any], ToolContext], dict[str, Any]]] = {
    "list_files": _list_files,
    "find_files": _find_files,
    "read_files": _read_files,
    "search_code": _search_code,
    "write_files": _write_files,
    "edit_file": _edit_file,
    "delete_file": _delete_file,
    "run_command": _run_command,
    "git_status": _git_status,
    "git_diff": _git_diff,
    "git_log": _git_log,
    "git_show": _git_show,
    "apply_patch": _apply_patch,
    "todo": _todo,
    "skill": _skill,
}
