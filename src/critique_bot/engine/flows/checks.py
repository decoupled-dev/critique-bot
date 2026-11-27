# SPDX-License-Identifier: Apache-2.0
"""What the verified flow checks with: the repro verdict, test output counts, test selection.

All of it is plain text work with no model: an expected-failure statement against a
command's output, failing-test counts from the usual runners' summaries, and the tests
that touch a set of changed files (path heuristics; the Stage 8 import graph when the
project index exists, through the ``code_nav`` tool).
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

# --------------------------------------------------------------------------- the repro

#: ``EXPECTED: ValueError: price must be positive`` / ``REPRO: python -m pytest .crit/repro/test_x.py``
_EXPECTED_RE = re.compile(r"^\s*\**\s*EXPECTED(?: FAILURE)?\s*\**\s*:\s*(.+?)\s*$", re.I | re.M)
_REPRO_RE = re.compile(r"^\s*\**\s*REPRO(?: COMMAND)?\s*\**\s*:\s*`*(.+?)`*\s*$", re.I | re.M)
_ERROR_TYPE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception|Failure|Panic|Fault)|panic|AssertionError|SIGSEGV|SIGABRT)\b"
)
#: Errors that say the repro itself is broken (it never reached the code under test).
SETUP_ERRORS = frozenset({
    "ImportError", "ModuleNotFoundError", "SyntaxError", "IndentationError", "NameError",
    "ClassNotFoundException", "NoClassDefFoundError", "FileNotFoundError", "CompilationError",
})
_SETUP_TEXT_RE = re.compile(r"(command not found|No such file or directory|cannot find symbol|unresolved reference"
                            r"|error: no tests ran|collected 0 items|Ran 0 tests|no tests to run)", re.I)
_STOP_WORDS = frozenset("a an the is are was be to of in on for and or not with when it its this that".split())


@dataclass(frozen=True)
class Expected:
    """The failure the model says the repro shows on the unmodified code."""

    kind: str
    message: str
    raw: str

    @property
    def words(self) -> list[str]:
        return [w for w in re.findall(r"[A-Za-z0-9_]{3,}", self.message.lower()) if w not in _STOP_WORDS][:12]


def parse_repro_reply(reply: str) -> tuple[str, Expected | None]:
    """(the repro command, the expected failure) from a REPRODUCE reply; "" / None when missing."""
    text = str(reply or "")
    command = ""
    found = _REPRO_RE.findall(text)
    if found:
        command = found[-1].strip().strip("`").strip()
    expected = None
    stated = _EXPECTED_RE.findall(text)
    if stated:
        expected = parse_expected(stated[-1])
    return command, expected


def parse_expected(raw: str) -> Expected:
    raw = raw.strip().strip("`").strip()
    match = _ERROR_TYPE_RE.search(raw)
    kind = match.group(1) if match else ""
    message = raw
    if match:
        message = (raw[: match.start()] + raw[match.end():]).strip(" :-–")
    return Expected(kind=kind, message=message, raw=raw)


@dataclass(frozen=True)
class ReproVerdict:
    accepted: bool
    reason: str
    #: Error types seen in the output.
    seen: tuple[str, ...] = ()


def judge_repro(expected: Expected | None, exit_code: int | None, output: str) -> ReproVerdict:
    """Does the repro fail on unmodified code, for the stated reason?

    It must fail (a non-zero exit). With an expected error type, that type must be in the
    output; a setup error (ImportError, SyntaxError, ...) when another type was expected is
    "the wrong reason". With an expected message, at least half of its words must appear.
    """
    text = str(output or "")
    seen = tuple(dict.fromkeys(_ERROR_TYPE_RE.findall(text)))
    if expected is None:
        return ReproVerdict(False, "no expected failure was stated (EXPECTED: <error type>: <message>)", seen)
    if exit_code == 0:
        return ReproVerdict(False, "the repro passed on the unmodified code, so it does not reproduce the problem", seen)
    if exit_code is None:
        return ReproVerdict(False, "the repro did not run", seen)
    lowered = text.lower()
    if expected.kind:
        if expected.kind.lower() not in lowered:
            setup = [kind for kind in seen if kind in SETUP_ERRORS]
            if setup:
                return ReproVerdict(False, f"it fails for the wrong reason: {setup[0]} (the repro is broken), "
                                           f"not the expected {expected.kind}", seen)
            other = f" ({', '.join(seen[:3])} instead)" if seen else ""
            return ReproVerdict(False, f"it fails, but not with the expected {expected.kind}{other}", seen)
    elif _SETUP_TEXT_RE.search(text) or any(kind in SETUP_ERRORS for kind in seen):
        bad = next((kind for kind in seen if kind in SETUP_ERRORS), "a setup error")
        return ReproVerdict(False, f"it fails for the wrong reason: {bad} (the repro is broken)", seen)
    words = expected.words
    if words:
        hits = sum(1 for word in words if word in lowered)
        if hits * 2 < len(words):
            return ReproVerdict(False, f"it fails, but the output does not match the expected message "
                                       f"({hits} of {len(words)} words found)", seen)
    return ReproVerdict(True, "it fails for the stated reason", seen)


# --------------------------------------------------------------------------- test output

_COUNT_PATTERNS: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], int]], ...] = (
    # pytest: "== 2 failed, 3 passed in 0.1s ==" / "1 failed, 1 error in 0.2s" (-q)
    (re.compile(r"^[= ]*((?:\d+ \w+,? ?)+) in [\d.]+s\b", re.M),
     lambda m: sum(int(n) for n in re.findall(r"(\d+) (?:failed|errors?)\b", m.group(1)))),
    # unittest: "FAILED (failures=2, errors=1)"
    (re.compile(r"FAILED \(([^)]*)\)"), lambda m: sum(int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", m.group(1)))),
    # JUnit / Gradle: "Tests run: 10, Failures: 2, Errors: 1" / "3 tests completed, 2 failed"
    (re.compile(r"Failures: (\d+), Errors: (\d+)"), lambda m: int(m.group(1)) + int(m.group(2))),
    (re.compile(r"\d+ tests? completed, (\d+) failed"), lambda m: int(m.group(1))),
    # Jest / Vitest: "Tests: 2 failed, 5 passed"
    (re.compile(r"Tests:\s+(\d+) failed"), lambda m: int(m.group(1))),
    # cargo: "test result: FAILED. 3 passed; 2 failed"
    (re.compile(r"test result: \w+\. \d+ passed; (\d+) failed"), lambda m: int(m.group(1))),
    # ctest: "50% tests passed, 2 tests failed out of 4"
    (re.compile(r"(\d+) tests? failed out of"), lambda m: int(m.group(1))),
)
_GO_FAIL_RE = re.compile(r"^--- FAIL: ", re.M)


def failing_count(output: str, ok: bool) -> int:
    """Failing tests in a test run's output; 0 when it passed, 1 when it failed and no count is found."""
    text = str(output or "")
    total = 0
    found = False
    for pattern, value in _COUNT_PATTERNS:
        matches = list(pattern.finditer(text))
        if matches:
            total = max(total, value(matches[-1]))
            found = True
    go = len(_GO_FAIL_RE.findall(text))
    if go:
        total = max(total, go)
        found = True
    if ok:
        return 0
    return total if found and total > 0 else 1


