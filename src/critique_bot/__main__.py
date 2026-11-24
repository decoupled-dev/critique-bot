"""Entry point of ``python -m critique_bot`` and of the standalone bundle.

The bundle ships one program, ``critique-bot``. ``crit`` is the same program started
with ``--as-crit`` first (the .deb's /usr/bin/crit and the Windows zip's crit.cmd do
that), or under a name that starts with ``crit``/``bot-agent`` other than critique-bot.
"""

import os
import sys


def _wants_crit(argv: list[str]) -> bool:
    if argv[1:2] == ["--as-crit"]:
        del argv[1]
        return True
    name = os.path.splitext(os.path.basename(argv[0] if argv else ""))[0].lower()
    return name in {"crit", "bot-agent"}


def main() -> int:
    if _wants_crit(sys.argv):
        from critique_bot.agent_cli import main as crit_main

        return crit_main(sys.argv[1:])
    from critique_bot.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
