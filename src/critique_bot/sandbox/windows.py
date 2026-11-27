"""Windows: no OS sandbox yet (best effort).

Commands run as before, with crit's permission prompts: in ``auto`` a command that is not
read-only still asks, and writes outside the workspace always ask. A restricted token plus a
job object (the plan's Windows backend) is not built yet. For a hard network boundary use the
outbound firewall rule in docs/windows-lockdown.md.
"""

from __future__ import annotations

from critique_bot.sandbox import Availability

PROBLEM = "Windows has no crit sandbox yet"
FIX = (
    "Commands run as before, with permission prompts (auto mode still asks before commands that are not "
    "read-only). For a network boundary, add the outbound firewall rule in docs/windows-lockdown.md, or run "
    "crit inside WSL2, where the Linux sandbox works."
)


def probe() -> Availability:
    return Availability(False, "none", PROBLEM, FIX)
