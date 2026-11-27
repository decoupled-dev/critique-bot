#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build a Windows or Linux deployment zip with PyInstaller.

Must be run on the OS you want to ship (Linux build != Windows .exe).

    python -m pip install -e ".[packaging]"
    python scripts/build.py
"""

from __future__ import annotations

import argparse
import platform
import struct
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "crit.spec"
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
    if machine in {"x86_64", "amd64"} and struct.calcsize("P") == 4:
        arch = "x86"  # 32-bit Python on a 64-bit OS builds a 32-bit bundle
    elif machine in {"x86_64", "amd64"}:
        arch = "x64"
    elif machine in {"aarch64", "arm64"}:
        arch = "arm64"
    else:
        arch = machine or "unknown"
    return f"{os_name}-{arch}"


def _binary_name() -> str:
    name = "crit.exe" if sys.platform == "win32" else "crit"
    return name


def _run_pyinstaller() -> Path:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    staged = DIST / "crit"
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
    # crit reads prompts, skills, model profiles and built-in agents only from its own bundle: every one must be there.
    for pattern, sub in (("prompts/*.txt", "prompts"), ("skills/*/SKILL.md", "skills"), ("profiles/**/*.json", "profiles"), ("agents/*.md", "agents")):
        for source in sorted((ROOT / "src" / "critique_bot").glob(pattern)):
            shipped = staged / "_internal" / "critique_bot" / source.relative_to(ROOT / "src" / "critique_bot")
            if not shipped.is_file():
                raise SystemExit(f"{source.name} ({sub}) is missing from the bundle: {shipped}")
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
    if result.returncode != 0 or "crit" not in (result.stdout + result.stderr):
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(f"{binary} --help failed with exit {result.returncode}")
    print(f"ok: {binary} --help")


def _assemble_zip(staged: Path, version: str, tag: str) -> Path:
    bundle_name = f"crit-{version}-{tag}"
    payload = DIST / bundle_name
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True)

    binary_name = _binary_name()
    shutil.copy2(staged / binary_name, payload / binary_name)
    internal = staged / "_internal"
    if internal.is_dir():
        shutil.copytree(internal, payload / "_internal", symlinks=True)
    # The zip holds only the program: the exe and its _internal folder (which already
    # carries LICENSE, NOTICE, THIRD_PARTY_NOTICES.md, and the prompts). Running the exe
    # installs it; examples and the worker-service files live in the repo's docs.

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
    print(f"building crit {version} for {tag}")
    staged = _run_pyinstaller()
    binary = staged / _binary_name()
    if not args.skip_smoke:
        _smoke_test(binary)
    zip_path = _assemble_zip(staged, version, tag)
    print(f"bundle ready: {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
