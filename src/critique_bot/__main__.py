"""Entry point of ``python -m critique_bot`` and of the standalone bundle (``crit``, ``crit.exe``).

The program is crit: with no mode word it runs the coding agent; ``crit review`` reviews a
patch. ``--as-crit`` (written by launchers of crit up to 1.0.4) is still accepted and ignored.

``crit uninstall`` is handled first, before anything else: no setup, no questions, no
self-install.

A downloaded program (Windows exe or the Linux zip) installs itself the first time it
runs, without a question (see installer.should_auto_install), then does what was asked.

Double-clicked in Windows Explorer, crit is not in a project folder and its console
window would close the moment it exits. So it then sets itself up if needed (the same
first-run questions as in a terminal, see setup_wizard), says how to use it from a
terminal, and waits for Enter before the window closes.
"""

import os
import sys


def _args() -> list[str]:
    """The arguments without the old launchers' ``--as-crit``."""
    return [a for a in sys.argv[1:] if a != "--as-crit"]


def launched_from_explorer() -> bool:
    """True on Windows when this process owns its console alone (a double-click, not a terminal)."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        processes = (ctypes.c_uint32 * 4)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(processes, 4)  # type: ignore[attr-defined]
    except Exception:
        return False
    return count == 1


def _double_click_main() -> int:
    """What a double-click does: setup questions if crit is not set up, then how to use it."""
    from critique_bot import cli
    from critique_bot.config_cmd import resolve_config_path

    from critique_bot import installer

    code = 0
    try:
        cli._migrate_legacy_files()
        location = resolve_config_path(None, os.path.expanduser("~"))
        if not location.exists:
            outcome = cli._first_run_wizard(location.path)
            code = outcome if isinstance(outcome, int) else 0
        else:
            print(f"crit is set up (settings: {location.path}).")
        if code == 0:
            print("")
            print("crit runs in a terminal, inside your project folder:")
            print("  1. Open the project folder in File Explorer.")
            print("  2. Click the address bar, type powershell, and press Enter.")
            print("  3. Type crit and press Enter (or crit review to review your changes).")
            if not installer.is_installed_copy() and installer.frozen_bundle_dir() is not None:
                print("  (crit is not installed yet: run crit.exe install.)")
            print("")
            print("See or change the settings any time with: crit config")
    except KeyboardInterrupt:
        print("")
        print("Stopped.")
        code = 130
    except Exception as exc:  # the window must stay readable whatever happened
        print(f"crit stopped: {exc}")
        code = 1
    return code


def _wait_for_enter() -> None:
    try:
        input("\nPress Enter to close this window.")
    except (EOFError, KeyboardInterrupt):
        pass


def _maybe_auto_install() -> None:
    """Install the downloaded program first (once; never from sources, the .deb, or with CRIT_NO_AUTO_INSTALL=1)."""
    try:
        from critique_bot import installer

        if installer.should_auto_install(_args()):
            installer.auto_install()
    except Exception as exc:  # installing is a convenience; the command still runs
        from critique_bot import log

        log.info(f"crit could not install itself: {exc}")


def main() -> int:
    if _args()[:1] == ["uninstall"]:
        explorer = launched_from_explorer()  # Settings > Apps > Uninstall opens a console of its own
        try:
            return _run()
        finally:
            if explorer:
                _wait_for_enter()
    _maybe_auto_install()
    if launched_from_explorer():
        args = _args()
        if not args:
            code = _double_click_main()
            _wait_for_enter()
            return code
        try:
            return _run()
        finally:
            _wait_for_enter()
    return _run()


def _run() -> int:
    rest = _args()
    if rest[:1] == ["uninstall"]:
        from critique_bot import installer

        return installer.main_uninstall(rest[1:])
    if rest[:1] == ["install"]:
        from critique_bot import installer

        return installer.main(rest[1:])
    from critique_bot.agent_cli import main as crit_main

    return crit_main(rest)


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()  # the bundle's index workers start through this program
    raise SystemExit(main())
