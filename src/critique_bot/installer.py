# SPDX-License-Identifier: Apache-2.0
"""``crit install`` and ``crit uninstall``: put the downloaded program where every terminal
finds it, and take everything away again.

Like other developer CLIs, crit lives in the user's own folder and needs no administrator
rights. Everything goes in crit's one folder (``config.crit_home()``: ``~/.crit``,
``%USERPROFILE%\\.crit``, or ``CRIT_HOME``):

* ``app/<version>/`` holds the program;
* ``bin/`` holds the launcher (``crit.cmd`` on Windows, ``crit`` elsewhere) and is added to
  PATH: the user's PATH in the registry on Windows, ``~/.profile`` (and ``~/.bashrc``,
  ``~/.zshrc``) elsewhere, when it is not there yet.
* On Windows, ``Settings > Apps > Installed apps`` lists crit (``HKCU\\...\\Uninstall\\crit``).

Versions before 1.0.1 installed into ``%LOCALAPPDATA%\\Programs\\critique-bot`` or
``~/.local/share/critique-bot`` with launchers in ``~/.local/bin``; the next install removes
that copy, the launchers that point at it, and (Windows) its bin folder on PATH.

Each version gets its own folder, so installing a new one works while the old one is
running; the launcher then points at the new one and older folders are removed when
they are not in use. Settings are not part of the install: crit asks for them the first
time it needs them (or ``crit config``), and they stay where they are across updates.

``crit uninstall`` asks nothing: it removes ``~/.crit`` (except ``projects/``, which holds
project data), the old layouts, the PATH entries and the Windows "Installed apps" entry.
A project's own ``.crit`` (or old ``.bot``) folder is never touched.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from critique_bot import __version__, log

_PATH_MARK = "# added by crit install"
#: The marker versions up to 1.0.4 wrote; uninstall removes those lines too.
_OLD_PATH_MARKS = ("# added by critique-bot install",)
#: Launchers crit versions wrote in a bin folder.
_OLD_LAUNCHERS = ("crit.cmd", "critique-bot.cmd", "crit", "critique-bot")
#: The Windows "Installed apps" entry (HKEY_CURRENT_USER).
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\crit"


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


#: Where the .deb puts the program (1.0.4 and before: /opt/critique-bot); apt manages it.
DEB_ROOT = Path("/opt/crit")
DEB_ROOTS = (DEB_ROOT, Path("/opt/critique-bot"))
#: Set to 1 to keep a downloaded program from installing itself when it runs.
ENV_NO_AUTO_INSTALL = "CRIT_NO_AUTO_INSTALL"


def is_installed_copy(bundle: Path | None = None) -> bool:
    """True when the running program is the installed one (or the .deb's, in /opt/crit)."""
    bundle = bundle or frozen_bundle_dir()
    if bundle is None:
        return False
    if is_deb_copy(bundle):
        return True
    try:
        bundle.resolve().relative_to(install_root().resolve())
    except ValueError:
        return False
    return True


def is_deb_copy(bundle: Path | None = None) -> bool:
    """True when the running program is the .deb's (/opt/crit, or /opt/critique-bot from 1.0.4)."""
    bundle = bundle or frozen_bundle_dir()
    if bundle is None or os.name == "nt":
        return False
    for root in DEB_ROOTS:
        try:
            bundle.resolve().relative_to(root)
            return True
        except ValueError:
            continue
    return False


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
    if argv[:1] in (["install"], ["uninstall"], ["--help"], ["-h"]):
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
            log.info(line)
        return False
    say(f"Installed crit {__version__} in {install_root() / __version__} and added it to your PATH.")
    return True


def _exe_name() -> str:
    return "crit.exe" if os.name == "nt" else "crit"


def _ours(text: str) -> bool:
    """True when a launcher's text starts crit: it names crit's folders or the old ``--as-crit``."""
    from critique_bot.config import crit_home

    if "--as-crit" in text:
        return True
    return any(str(folder) in text for folder in (crit_home(), install_root(), legacy_install_root()))


def _write_launchers(app: Path) -> list[Path]:
    """crit in the bin folder, pointing at ``app``; an old critique-bot launcher there goes."""
    folder = bin_dir()
    folder.mkdir(parents=True, exist_ok=True)
    exe = app / _exe_name()
    written = []
    for old in ("critique-bot.cmd", "critique-bot"):
        path = folder / old
        try:
            if path.is_file() and _ours(path.read_text(encoding="utf-8", errors="replace")):
                path.unlink()
        except OSError as exc:
            log.info(f"left {path}: {exc}")
    if os.name == "nt":
        path = folder / "crit.cmd"
        path.write_text(f'@"{exe}" %*\r\n', encoding="utf-8", newline="")
        written.append(path)
        return written
    path = folder / "crit"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f'#!/bin/sh\nexec "{exe}" "$@"\n', encoding="utf-8")
    tmp.chmod(0o755)
    os.replace(tmp, path)
    written.append(path)
    return written


