"""The guard after every write or edit: a parse check, then a fast lint of the changed file.

**Parse check.** tree-sitter when the optional ``index`` extra is installed (counting
``ERROR`` and missing nodes), else the language's own front end where it is cheap: Python
``compile``, JSON, XML, TOML in process, ``bash -n`` for shell scripts, and a brace balance
for the C family, Java, Kotlin, Go, Rust, JS/TS (:func:`agent_edit.syntax_check`). Only a
**new** problem counts: the file parsed before and does not now, or it has more error
nodes than before. What happens then is the ``edit_guard`` setting:

- ``"flag"`` (default): the change is kept and the result says what broke. Python, JSON and
  XML edits that break a file that parsed are still refused, as before (cheap, exact
  checks, and a half-applied edit there breaks the build or the app at once);
- ``"reject"``: any new syntax error refuses the change (SWE-agent style);
- ``"off"``: no parse report and no lint (the Python/JSON/XML refusal stays).

**Lint.** A per-language table of fast linters (ruff or pyflakes, shellcheck, ktlint,
``gofmt -e``), overridable with ``"lint"`` in ``.crit/settings.json`` (a suffix to a command
or a list of alternative commands, ``{file}`` for the file; ``"lint": false`` turns it off).
It runs on the changed file only, within ``lint_timeout_s`` (default 5 s) for the whole
check, and reports only issues that are **new** compared with the file before the change:
the old text is linted as a sibling temp copy and the two runs are compared by message,
ignoring line numbers. Linters that run code from the repository (eslint configs) are not
in the default table; ``lint`` is a security key, so a committed settings file cannot add
one.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from critique_bot import agent_edit, agent_shell

GUARD_MODES = ("flag", "reject", "off")
DEFAULT_LINT_TIMEOUT = 5.0
MAX_LINT_LINES = 10

#: suffix -> alternative commands; the first whose program is installed runs.
DEFAULT_LINT: dict[str, tuple[str, ...]] = {
    ".py": ("ruff check --quiet --no-cache --output-format=concise {file}", "{python} -m pyflakes {file}"),
    ".sh": ("shellcheck -f gcc {file}",),
    ".bash": ("shellcheck -f gcc {file}",),
    ".kt": ("ktlint --relative {file}",),
    ".kts": ("ktlint --relative {file}",),
    ".go": ("gofmt -e -l {file}",),
}

_TS_BY_SUFFIX = {
    ".py": ("tree_sitter_python", "language"),
    ".java": ("tree_sitter_java", "language"),
    ".kt": ("tree_sitter_kotlin", "language"),
    ".kts": ("tree_sitter_kotlin", "language"),
    ".go": ("tree_sitter_go", "language"),
    ".rs": ("tree_sitter_rust", "language"),
    ".c": ("tree_sitter_c", "language"),
    ".h": ("tree_sitter_cpp", "language"),
    ".cc": ("tree_sitter_cpp", "language"),
    ".cpp": ("tree_sitter_cpp", "language"),
    ".hpp": ("tree_sitter_cpp", "language"),
    ".cs": ("tree_sitter_c_sharp", "language"),
    ".js": ("tree_sitter_javascript", "language"),
    ".jsx": ("tree_sitter_javascript", "language"),
    ".mjs": ("tree_sitter_javascript", "language"),
    ".ts": ("tree_sitter_typescript", "language_typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx"),
}
_parsers: dict[str, Any] = {}


@dataclass
class Parse:
    """``status`` is "ok", "error: ...", or "" (not checked); ``errors`` counts error nodes."""

    status: str
    errors: int = 0
    via: str = ""

    @property
    def broken(self) -> bool:
        return self.status.startswith("error")


@dataclass
class Report:
    """What the guard found for one changed file."""

    name: str
    parse: Parse
    #: A syntax problem the change introduced (the text for the result), or "".
    new_syntax: str = ""
    #: New lint issues, as "path:line message".
    lint: list[str] = field(default_factory=list)
    #: Why lint did not run or could not compare ("" when it did, or had nothing to run).
    lint_note: str = ""

    def text(self) -> str:
        parts: list[str] = []
        if self.new_syntax:
            parts.append(f"syntax: the change introduced an error in {self.name}: {self.new_syntax}; fix it next")
        if self.lint:
            count = len(self.lint)
            shown = self.lint[:MAX_LINT_LINES]
            more = f"\n  ... {count - len(shown)} more" if count > len(shown) else ""
            parts.append(f"lint: {count} new {'issue' if count == 1 else 'issues'}:\n  " + "\n  ".join(shown) + more)
        if self.lint_note:
            parts.append(f"lint: {self.lint_note}")
        return "\n".join(parts)


def mode(settings: dict[str, Any] | None) -> str:
    raw = str((settings or {}).get("edit_guard") or "flag").strip().lower()
    return raw if raw in GUARD_MODES else "flag"


# ---------------------------------------------------------------------------- parse


def _tree_sitter(suffix: str) -> Any:
    """A tree-sitter parser for ``suffix``, or None (the extra is not installed, or no grammar)."""
    if suffix in _parsers:
        return _parsers[suffix]
    parser = None
    spec = _TS_BY_SUFFIX.get(suffix)
    if spec is not None:
        try:
            from tree_sitter import Language, Parser  # type: ignore

            module = importlib.import_module(spec[0])
            parser = Parser(Language(getattr(module, spec[1])()))
        except Exception:  # noqa: BLE001 (optional extra; any failure means "not available")
            parser = None
    _parsers[suffix] = parser
    return parser


def _tree_errors(parser: Any, text: str) -> tuple[int, int]:
    """Error and missing nodes in the parse tree, and the first one's line (1-based, 0 if none)."""
    tree = parser.parse(text.encode("utf-8", "replace"))
    root = tree.root_node
    if not getattr(root, "has_error", False):
        return 0, 0
    count, first = 0, 0
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or getattr(node, "is_missing", False):
            count += 1
            line = node.start_point[0] + 1
            first = line if not first else min(first, line)
            continue
        if getattr(node, "has_error", True):
            stack.extend(node.children)
    return max(count, 1), first