_TEST_COMMAND_RE = re.compile(
    r"\b(pytest|unittest|tox|nox|go test|cargo test|ctest|atest|jest|vitest|mocha|npm (?:run )?test|yarn test"
    r"|pnpm test|gradlew?\b[^\n|;&]*?(?:[Tt]est|check)|mvn (?:\S+ )*test|make (?:test|check)|rspec|phpunit|dotnet test"
    r"|swift test|mix test)\b",
    re.I,
)


def looks_like_tests(command: str) -> bool:
    return bool(_TEST_COMMAND_RE.search(str(command or "")))


# --------------------------------------------------------------------------- selection

_SKIP_DIRS = frozenset({".git", ".crit", ".bot", "node_modules", "build", "out", "dist", "target", ".gradle",
                        "__pycache__", ".venv", "venv", ".tox", ".idea", ".repo"})
MAX_WALK_FILES = 20_000
MAX_SELECTED_FILES = 12
_TEST_DIR_NAMES = frozenset({"test", "tests", "testing", "spec", "specs", "__tests__", "androidTest", "testFixtures"})


def is_test_path(rel: str) -> bool:
    """A test file or a file under a test folder (or the repro folder)."""
    path = PurePosixPath(str(rel).replace("\\", "/"))
    if str(path).startswith(".crit/repro/"):
        return True
    if any(part in _TEST_DIR_NAMES for part in path.parts[:-1]):
        return True
    name = path.name
    stem = name.split(".", 1)[0]
    return bool(
        name.startswith("test_") or stem.endswith("_test") or stem.endswith("Test") or stem.endswith("Tests")
        or stem.endswith("_spec") or ".test." in name or ".spec." in name
    )


