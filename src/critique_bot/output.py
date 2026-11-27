# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from critique_bot import log



def private_dir(path: Path, *, windows_acl: bool = True) -> Path:
    """Create ``path`` (and its parents) readable by its owner only; return it.

    For folders that hold code, prompts, or a signed-in browser: the browser profile,
    the CI queue, transcripts. An existing folder is narrowed too. POSIX: mode 0700.
    Windows: best effort with icacls (inherited entries removed, full control for the
    current account only); a failure is logged and the folder keeps its permissions.
    ``windows_acl=False`` skips the Windows step (the CI queue, which a runner
    account may share through an ACL the administrator sets).
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and not windows_acl:
        return path
    restrict_dir(path)
    return path


def restrict_dir(path: Path) -> None:
    """Narrow an existing folder to its owner (see :func:`private_dir`)."""
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            log.warn(f"could not make {path} private: {exc}")
        return
    _restrict_windows(Path(path))


_WINDOWS_DONE: set[str] = set()


def _windows_account() -> str:
    """The current account as ``*SID`` (preferred) or ``DOMAIN\\user`` for icacls."""
    whoami = shutil.which("whoami")
    if whoami:
        try:
            proc = subprocess.run(
                [whoami, "/user", "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            fields = [f.strip().strip('"') for f in proc.stdout.strip().split(",")]
            if proc.returncode == 0 and len(fields) >= 2 and fields[-1].startswith("S-"):
                return "*" + fields[-1]
        except (OSError, subprocess.TimeoutExpired):
            pass
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{user}" if domain and user else user


def _restrict_windows(path: Path) -> None:
    key = str(path.resolve()).lower()
    if key in _WINDOWS_DONE:
        return
    icacls = shutil.which("icacls") or os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "icacls.exe"
    )
    account = _windows_account()
    if not account:
        log.warn(f"could not make {path} private: current Windows account unknown")
        return
    # Grant first, then drop inherited entries: if the grant fails the folder is
    # never left with no access at all.
    steps = (
        [icacls, str(path), "/grant:r", f"{account}:(OI)(CI)F"],
        [icacls, str(path), "/inheritance:r"],
    )
    for argv in steps:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warn(f"could not make {path} private: {exc}")
            return
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            log.warn(f"could not make {path} private (icacls {proc.returncode}): {detail}")
            return
    _WINDOWS_DONE.add(key)


def result_path_under(results_dir: Path, *parts: str) -> Path:
    """``results_dir / parts`` resolved; raises ValueError if it leaves ``results_dir``."""
    base = Path(results_dir).resolve()
    target = base.joinpath(*parts).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"refusing to write outside {base}: {target}")
    return target


def write_output(
    output_dir: Path,
    body: str,
    payload: dict[str, Any],
    *,
    stem: str = "review",
    print_body: bool = True,
    mode: int | None = None,
) -> None:
    if not output_dir.exists():
        # A folder made here (a session transcript, a CI out/ folder) is owner-only;
        # an existing folder the caller chose keeps its permissions.
        output_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                os.chmod(output_dir, 0o700)
            except OSError as exc:
                log.warn(f"could not make {output_dir} private: {exc}")
    # stem may come from a queued job: keep both files inside output_dir.
    md_path = result_path_under(output_dir, f"{stem}.md")
    json_path = result_path_under(output_dir, f"{stem}.json")
    md_path.write_text(body, encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if mode is not None and os.name != "nt":
        # A shared CI queue: the CI user reads these through the queue group.
        for written in (md_path, json_path):
            os.chmod(written, mode)
    log.info(
        f"wrote {stem} "
        + log.kv(
            markdown=str(md_path),
            json=str(json_path),
            chars=len(body),
        )
    )
    if print_body:
        log.print_safe(log.paint(body, log.MODEL_COLOR), flush=True)


def write_review(
    output_dir: Path,
    body: str,
    payload: dict[str, Any],
) -> None:
    write_output(output_dir, body, payload, stem="review")


def isoformat(moment: datetime) -> str:
    return moment.astimezone().isoformat(timespec="seconds")
