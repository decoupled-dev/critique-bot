"""Knowledge-graph lookup for ``code_graph``.

CodeGraph (https://github.com/colbymchenry/codegraph) ships with this app.
The official v1.6.2 archives live in ``vendor/codegraph`` and are extracted
locally. Nothing is downloaded. A frozen build also looks beside the
executable. ``init`` builds ``.codegraph/`` once, and ``sync`` updates it
after files change. One ``explore`` call returns the symbols, their source,
and the call path.

Graphify (https://github.com/Graphify-Labs/graphify) is a fallback when its
CLI is the only graph tool a caller injected and ``graphify-out/graph.json``
already exists. It is not built automatically: that pass can read docs with a
model. Code extraction stays on this machine.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Callable

from critique_bot import agent_shell, log

_ACTIONS = {"explore", "callers", "callees", "impact", "explain", "path"}
_VENDORED_VERSION = "1.6.2"
_VERSION = re.compile(r"^v?\d+\.\d+\.\d+$")


def detect(which: Callable[[str], str | None] | None = None) -> str:
    """``codegraph`` (the app's copy), ``graphify``, or ``""`` for an injected miss."""
    if which is not None:
        if which("codegraph"):
            return "codegraph"
        if which("graphify"):
            return "graphify"
        return ""
    return "codegraph"


def hint(workspace: Path, which: Callable[[str], str | None] | None = None) -> str:
    """One ENVIRONMENT line describing the graph the model can query."""
    backend = detect(which)
    root = Path(workspace)
    if backend == "codegraph":
        if (root / ".codegraph").is_dir():
            state = "ready"
        elif which is None:
            state = "included with this app; built on the first task, then updated after each edit"
        else:
            state = "built on the first task, then updated after each edit"
        return (
            f"code graph: codegraph ({state}). "
            "Use code_graph before search_code when the question is how code connects."
        )
    if backend == "graphify" and (root / "graphify-out" / "graph.json").is_file():
        return "code graph: graphify (graphify-out/graph.json). Use code_graph to query it."
    if backend == "graphify":
        return "code graph: graphify is installed but this project has no graphify-out/graph.json yet."
    return "code graph: not installed. search_code uses the local symbol index. Install codegraph to query call paths."


def install_dir() -> Path:
    """App-owned CodeGraph directory, separate from a user-wide ``codegraph`` install."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "critique-bot" / "codegraph" / "current"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "critique-bot"
            / "codegraph"
            / "current"
        )
    return Path.home() / ".config" / "critique-bot" / "codegraph" / "current"


def shipped_dirs() -> list[Path]:
    """Places the app looks before extracting: beside a frozen exe, then the app data dir."""
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent / "codegraph")
    dirs.append(install_dir())
    return dirs


def launcher(root: Path) -> Path | None:
    names = ("codegraph.cmd", "codegraph.exe", "codegraph") if sys.platform == "win32" else ("codegraph",)
    for name in names:
        path = Path(root) / "bin" / name
        if path.is_file():
            return path
    return None


def resolve(which: Callable[[str], str | None] | None = None, *, download: bool = False) -> str:
    """Path to the CodeGraph launcher. An injected ``which`` never extracts or searches."""
    if which is not None:
        return which("codegraph") or ""
    for root in shipped_dirs():
        found = launcher(root)
        if found is not None:
            return str(found)
    on_path = shutil.which("codegraph") or ""
    if on_path or not download:
        return on_path
    return str(install_bundle(install_dir()))


def platform_target() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        arch = "arm64"
    elif machine in {"x86_64", "amd64", "x64"}:
        arch = "x64"
    else:
        raise OSError(f"CodeGraph has no build for {platform.machine()}")
    if sys.platform == "win32":
        return f"win32-{arch}"
    if sys.platform == "darwin":
        return f"darwin-{arch}"
    return f"linux-{arch}"


def archive_name(target: str) -> str:
    if target.startswith("win32-"):
        return f"codegraph-{target}.zip"
    return f"codegraph-{target}.tar.gz"


def release_url(version: str, target: str) -> str:
    return (
        "https://github.com/colbymchenry/codegraph/releases/download/"
        f"{_tag(version)}/{archive_name(target)}"
    )


def vendor_dirs(version: str = "") -> list[Path]:
    """Directories that may hold the CodeGraph archives shipped in this repo."""
    tag = _pinned(version)
    relative = Path("vendor") / "codegraph" / tag
    found: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            return
        seen.add(key)
        found.append(path)

    for parent in Path(__file__).resolve().parents:
        add(parent / relative)
    if getattr(sys, "frozen", False):
        add(Path(sys.executable).resolve().parent / relative)
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            add(Path(meipass) / relative)
    return found


def vendored_archive(target: str, version: str = "") -> Path:
    """Local release archive for ``target``. Never contacts the network."""
    name = archive_name(target)
    for root in vendor_dirs(version):
        path = root / name
        if path.is_file():
            return path
    tag = _pinned(version)
    raise OSError(
        f"CodeGraph {name} ({tag}) is not in this app. "
        "The copy ships in vendor/codegraph and is not downloaded."
    )


def install_bundle(dest: Path, *, version: str = "") -> Path:
    """Extract the CodeGraph build shipped in this repo into ``dest`` and return its launcher."""
    dest = Path(dest)
    found = launcher(dest)
    if found is not None:
        return found
    archive = vendored_archive(platform_target(), version)
    _verify_sha256(archive)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.with_name(dest.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    log.print_safe("Installing the CodeGraph copy shipped with this app...", file=sys.stderr, flush=True)
    try:
        _extract(archive, staging)
        if launcher(staging) is None:
            raise OSError("CodeGraph archive did not contain bin/codegraph")
        if dest.exists():
            shutil.rmtree(dest)
        staging.rename(dest)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    found = launcher(dest)
    if found is None:
        raise OSError("CodeGraph install is missing its launcher")
    if sys.platform != "win32":
        found.chmod(found.stat().st_mode | 0o111)
    return found


def _pinned(version: str) -> str:
    pinned = version.strip() or os.environ.get("CODEGRAPH_VERSION", "").strip() or _VENDORED_VERSION
    return _tag(pinned)


def prepare(
    workspace: Path,
    *,
    runner: Callable[..., Any] | None = None,
    timeout: float = 120,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """Build the CodeGraph index once, or sync it. Empty when there is nothing to do."""
    backend = detect(which)
    root = Path(workspace)
    if backend == "codegraph":
        try:
            exe = resolve(which, download=which is None)
        except OSError as exc:
            if which is None and shutil.which("graphify") and (root / "graphify-out" / "graph.json").is_file():
                return "code graph: graphify ready"
            return "code graph: CodeGraph could not be installed. Use search_code.\n" + str(exc)
        if not exe:
            return "code graph: CodeGraph could not be installed. Use search_code."
        if (root / ".codegraph").is_dir():
            code, out = _run(root, _command(exe, ["sync", str(root)]), timeout=min(timeout, 45), runner=runner)
            if code == 0:
                return "code graph: codegraph synced"
            return "code graph: codegraph sync failed. Use search_code.\n" + _brief(out)
        if runner is None:
            log.print_safe("Building the code graph for this project...", file=sys.stderr, flush=True)
        code, out = _run(root, _command(exe, ["init", str(root), "--yes"]), timeout=timeout, runner=runner)
        if code == 0:
            return "code graph: codegraph built"
        return "code graph: codegraph init failed. Use search_code.\n" + _brief(out)
    if backend == "graphify" and (root / "graphify-out" / "graph.json").is_file():
        return "code graph: graphify ready"
    return ""


def sync_after_edit(
    workspace: Path,
    *,
    runner: Callable[..., Any] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> None:
    """Incremental update after a write. No-op until an index exists."""
    root = Path(workspace)
    if detect(which) != "codegraph" or not (root / ".codegraph").is_dir():
        return
    exe = resolve(which, download=False)
    if exe:
        _run(root, _command(exe, ["sync", str(root)]), timeout=20, runner=runner)


def query(
    workspace: Path,
    text: str,
    *,
    action: str = "explore",
    target: str = "",
    runner: Callable[..., Any] | None = None,
    timeout: float = 60,
    which: Callable[[str], str | None] | None = None,
) -> tuple[int, str]:
    """Run one graph query. The exit code is 127 when no CLI is installed."""
    question = " ".join(text.split())
    if not question:
        return 2, "query is required"
    if len(question) > 500:
        question = question[:500]
    kind = action.strip().lower() or "explore"
    if kind not in _ACTIONS:
        return 2, "action must be explore, callers, callees, impact, explain, or path"
    backend = detect(which)
    exe = ""
    if backend == "codegraph":
        try:
            exe = resolve(which, download=which is None and runner is None)
        except OSError:
            exe = ""
        if not exe and which is None and shutil.which("graphify"):
            backend = "graphify"
            exe = ""
        elif not exe and runner is None:
            return 127, (
                "CodeGraph could not be installed with this app. "
                "Use search_code meanwhile."
            )
        if not exe:
            exe = "codegraph"
    elif not backend and runner is None:
        return 127, (
            "no code graph. Install CodeGraph (codegraph init in this project) "
            "or build a Graphify graph (graphify-out/graph.json), then use search_code meanwhile."
        )
    elif not backend:
        backend = "codegraph"
        exe = "codegraph"
    root = Path(workspace)
    if backend == "graphify":
        argv = _graphify_argv(root, kind, question, target)
    else:
        argv = _codegraph_argv(exe or "codegraph", kind, question, target)
    if argv is None:
        return 2, "path needs query and target, the two symbols to connect"
    return _run(root, argv, timeout=timeout, runner=runner)


def _codegraph_argv(exe: str, action: str, query: str, target: str) -> list[str] | None:
    if action == "path":
        if not target.strip():
            return None
        return _command(exe, ["explore", f"how does {query} reach {target.strip()}"])
    command = {"explain": "explore"}.get(action, action)
    return _command(exe, [command, query])


def _graphify_argv(workspace: Path, action: str, query: str, target: str) -> list[str] | None:
    del workspace
    if action == "path":
        if not target.strip():
            return None
        return ["graphify", "path", query, target.strip()]
    if action == "explain":
        return ["graphify", "explain", query]
    return ["graphify", "query", query]


def _run(
    workspace: Path,
    argv: list[str],
    *,
    timeout: float,
    runner: Callable[..., Any] | None,
) -> tuple[int, str]:
    if runner is not None:
        try:
            proc = runner(
                argv,
                cwd=str(workspace),
                capture_output=True,
                check=False,
                timeout=timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            return 124, agent_shell.tidy(agent_shell.decode(exc.stdout) + "\n" + agent_shell.decode(exc.stderr))
        code = int(getattr(proc, "returncode", 1) or 0)
        text = agent_shell.decode(getattr(proc, "stdout", b""))
        err = agent_shell.decode(getattr(proc, "stderr", b""))
        body = agent_shell.tidy("\n".join(part for part in (text, err) if part))
        return code, body or f"exit {code}"
    try:
        proc = subprocess.run(
            argv,
            cwd=str(workspace),
            capture_output=True,
            check=False,
            timeout=timeout,
            shell=False,
            env=agent_shell.environment(),
        )
    except subprocess.TimeoutExpired as exc:
        return 124, agent_shell.tidy(agent_shell.decode(exc.stdout) + "\n" + agent_shell.decode(exc.stderr))
    except FileNotFoundError:
        return 127, f"{argv[0]} is not on PATH"
    text = agent_shell.tidy(agent_shell.decode(proc.stdout))
    err = agent_shell.tidy(agent_shell.decode(proc.stderr))
    body = "\n".join(part for part in (text, err) if part)
    return int(proc.returncode or 0), body or f"exit {proc.returncode}"


def _brief(text: str) -> str:
    line = text.strip().splitlines()
    return line[-1][:240] if line else "no output"


def _tag(version: str) -> str:
    text = version.strip()
    if not _VERSION.fullmatch(text):
        raise OSError(f"CodeGraph version {version!r} is not a release tag")
    return text if text.startswith("v") else f"v{text}"


def _command(exe: str, args: list[str]) -> list[str]:
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/d", "/s", "/c", exe, *args]
    return [exe, *args]


def _verify_sha256(archive: Path) -> None:
    sums = archive.parent / "SHA256SUMS"
    if not sums.is_file():
        raise OSError(f"CodeGraph checksum file is missing beside {archive.name}")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    expected = ""
    for line in sums.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == archive.name:
            expected = parts[0].lower()
            break
    if not expected:
        raise OSError(f"CodeGraph checksum file has no entry for {archive.name}")
    if digest != expected:
        raise OSError(f"CodeGraph archive {archive.name} failed its checksum")


def _safe_rel(name: str) -> str | None:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        return None
    text = path.as_posix()
    return text if text not in {"", "."} else None


def _extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        _extract_zip(archive, dest)
    else:
        _extract_tar(archive, dest)
    _hoist(dest)


def _extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            name = _safe_rel(info.filename)
            if name is None:
                continue
            target = dest / name
            if info.is_dir() or name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def _extract_tar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:*") as bundle:
        for member in bundle.getmembers():
            name = _safe_rel(member.name)
            if name is None:
                continue
            target = dest / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym() or member.islnk():
                link = member.linkname
                if Path(link).is_absolute() or ".." in Path(link).parts:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(link)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = bundle.extractfile(member)
            if extracted is None:
                continue
            with extracted, target.open("wb") as out:
                shutil.copyfileobj(extracted, out)
            target.chmod(member.mode & 0o777)


def _hoist(dest: Path) -> None:
    if launcher(dest) is not None:
        return
    children = list(dest.iterdir())
    if len(children) != 1 or not children[0].is_dir() or launcher(children[0]) is None:
        return
    inner = children[0]
    for item in list(inner.iterdir()):
        item.rename(dest / item.name)
    inner.rmdir()
