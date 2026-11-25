"""Entry point of ``python -m critique_bot`` and of the standalone bundle.

The bundle ships one program, ``critique-bot``. ``crit`` is the same program started
with ``--as-crit`` first (the .deb's /usr/bin/crit and the Windows zip's crit.cmd do
that), or under a name that starts with ``crit``/``bot-agent`` other than critique-bot.

A downloaded program (Windows exe or the Linux zip) installs itself the first time it
runs, without a question (see installer.should_auto_install), then does what was asked.

Double-clicked in Windows Explorer, crit is not in a project folder and its console
window would close the moment it exits. So it then sets itself up if needed (the config
file question), says how to use it from a terminal, and waits for Enter before the window
closes.
"""

import os
import sys


def _wants_crit(argv: list[str]) -> bool:
    if argv[1:2] == ["--as-crit"]:
        del argv[1]
        return True
    name = os.path.splitext((argv[0] if argv else "").replace("\\", "/").rsplit("/", 1)[-1])[0].lower()
    return name in {"crit", "bot-agent"}


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
                print("  (crit is not installed yet: run critique-bot.exe install, or double-click it again and answer Y.)")
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

        if installer.should_auto_install([a for a in sys.argv[1:] if a != "--as-crit"]):
            installer.auto_install()
    except Exception as exc:  # installing is a convenience; the command still runs
        print(f"crit could not install itself: {exc}", file=sys.stderr)


def main() -> int:
    _maybe_auto_install()
    if launched_from_explorer():
        args = [a for a in sys.argv[1:] if a != "--as-crit"]
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
    rest = [a for a in sys.argv[1:] if a != "--as-crit"]
    if rest[:1] == ["install"]:
        from critique_bot import installer

        return installer.main(rest[1:])
    if _wants_crit(sys.argv):
        from critique_bot.agent_cli import main as crit_main

        return crit_main(sys.argv[1:])
    from critique_bot.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
