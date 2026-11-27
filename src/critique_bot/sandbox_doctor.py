"""``crit doctor sandbox``: can this machine sandbox the agent's commands, and if not, how to fix it.

It probes the backend (bubblewrap on Linux, sandbox-exec on macOS), then runs four real
checks in a throwaway workspace: a write inside it must work; a write to the home folder,
a read of ``~/.ssh``, and a network connection must all fail. Exit 0 when the sandbox works,
1 when it does not (the fix is printed).

Apps layer: the routing layer calls :func:`doctor_sandbox` (``crit doctor sandbox``).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from critique_bot import log, sandbox


def _settings(workspace: Path) -> tuple[sandbox.Settings, str]:
    path = workspace / ".crit" / "settings.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return sandbox.Settings(), "default"
    try:
        return sandbox.parse_settings(raw.get("sandbox") if isinstance(raw, dict) else None), str(path)
    except ValueError as exc:
        return sandbox.Settings(), f"{path}: {exc} (the default is used)"


def _probe(policy: sandbox.Policy, argv: list[str], cwd: Path) -> tuple[int, str]:
    try:
        proc = sandbox.run(argv, cwd=cwd, env=dict(os.environ), policy=policy, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        out, _ = proc.communicate(timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 255, str(exc)
    return proc.returncode, (out or b"").decode("utf-8", "replace").strip()


def live_checks(policy_for: Callable[[Path], sandbox.Policy] | None = None) -> list[tuple[str, bool, str]]:
    """(check, passed, detail) for each escape check, run for real in a throwaway workspace."""
    workspace = Path(tempfile.mkdtemp(prefix="crit-doctor-"))
    home = Path(os.path.expanduser("~"))
    marker = home / f".crit-sandbox-probe-{os.getpid()}"
    results: list[tuple[str, bool, str]] = []
    try:
        policy = policy_for(workspace) if policy_for else sandbox.build_policy(
            sandbox.Settings(mode=sandbox.WORKSPACE_WRITE), workspace, make_caches=False)
        code, out = _probe(policy, ["/bin/sh", "-c", "echo ok > probe.txt && cat probe.txt"], workspace)
        results.append(("write inside the workspace", code == 0 and (workspace / "probe.txt").exists(), out))
        code, out = _probe(policy, ["/bin/sh", "-c", f"echo x > '{marker}'"], workspace)
        leaked = marker.exists()
        if leaked:
            marker.unlink()
        results.append(("write to the home folder is refused", code != 0 and not leaked, out))
        ssh = home / ".ssh"
        if ssh.is_dir():
            code, out = _probe(policy, ["/bin/sh", "-c", f"ls -A '{ssh}'"], workspace)
            results.append(("~/.ssh is hidden", code != 0 or not out.strip(), out[:200]))
        else:
            results.append(("~/.ssh is hidden", True, "no ~/.ssh on this machine (nothing to hide)"))
        code, out = _probe(policy, [sys.executable, "-I", "-c",
                                    "import socket; socket.create_connection(('1.1.1.1', 53), 3)"], workspace)
        results.append(("direct network connection is refused", code != 0, out.splitlines()[-1] if out else ""))
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return results


def doctor_sandbox(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="crit doctor sandbox", description="Check the OS sandbox for agent commands.")
    parser.add_argument("--workspace", default=".", help="project folder whose .crit/settings.json to read (default: here)")
    parser.add_argument("--no-run", action="store_true", help="only probe the backend; skip the live escape checks")
    args = parser.parse_args(argv)
    say = log.print_safe
    workspace = Path(args.workspace).resolve()
    wanted, source = _settings(workspace)
    found = sandbox.availability()
    say(f"platform:  {sys.platform}")
    say(f"mode:      {wanted.mode}  ({source})" + (f", preset {wanted.preset}" if wanted.preset else ""))
    say(f"backend:   {found.backend}: {'works' if found.ok else 'NOT AVAILABLE'}")
    for line in found.details:
        say(f"           {line}")
    if wanted.mode == sandbox.FULL:
        say('The sandbox is off ("sandbox": "full"): commands run with your own rights and permission prompts.')
    if not found.ok:
        say(f"problem:   {found.problem}")
        say(f"fix:       {found.fix}")
        say("Until then crit runs commands as before (no sandbox), with its permission prompts.")
        return 1
    if args.no_run:
        return 0
    failed = 0
    for name, passed, detail in live_checks():
        say(f"  {'ok  ' if passed else 'FAIL'}  {name}" + (f"   [{detail[:120]}]" if detail and not passed else ""))
        failed += 0 if passed else 1
    if failed:
        say(f"{failed} check(s) failed: the sandbox does not hold on this machine. Report it with the lines above.")
        return 1
    say("The sandbox works: commands write only the workspace and caches, secrets are hidden, and the network "
        "goes only through crit's egress proxy.")
    return 0
