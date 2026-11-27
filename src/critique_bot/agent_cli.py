"""``crit`` is the one command for everything crit does.

The first word picks the mode; with no mode word crit runs the local coding agent:

    crit [TASK]                 coding agent (default; also: crit agent, crit init, crit undo)
    crit review [PATCH]         code review: the Code Reviewer reads the project, then reports
    crit review --quick         the one-shot review (one message, no tools; what CI runs)
    crit ask --prompt ...       one-shot prompt with optional files (also: crit general)
    crit chat                   interactive conversation in this terminal
    crit setup | worker | submit | queue-status | gitlab-post | eval
    crit install | uninstall    put crit on PATH / remove crit for this user (no questions)

``crit --yes`` (also ``--auto`` or ``-y``) runs edits and commands without
asking; see ``crit --help``.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from critique_bot.cli import MODE_WORDS, SUBCOMMANDS
    from critique_bot.cli import main as cli_main

    args = [arg for arg in (sys.argv[1:] if argv is None else argv) if arg != "--as-crit"]
    if args[:1] == ["uninstall"]:  # before any setup, theme, or config: it asks nothing
        from critique_bot import installer

        return installer.main_uninstall(args[1:])
    if args[:1] == ["install"]:
        from critique_bot import installer

        return installer.main(args[1:])
    args = ["--yes" if arg == "-y" else arg for arg in args]
    if args and args[0] in MODE_WORDS:
        # crit's own defaults (the agent review for crit review) after the mode word
        return cli_main([args[0], "--crit-defaults", *args[1:]])
    if args and args[0] in SUBCOMMANDS:
        return cli_main(args)
    return cli_main(["--mode", "agent", "--crit-defaults", *args])


if __name__ == "__main__":
    raise SystemExit(main())