def _broadcast_environment_change() -> None:
    """Tell Explorer the user's environment changed (WM_SETTINGCHANGE), so new terminals see it."""
    try:
        import ctypes

        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))  # type: ignore[attr-defined]
    except Exception as exc:
        log.info(f"could not announce the PATH change: {exc}")


def _windows_add_to_path(folder: Path) -> bool:
    """Add ``folder`` to the user's PATH (HKCU\\Environment). True when it was added."""
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
    _broadcast_environment_change()
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
        say("Install works for the downloaded program (crit.exe or the Linux bundle), not from source.")
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
        try:
            _windows_register_app(app)
        except OSError as exc:
            log.info(f"could not add crit to Installed apps: {exc}")
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


def _folder_size_kb(folder: Path) -> int:
    total = 0
    for path in folder.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total // 1024


def _windows_register_app(app: Path) -> None:
    """The "Installed apps" entry (HKCU, no administrator rights): its Uninstall runs ``crit uninstall``."""
    import winreg

    from critique_bot.config import crit_home

    exe = app / _exe_name()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        for name, value in (
            ("DisplayName", "crit"),
            ("DisplayVersion", __version__),
            ("Publisher", "crit"),
            ("InstallLocation", str(crit_home())),
            ("DisplayIcon", str(exe)),
            ("UninstallString", f'"{exe}" uninstall'),
        ):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        for name, number in (("NoModify", 1), ("NoRepair", 1), ("EstimatedSize", _folder_size_kb(app))):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, number)


def _windows_unregister_app() -> bool:
    """Remove the "Installed apps" entry. True when it was there."""
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        return False
    return True


def _remove_path_lines() -> list[Path]:
    """Drop the PATH lines crit's installer added (only lines with its marker) from the shell files."""
    changed = []
    marks = (_PATH_MARK, *_OLD_PATH_MARKS)
    for name in (".profile", ".bashrc", ".zshrc"):
        rc = Path.home() / name
        try:
            if not rc.is_file():
                continue
            text = rc.read_text(encoding="utf-8", errors="surrogateescape")
            lines = text.splitlines(keepends=True)
            kept = [line for line in lines if not any(mark in line for mark in marks)]
            if len(kept) == len(lines):
                continue
            rc.write_text("".join(kept), encoding="utf-8", errors="surrogateescape")
            changed.append(rc)
        except OSError as exc:
            log.info(f"could not change {rc}: {exc}")
    return changed


def _old_folders() -> list[Path]:
    """The folders versions before 1.0.1 used for settings, the browser profile and the program."""
    from critique_bot import config

    folders = [config.legacy_config_dir(), legacy_install_root()]
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        folders.append(Path(base) / "critique-bot")
    elif sys.platform == "darwin":
        folders.append(Path.home() / "Library" / "Application Support" / "critique-bot")
    return folders


def _remove_tree(path: Path, failed: list[Path]) -> None:
    """Remove ``path`` (a file, a link, or a folder) as far as possible; what is left goes in ``failed``."""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
            return
        if not path.exists():
            return
        shutil.rmtree(path, ignore_errors=True)
    except OSError as exc:
        log.info(f"could not remove {path}: {exc}")
    if path.exists() or path.is_symlink():
        failed.append(path)


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (OSError, ValueError):
        return False