def parse_check(name: str, text: str) -> Parse:
    """Does ``text`` parse as the language of ``name``?"""
    suffix = Path(name).suffix.lower()
    parser = _tree_sitter(suffix)
    if parser is not None:
        try:
            count, line = _tree_errors(parser, text)
        except Exception:  # noqa: BLE001
            count, line = -1, 0
        if count == 0:
            return Parse("ok", 0, "tree-sitter")
        if count > 0:
            return Parse(f"error: line {line}: syntax error", count, "tree-sitter")
    if suffix in {".py", ".json"} or suffix in agent_edit.XML_SUFFIXES:
        status = agent_edit.syntax_check(name, text)
        return Parse(status, 1 if status.startswith("error") else 0, "compiler")
    if suffix == ".toml":
        return _toml_check(text)
    if suffix in {".sh", ".bash"}:
        return _bash_check(text)
    status = agent_edit.syntax_check(name, text)
    return Parse(status, 1 if status.startswith("error") else 0, "braces" if status else "")


def _toml_check(text: str) -> Parse:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:  # Python 3.10
        return Parse("")
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return Parse(f"error: {exc}", 1, "tomllib")
    return Parse("ok", 0, "tomllib")


def _bash_check(text: str) -> Parse:
    bash = shutil.which("bash")
    if bash is None or os.name == "nt":
        return Parse("")
    try:
        proc = subprocess.run([bash, "-n"], input=text.encode("utf-8", "replace"), capture_output=True, timeout=3,
                              check=False)
    except (OSError, subprocess.TimeoutExpired):
        return Parse("")
    if proc.returncode == 0:
        return Parse("ok", 0, "bash -n")
    message = proc.stderr.decode("utf-8", "replace").strip().split("\n")[0]
    message = re.sub(r"^bash: (?:line )?", "line ", message)
    return Parse(f"error: {message}", 1, "bash -n")


def new_syntax_error(name: str, before: str | None, after: str) -> tuple[Parse, str]:
    """The parse of ``after``, and the problem the change introduced ("" when none is new)."""
    now = parse_check(name, after)
    if not now.broken:
        return now, ""
    if before is None:
        return now, now.status.removeprefix("error: ")
    was = parse_check(name, before)
    if not was.broken or now.errors > was.errors:
        return now, now.status.removeprefix("error: ")
    return now, ""


# ---------------------------------------------------------------------------- lint


_ISSUE_RE = re.compile(r"^(?P<path>(?:[A-Za-z]:)?[^:\n]*):(?P<line>\d+)(?::(?P<col>\d+))?:?\s*(?P<msg>.+)$")


