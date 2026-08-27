#!/usr/bin/env python3
"""Build a Windows or Linux deployment zip with PyInstaller.

Must be run on the OS you want to ship (Linux build != Windows .exe).

    python -m pip install -e ".[packaging]"
    python scripts/build.py
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "critique-bot.spec"
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def _version() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from critique_bot import __version__

    return __version__


def _platform_tag() -> str:
    system = sys.platform
    if system == "win32":
        os_name = "windows"
    elif system.startswith("linux"):
        os_name = "linux"
    elif system == "darwin":
        os_name = "macos"
    else:
        os_name = system
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        arch = "x64"
    elif machine in {"aarch64", "arm64"}:
        arch = "arm64"
    else:
        arch = machine or "unknown"
    return f"{os_name}-{arch}"


def _binary_name() -> str:
    name = "critique-bot.exe" if sys.platform == "win32" else "critique-bot"
    return name


def _run_pyinstaller() -> Path:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    staged = DIST / "critique-bot"
    if staged.exists():
        shutil.rmtree(staged)
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC),
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
    ]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)
    binary = staged / _binary_name()
    if not binary.is_file():
        raise SystemExit(f"PyInstaller did not produce {binary}")
    driver_name = "node.exe" if sys.platform == "win32" else "node"
    driver = staged / "_internal" / "playwright" / "driver" / driver_name
    if not driver.is_file():
        raise SystemExit(f"Playwright driver missing from bundle: {driver}")
    return staged


def _smoke_test(binary: Path) -> None:
    cmd = [str(binary), "--help"]
    print("+", " ".join(cmd), flush=True)
    result = subprocess.run(
        cmd,
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0 or "critique-bot" not in (result.stdout + result.stderr):
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(f"{binary} --help failed with exit {result.returncode}")
    print(f"ok: {binary} --help")


def _write_readme(dest: Path, binary_name: str) -> None:
    invoke = f".\\{binary_name}" if sys.platform == "win32" else f"./{binary_name}"
    dest.write_text(
        "\n".join(
            [
                "critique-bot standalone bundle",
                "",
                "Requires Microsoft Edge on this machine.",
                "Copy config.example.json to config.json and fill in the chat URL",
                "and CSS selectors. First login: run with --headed, then reuse",
                "the .edge-profile directory on later runs.",
                "",
                "Review a patch:",
                f"  {invoke} --config config.json --patch-file diff.patch",
                "",
                "General prompt:",
                f'  {invoke} --config config.json --mode general '
                '--prompt "Summarize this" notes.txt',
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _assemble_zip(staged: Path, version: str, tag: str) -> Path:
    bundle_name = f"critique-bot-{version}-{tag}"
    payload = DIST / bundle_name
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True)

    binary_name = _binary_name()
    shutil.copy2(staged / binary_name, payload / binary_name)
    internal = staged / "_internal"
    if internal.is_dir():
        shutil.copytree(internal, payload / "_internal", symlinks=True)
    shutil.copy2(ROOT / "config.example.json", payload / "config.example.json")
    prompts_src = ROOT / "prompts"
    if prompts_src.is_dir():
        shutil.copytree(prompts_src, payload / "prompts")
    _write_readme(payload / "README.txt", binary_name)

    zip_path = DIST / f"{bundle_name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(payload.parent).as_posix())
    print(f"wrote {zip_path} ({zip_path.stat().st_size} bytes)")
    return zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="do not run the bundled binary --help after the build",
    )
    args = parser.parse_args(argv)

    if not SPEC.is_file():
        raise SystemExit(f"missing spec: {SPEC}")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit(
            'PyInstaller is required. Install with: python -m pip install -e ".[packaging]"'
        ) from None

    version = _version()
    tag = _platform_tag()
    print(f"building critique-bot {version} for {tag}")
    staged = _run_pyinstaller()
    binary = staged / _binary_name()
    if not args.skip_smoke:
        _smoke_test(binary)
    zip_path = _assemble_zip(staged, version, tag)
    print(f"bundle ready: {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