def _schedule_windows_removal(folder: Path) -> bool:
    """Remove ``folder`` once this program exits: a detached ``cmd`` waits about 2 s, removes it
    (trying again each second while the exe is still running, for up to 5 minutes), and ends."""
    target = str(folder).replace('"', "")
    script = (
        f'for /l %i in (1,1,300) do (ping -n 3 127.0.0.1 >nul & rmdir /s /q "{target}" 2>nul'
        f' & if not exist "{target}" exit)'
    )
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= int(getattr(subprocess, name, 0))
    cwd = os.environ.get("TEMP") or os.environ.get("SystemRoot") or str(Path.home())
    try:
        subprocess.Popen(  # nosec B602 B607: a fixed cmd script; the folder is crit's own
            f'cmd.exe /d /s /c "{script}"',
            cwd=cwd,
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        log.info(f"could not schedule the removal of {folder}: {exc}")
        return False
    return True


def uninstall(*, say=print) -> int:
    """Remove crit for this user without a question: ``~/.crit`` (but ``projects/``), the old
    layouts, the launchers, the PATH entries, and the Windows "Installed apps" entry.
    Projects' own ``.crit`` / ``.bot`` folders are never touched. Each item is best effort."""
    from critique_bot.config import crit_home

    log.info("crit uninstall")
    home = crit_home()
    running = frozen_bundle_dir()
    deb = is_deb_copy(running)
    failed: list[Path] = []

    # Old launchers in ~/.local/bin (and the old Windows bin folder) that start crit.
    for folder in {legacy_bin_dir(), bin_dir()}:
        for name in _OLD_LAUNCHERS:
            path = folder / name
            try:
                if path.is_file() and (folder == bin_dir() or _ours(path.read_text(encoding="utf-8", errors="replace"))):
                    path.unlink()
            except OSError as exc:
                log.info(f"could not remove {path}: {exc}")

    if os.name == "nt":
        for folder in (bin_dir(), legacy_bin_dir()):
            try:
                _windows_remove_from_path(folder)
            except OSError as exc:
                log.info(f"could not take {folder} off PATH: {exc}")
        _broadcast_environment_change()
        try:
            _windows_unregister_app()
        except OSError as exc:
            log.info(f"could not remove the Installed apps entry: {exc}")
    else:
        _remove_path_lines()

    for folder in _old_folders():
        _remove_tree(folder, failed)

    # crit's own folder: everything but projects/ (that is project data, like a project's .crit).
    log.close_file()  # the log file is in there: no line may create it again
    keep = home / "projects"
    if home.is_dir() and not home.is_symlink():
        for child in sorted(home.iterdir()):
            if child.name == keep.name and child.is_dir():
                continue
            _remove_tree(child, failed)
        if not keep.exists():
            try:
                home.rmdir()
            except OSError:
                if os.name != "nt" or running is None or not _inside(running, home):
                    failed.append(home)
    elif home.is_symlink():
        _remove_tree(home, failed)

    # Windows keeps the running crit.exe locked: remove its folder once crit has exited.
    if os.name == "nt" and running is not None and _inside(running, home):
        locked = home if not keep.exists() else home / "app"
        if any(_inside(path, locked) or path == locked for path in failed) or locked.exists():
            if _schedule_windows_removal(locked):
                failed = [path for path in failed if not (_inside(path, locked) or path == locked)]

    if deb:
        say("crit is removed (settings and sign-in). Project .crit folders were kept.")
        say("Run: sudo apt purge crit")
    elif running is None:
        say("crit is removed (settings and sign-in). Project .crit folders were kept.")
    else:
        say("crit is removed (settings, sign-in, and program). Project .crit folders were kept.")
    if failed:
        say(f"Could not remove (in use; close the browser or crit and delete by hand): {', '.join(str(p) for p in failed)}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="crit install",
        description="Install crit for this user so every terminal finds it (no administrator rights needed).",
    )
    parser.add_argument("--uninstall", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config-file", metavar="PATH", help="after installing, use this config file (url, page elements, ...)")
    args = parser.parse_args(argv)
    if args.uninstall:
        return uninstall()
    code = install()
    if code == 0 and args.config_file:
        code = set_config_file(args.config_file)
    return code


def main_uninstall(argv: list[str]) -> int:
    """``crit uninstall``: no questions, no setup; see :func:`uninstall`."""
    parser = argparse.ArgumentParser(
        prog="crit uninstall",
        description=(
            "Remove crit for this user, without asking: the program, settings, sign-in (browser profile), "
            "cache and logs in ~/.crit, the files of older versions, and crit's PATH entries "
            "(on Windows also the Installed apps entry). Projects' own .crit folders are kept. "
            "For the .deb package, run sudo apt purge crit afterwards."
        ),
    )
    parser.parse_args(argv)
    return uninstall()