def _candidate_names(rel: str) -> set[str]:
    path = PurePosixPath(rel)
    stem = path.name.split(".", 1)[0]
    ext = path.suffix
    names = {
        f"test_{stem}{ext}", f"{stem}_test{ext}", f"{stem}Test{ext}", f"{stem}Tests{ext}",
        f"{stem}.test{ext}", f"{stem}.spec{ext}", f"{stem}_spec{ext}",
    }
    if ext in {".kt", ".java"}:
        names |= {f"{stem}Test.kt", f"{stem}Test.java"}
    if ext in {".ts", ".tsx", ".js", ".jsx"}:
        names |= {f"{stem}.test.ts", f"{stem}.test.js", f"{stem}.spec.ts", f"{stem}.spec.js"}
    if ext in {".c", ".cc", ".cpp", ".h", ".hpp"}:
        names |= {f"{stem}_test.cc", f"{stem}_test.cpp", f"{stem}_unittest.cc", f"{stem}_unittest.cpp"}
    return names


def walk_files(workspace: Path, limit: int = MAX_WALK_FILES) -> list[str]:
    """Workspace-relative files, skipping build output and vendored folders; at most ``limit``."""
    root = Path(workspace)
    found: list[str] = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            found.append(Path(folder, name).relative_to(root).as_posix())
            if len(found) >= limit:
                return found
    return found


def tests_for_paths(changed: Iterable[str], files: Iterable[str]) -> list[str]:
    """Test files that, by name, belong to the changed files; changed test files are their own tests."""
    changed = [str(item) for item in changed]
    wanted: dict[str, str] = {}
    for rel in changed:
        if is_test_path(rel):
            continue
        for name in _candidate_names(rel):
            wanted.setdefault(name, rel)
    selected = [rel for rel in changed if is_test_path(rel) and not rel.startswith(".crit/")]
    for rel in files:
        if PurePosixPath(rel).name in wanted and rel not in selected:
            selected.append(rel)
    return selected[:MAX_SELECTED_FILES]


_GRAPH_LINE_RE = re.compile(r"^\s*(?:<-\s*)?([\w./-]+\.\w+)")


def tests_from_graph(changed: Iterable[str], nav: Callable[[str], str] | None) -> list[str]:
    """Test files that import the changed files, from the index (``nav(path)`` returns code_nav's
    imported_by text). Empty without an index."""
    if nav is None:
        return []
    out: list[str] = []
    for rel in changed:
        if is_test_path(rel):
            continue
        try:
            text = nav(rel) or ""
        except Exception:  # noqa: BLE001 - the graph is a bonus; selection works without it
            continue
        for line in text.splitlines():
            match = _GRAPH_LINE_RE.match(line)
            if match and is_test_path(match.group(1)) and match.group(1) not in out:
                out.append(match.group(1))
    return out[:MAX_SELECTED_FILES]


