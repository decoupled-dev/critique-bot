"""``critique-bot install``: put the downloaded program where every terminal finds it.

Like other developer CLIs, crit lives in the user's own folders and needs no
administrator rights:

* Windows: ``%LOCALAPPDATA%\\Programs\\critique-bot\\app-<version>\\`` holds the program,
  ``...\\critique-bot\\bin\\`` holds ``crit.cmd`` and ``critique-bot.cmd``, and that bin
  folder is added to the user's PATH.
* Linux and macOS: ``~/.local/share/critique-bot/app-<version>/`` holds the program and
  ``~/.local/bin/crit`` and ``critique-bot`` start it; ``~/.local/bin`` is added to PATH
  in ``~/.profile`` (and ``~/.bashrc``) when it is not there yet.

Each version gets its own folder, so installing a new one works while the old one is
running; the launchers then point at the new one and older folders are removed when
they are not in use. Settings are not part of the install: crit asks for them the first
time it needs them (or ``crit config``), and they stay where they are across updates.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path

from critique_bot import __version__, log

_PATH_MARK = "# added by critique-bot install"


def frozen_bundle_dir() -> Path | None:
    """The folder of the running standalone program, or None when running from Python sources."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def install_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Programs" / "critique-bot"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "critique-bot"


def bin_dir() -> Path:
    if os.name == "nt":
        return install_root() / "bin"
    return Path.home() / ".local" / "bin"


def is_installed_copy(bundle: Path | None = None) -> bool:
    """True when the running program is the installed one."""
    bundle = bundle or frozen_bundle_dir()
    if bundle is None:
        return False
    try:
        bundle.resolve().relative_to(install_root().resolve())
    except ValueError:
        return False
    return True


def _exe_name() -> str:
    return "critique-bot.exe" if os.name == "nt" else "critique-bot"


def _write_launchers(app: Path) -> list[Path]:
    """crit and critique-bot in the bin folder, pointing at ``app``."""
    folder = bin_dir()
    folder.mkdir(parents=True, exist_ok=True)
    exe = app / _exe_name()
    written = []
    if os.name == "nt":
        for name, extra in (("crit.cmd", "--as-crit "), ("critique-bot.cmd", "")):
            path = folder / name
            path.write_text(f'@"{exe}" {extra}%*\r\n', encoding="utf-8", newline="")
            written.append(path)
        return written
    for name, extra in (("crit", '--as-crit '), ("critique-bot", "")):
        path = folder / name
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(f'#!/bin/sh\nexec "{exe}" {extra}"$@"\n', encoding="utf-8")
        tmp.chmod(0o755)
        os.replace(tmp, path)
        written.append(path)
    return written


def _windows_add_to_path(folder: Path) -> bool:
    """Add ``folder`` to the user's PATH (HKCU\\Environment). True when it was added."""
    import ctypes
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except OSError:
            current, kind = "", winreg.REG_EXPAND_SZ
        parts = [p for p in str(current).split(";") if p]
        if any(os.path.normcase(os.path.expandvars(p)).rstrip("\\") == os.path.normcase(str(folder)).rstrip("\\") for p in parts):
            return False
        parts.append(str(folder))
        winreg.SetValueEx(key, "Path", 0, kind if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else winreg.REG_EXPAND_SZ, ";".join(parts))
    # Tell Explorer, so terminals opened from now on see the new PATH.
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))  # type: ignore[attr-defined]
    return True


def _posix_add_to_path(folder: Path) -> list[Path]:
    """Add ``folder`` to PATH in the shell start files when it is not on PATH. The files changed."""
    on_path = any(Path(p).expanduser() == folder for p in os.environ.get("PATH", "").split(os.pathsep) if p)
    if on_path:
        return []
    line = f'export PATH="{folder}:$PATH"  {_PATH_MARK}\n'
    changed = []
    for name in (".profile", ".bashrc", ".zshrc"):
        rc = Path.home() / name
        if name != ".profile" and not rc.exists():
            continue
        text = rc.read_text(encoding="utf-8", errors="replace") if rc.exists() else ""
        if _PATH_MARK in text:
            continue
        with rc.open("a", encoding="utf-8") as handle:
            handle.write(("\n" if text and not text.endswith("\n") else "") + line)
        changed.append(rc)
    return changed


def _remove_old_versions(keep: Path) -> None:
    for old in install_root().glob("app-*"):
        if old.resolve() == keep.resolve():
            continue
        try:
            shutil.rmtree(old)
        except OSError:
            log.info(f"left {old} (in use); it is removed on a later install")


def install(bundle: Path | None = None, *, say=print) -> int:
    """Copy the running program into the install folder, write the launchers, and set PATH."""
    bundle = bundle or frozen_bundle_dir()
    if bundle is None:
        say("Install works for the downloaded program (critique-bot.exe or the Linux bundle), not from source.")
        say("From source, use scripts/setup-crit.sh or scripts/setup-crit.ps1.")
        return 1
    if not (bundle / _exe_name()).is_file():
        say(f"Problem: {bundle} does not contain {_exe_name()}.")
        return 1
    root = install_root()
    app = root / f"app-{__version__}"
    if bundle.resolve() != app.resolve():
        staging = root / f".app-{__version__}.partial"
        if staging.exists():
            shutil.rmtree(staging)
        root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bundle, staging, symlinks=True)
        if app.exists():
            try:
                shutil.rmtree(app)
            except OSError:
                shutil.rmtree(staging, ignore_errors=True)
                say(f"Problem: crit {__version__} is installed and running; close it and install again.")
                return 1
        os.replace(staging, app)
        if os.name != "nt":
            exe = app / _exe_name()
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    _write_launchers(app)
    _remove_old_versions(app)
    folder = bin_dir()
    if os.name == "nt":
        _windows_add_to_path(folder)
        say(f"Installed crit {__version__} in {app}.")
        say("Open a new terminal (PowerShell or Command Prompt) in any project folder and type: crit")
    else:
        changed = _posix_add_to_path(folder)
        say(f"Installed crit {__version__} in {app}.")
        if changed:
            say(f"Added {folder} to PATH in {', '.join(str(p) for p in changed)}. Open a new terminal, then type: crit")
        else:
            say("In any project folder, type: crit")
    say("crit asks for your company's chat address the first time it needs it (or set it with: crit config --url ...).")
    return 0


def uninstall(*, say=print) -> int:
    """Remove the launchers and program folders (settings and project .bot folders stay)."""
    for name in ("crit.cmd", "critique-bot.cmd") if os.name == "nt" else ("crit", "critique-bot"):
        path = bin_dir() / name
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            if str(install_root()) in text:
                path.unlink()
    shutil.rmtree(install_root(), ignore_errors=True)
    say("Removed crit. Your settings are kept; delete them with: crit config --reset (before uninstalling) "
        "or remove the critique-bot folder in your user settings folder.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="critique-bot install",
        description="Install crit for this user so every terminal finds it (no administrator rights needed).",
    )
    parser.add_argument("--uninstall", action="store_true", help="remove the installed program and launchers")
    args = parser.parse_args(argv)
    if args.uninstall:
        return uninstall()
    return install()