def lint_commands(name: str, settings: dict[str, Any] | None) -> list[str]:
    """The commands for ``name``'s suffix: the settings table first, else the default one."""
    suffix = Path(name).suffix.lower()
    table = (settings or {}).get("lint")
    if table is False:
        return []
    if isinstance(table, dict) and suffix in table:
        value = table[suffix]
        if value in (None, False, ""):
            return []
        return [value] if isinstance(value, str) else [item for item in value if isinstance(item, str)]
    return list(DEFAULT_LINT.get(suffix, ()))


def _argv(command: str, file: str) -> list[str] | None:
    """``command`` as argv with ``{file}`` and ``{python}`` filled in; None when its program is missing."""
    try:
        words = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return None
    if not words:
        return None
    argv = [word.replace("{python}", sys.executable).replace("{file}", file) for word in words]
    if words[0] == "{python}":
        if len(argv) >= 3 and argv[1] == "-m" and importlib.util.find_spec(argv[2]) is None:
            return None
        return argv
    program = shutil.which(argv[0])
    if program is None:
        return None
    return [program, *argv[1:]]


def _issues(output: str) -> list[tuple[int, str]]:
    found = []
    for line in output.splitlines():
        match = _ISSUE_RE.match(line.strip())
        if match is not None:
            found.append((int(match.group("line")), match.group("msg").strip()))
    return found


def _run(argv: list[str], cwd: Path, timeout: float) -> str | None:
    """Combined output of one lint run; None when it timed out or could not start."""
    if timeout <= 0:
        return None
    try:
        proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, timeout=timeout, check=False,
                              env=agent_shell.scrubbed_environment())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.decode("utf-8", "replace") + "\n" + proc.stderr.decode("utf-8", "replace")


def lint_new(path: Path, name: str, before: str | None, settings: dict[str, Any] | None,
             workspace: Path, *, like: agent_edit.TextFile | None = None) -> tuple[list[str], str]:
    """New lint issues in ``path`` (already written) compared with ``before``, and a note.

    Returns ``([], note)`` when lint did not run or could not compare (the note says why;
    empty when there is no linter for the file).
    """
    budget = float((settings or {}).get("lint_timeout_s") or DEFAULT_LINT_TIMEOUT)
    deadline = time.monotonic() + max(0.5, budget)
    argv = None
    for command in lint_commands(name, settings):
        argv = _argv(command, str(path))
        if argv is not None:
            break
    if argv is None:
        return [], ""
    program = Path(argv[0]).name if argv[0] != sys.executable else " ".join(argv[1:3])
    after_out = _run(argv, workspace, deadline - time.monotonic())
    if after_out is None:
        return [], f"skipped ({program} took longer than {budget:.0f}s)"
    after = _issues(after_out)
    if not after:
        return [], ""
    old: list[tuple[int, str]] = []
    if before is not None:
        copy = _sibling_copy(path, before, like)
        if copy is None:
            return [], "could not compare with the file before the change"
        try:
            index = argv.index(str(path))
            before_argv = argv[:index] + [str(copy)] + argv[index + 1:]
            before_out = _run(before_argv, workspace, deadline - time.monotonic())
        finally:
            copy.unlink(missing_ok=True)
        if before_out is None:
            return [], f"skipped the comparison ({program} took longer than {budget:.0f}s)"
        old = _issues(before_out.replace(copy.name, path.name))
    left = Counter(message for _line, message in old)
    fresh: list[str] = []
    for line, message in after:
        if left[message] > 0:
            left[message] -= 1
            continue
        fresh.append(f"{name}:{line} {message}")
    return fresh, ""


def _sibling_copy(path: Path, text: str, like: agent_edit.TextFile | None) -> Path | None:
    """The old text in a hidden file next to ``path`` (same folder, so the same lint config applies)."""
    try:
        handle, temp = tempfile.mkstemp(prefix=".crit-lint-", suffix=path.suffix, dir=str(path.parent))
        os.close(handle)
        agent_edit.save_text(Path(temp), text, like)
        return Path(temp)
    except (OSError, ValueError):
        return None


def check(path: Path, name: str, before: str | None, after: str, settings: dict[str, Any] | None,
          workspace: Path, *, like: agent_edit.TextFile | None = None, lint: bool = True) -> Report:
    """Parse ``after`` and lint the written file; ``before`` is None for a new file."""
    current = mode(settings)
    if current == "off":
        return Report(name, Parse(""))
    parse, problem = new_syntax_error(name, before, after)
    report = Report(name, parse, new_syntax=problem)
    if lint and path.is_file():
        report.lint, report.lint_note = lint_new(path, name, before, settings, workspace, like=like)
    return report