@dataclass(frozen=True)
class TestTarget:
    """One command the flow runs before and after the change."""

    label: str
    command: str
    #: Where it came from: check (AGENT.md / check_command), files (by name), graph (imports), repro.
    source: str
    files: tuple[str, ...] = ()


def _python() -> str:
    return "python" if shutil.which("python") else "python3"


def _uses_pytest(workspace: Path) -> bool:
    root = Path(workspace)
    if (root / "pytest.ini").is_file() or (root / "conftest.py").is_file():
        return True
    for name, marker in (("pyproject.toml", "[tool.pytest"), ("setup.cfg", "[tool:pytest"), ("tox.ini", "[pytest")):
        try:
            if marker in (root / name).read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def commands_for(test_files: list[str], workspace: Path) -> list[TestTarget]:
    """Commands that run these test files, for the languages crit can run one file of.

    Python (pytest when the project uses it, else unittest discovery per folder) and Go
    (the package). Other languages run through the project's test command only.
    """
    targets: list[TestTarget] = []
    python = [rel for rel in test_files if rel.endswith(".py")]
    if python:
        if _uses_pytest(workspace):
            targets.append(TestTarget("tests for the changed files", f"{_python()} -m pytest -q " + " ".join(python),
                                      "files", tuple(python)))
        else:
            for rel in python:
                path = PurePosixPath(rel)
                folder = str(path.parent) if str(path.parent) != "." else "."
                targets.append(TestTarget(
                    f"{rel}", f"{_python()} -m unittest discover -s {folder} -p {path.name}", "files", (rel,)))
    go_dirs = sorted({str(PurePosixPath(rel).parent) for rel in test_files if rel.endswith("_test.go")})
    for folder in go_dirs:
        targets.append(TestTarget(f"go package {folder}", f"go test ./{folder}", "files",
                                  tuple(rel for rel in test_files if rel.startswith(folder + "/"))))
    return targets


@dataclass
class Selection:
    targets: list[TestTarget] = field(default_factory=list)
    #: Test files found for the changed files that no target runs on its own (Java, Kotlin, JS ...).
    unrun: list[str] = field(default_factory=list)


def select_tests(
    workspace: Path,
    changed: list[str],
    *,
    check_command: str | None,
    nav: Callable[[str], str] | None = None,
    files: list[str] | None = None,
) -> Selection:
    """The selected set: the project's test command, then tests for the changed files."""
    selection = Selection()
    if check_command:
        selection.targets.append(TestTarget("project tests", check_command, "check"))
    source_changes = [rel for rel in changed if not rel.startswith(".crit/")]
    if not source_changes:
        return selection
    listing = files if files is not None else walk_files(workspace)
    found = tests_for_paths(source_changes, listing)
    for rel in tests_from_graph(source_changes, nav):
        if rel not in found:
            found.append(rel)
    runnable = commands_for(found, workspace)
    covered = {rel for target in runnable for rel in target.files}
    selection.targets.extend(runnable)
    selection.unrun = [rel for rel in found if rel not in covered]
    return selection


@dataclass
class TestResult:
    target: TestTarget
    ok: bool
    failing: int
    output: str = ""
    #: The command could not run (denied, missing tool): not a failure of the code.
    skipped: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.target.label, "command": self.target.command, "source": self.target.source,
                "ok": self.ok, "failing": self.failing, "skipped": self.skipped or None}


def regressions(before: dict[str, TestResult], after: dict[str, TestResult]) -> list[str]:
    """Labels of targets that passed before and fail after."""
    out = []
    for label, result in after.items():
        old = before.get(label)
        if old is not None and old.ok and not old.skipped and not result.ok and not result.skipped:
            out.append(label)
    return out
