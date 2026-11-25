"""``critique-bot install``: put the downloaded program where every terminal finds it.

Like other developer CLIs, crit lives in the user's own folder and needs no administrator
rights. Everything goes in crit's one folder (``config.crit_home()``: ``~/.crit``,
``%USERPROFILE%\\.crit``, or ``CRIT_HOME``):

* ``app/<version>/`` holds the program;
* ``bin/`` holds the launchers (``crit.cmd`` and ``critique-bot.cmd`` on Windows, ``crit`` and
  ``critique-bot`` elsewhere) and is added to PATH: the user's PATH in the registry on
  Windows, ``~/.profile`` (and ``~/.bashrc``, ``~/.zshrc``) elsewhere, when it is not there yet.

Versions before 1.1 installed into ``%LOCALAPPDATA%\\Programs\\critique-bot`` or
``~/.local/share/critique-bot`` with launchers in ``~/.local/bin``; the next install removes
that copy, the launchers that point at it, and (Windows) its bin folder on PATH.

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
    """``app`` in crit's folder: one ``<version>`` folder per installed version."""
    from critique_bot.config import crit_home

    return crit_home() / "app"


def bin_dir() -> Path:
    """``bin`` in crit's folder: the launchers, on PATH."""
    from critique_bot.config import crit_home

    return crit_home() / "bin"


def legacy_install_root() -> Path:
    """Where versions before 1.1 installed the program."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Programs" / "critique-bot"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "critique-bot"


def legacy_bin_dir() -> Path:
    if os.name == "nt":
        return legacy_install_root() / "bin"
    return Path.home() / ".local" / "bin"


#: Where the .deb puts the program; apt manages it, so it counts as installed.
DEB_ROOT = Path("/opt/critique-bot")
#: Set to 1 to keep a downloaded program from installing itself when it runs.
ENV_NO_AUTO_INSTALL = "CRIT_NO_AUTO_INSTALL"


def is_installed_copy(bundle: Path | None = None) -> bool:
    """True when the running program is the installed one (or the .deb's, in /opt/critique-bot)."""
    bundle = bundle or frozen_bundle_dir()
    if bundle is None:
        return False
    if os.name != "nt":
        try:
            bundle.resolve().relative_to(DEB_ROOT)
            return True
        except ValueError:
            pass
    try:
        bundle.resolve().relative_to(install_root().resolve())
    except ValueError:
        return False
    return True


def _version_key(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def installed_versions() -> list[str]:
    """The versions in the install folder (``app-<version>`` with the program in it)."""
    found = []
    root = install_root()
    if not root.is_dir():
        return found
    for app in root.iterdir():
        if not app.name.startswith(".") and (app / _exe_name()).is_file():
            found.append(app.name)
    return sorted(found, key=_version_key)


def should_auto_install(argv: list[str], bundle: Path | None = None) -> bool:
    """True when the downloaded program should install itself before doing what was asked."""
    bundle = bundle or frozen_bundle_dir()
    if bundle is None or is_installed_copy(bundle):
        return False
    if os.environ.get(ENV_NO_AUTO_INSTALL, "").strip() not in ("", "0"):
        return False
    if argv[:1] in (["install"], ["--help"], ["-h"]):
        return False
    mine = _version_key(__version__)
    return not any(_version_key(version) >= mine for version in installed_versions())


def _say_stderr(text: str) -> None:
    print(text, file=sys.stderr)  # stdout may be a protocol (crit --acp)


def auto_install(*, say=_say_stderr) -> bool:
    """Install the running program without a question; one line says where. True when it worked."""
    lines: list[str] = []
    code = install(say=lines.append)
    if code != 0:
        for line in lines:
            log.notice(line)
        return False
    say(f"Installed crit {__version__} in {install_root() / __version__} and added it to your PATH.")
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


def _windows_remove_from_path(folder: Path) -> bool:
    """Drop ``folder`` from the user's PATH (HKCU\\Environment). True when it was there."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except OSError:
            return False
        parts = [p for p in str(current).split(";") if p]
        mine = os.path.normcase(str(folder)).rstrip("\\")
        kept = [p for p in parts if os.path.normcase(os.path.expandvars(p)).rstrip("\\") != mine]
        if len(kept) == len(parts):
            return False
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(kept))
    return True


def remove_legacy_install(*, running: Path | None = None) -> list[Path]:
    """Remove the copy versions before 1.1 installed: the launchers in the old bin folder that
    point at the old program folder (others are left), the old bin folder on the Windows PATH,
    and the old program folder unless crit is running from it. The ``~/.profile`` lines stay
    (harmless). What was removed."""
    root = legacy_install_root()
    folder = legacy_bin_dir()
    removed: list[Path] = []
    names = ("crit.cmd", "critique-bot.cmd") if os.name == "nt" else ("crit", "critique-bot")
    for name in names:
        path = folder / name
        try:
            if path.is_file() and str(root) in path.read_text(encoding="utf-8", errors="replace"):
                path.unlink()
                removed.append(path)
        except OSError as exc:
            log.info(f"left {path}: {exc}")
    if os.name == "nt" and root.is_dir():
        try:
            _windows_remove_from_path(folder)
        except OSError as exc:
            log.info(f"could not take {folder} off PATH: {exc}")
    if root.is_dir():
        inside = False
        if running is not None:
            try:
                running.resolve().relative_to(root.resolve())
                inside = True
            except ValueError:
                pass
        if not inside:
            shutil.rmtree(root, ignore_errors=True)
            if not root.exists():
                removed.append(root)
    return removed


def _remove_old_versions(keep: Path) -> None:
    root = install_root()
    if not root.is_dir():
        return
    for old in root.iterdir():
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
    from critique_bot.config import ensure_crit_home

    ensure_crit_home()
    root = install_root()
    app = root / __version__
    if bundle.resolve() != app.resolve():
        staging = root / f".{__version__}.partial"
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
    remove_legacy_install(running=bundle)
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
    say("crit asks how to reach the model the first time it runs (or set it with crit config; see crit config --help).")
    return 0


def set_config_file(path: str, *, say=print) -> int:
    """``install --config-file PATH``: store the config file in the user config (checked first)."""
    from critique_bot.config import user_config_path
    from critique_bot.config_cmd import main as config_main

    return config_main(["--config", str(user_config_path()), "--config-file", path])


def uninstall(*, say=print) -> int:
    """Remove the launchers and program folders (settings and project .bot folders stay)."""
    for name in ("crit.cmd", "critique-bot.cmd") if os.name == "nt" else ("crit", "critique-bot"):
        path = bin_dir() / name
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            if str(install_root()) in text:
                path.unlink()
    shutil.rmtree(install_root(), ignore_errors=True)
    from critique_bot.config import crit_home

    say(f"Removed crit. Your settings and browser sign-in are kept in {crit_home()}; delete that folder to remove them too.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="critique-bot install",
        description="Install crit for this user so every terminal finds it (no administrator rights needed).",
    )
    parser.add_argument("--uninstall", action="store_true", help="remove the installed program and launchers")
    parser.add_argument("--config-file", metavar="PATH", help="after installing, use this config file (url, page elements, ...)")
    args = parser.parse_args(argv)
    if args.uninstall:
        return uninstall()
    code = install()
    if code == 0 and args.config_file:
        code = set_config_file(args.config_file)
    return code
