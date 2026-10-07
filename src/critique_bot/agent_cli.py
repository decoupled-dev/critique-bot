"""``crit`` is the local coding agent. ``bot-agent`` is the same command."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from critique_bot.cli import main as cli_main

    args = list(sys.argv[1:] if argv is None else argv)
    return cli_main(["--mode", "agent", *args])


if __name__ == "__main__":
    raise SystemExit(main())
