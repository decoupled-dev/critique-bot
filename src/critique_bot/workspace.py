"""Build an MR diff from the local Git checkout and load changed file bodies.

``critique-bot submit`` runs inside the GitLab job, so the app repo is already
checked out at HEAD. This module uses local ``git`` (not the GitLab Files API).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from critique_bot import log
from critique_bot.patch import (
    InputLimits,
    LoadedInput,
    changed_file_paths,
    context_file_priority,
    load_path,
    looks_binary_path,
    skip_changed_file_bodies,
    skip_file_context,
)

DEFAULT_PATCH_NAME = "diff.patch"
FETCH_DEPTH = 50
ENV_GITLAB_CI = "GITLAB_CI"
ENV_DIFF_BASE = "CI_MERGE_REQUEST_DIFF_BASE_SHA"
ENV_COMMIT_SHA = "CI_COMMIT_SHA"
ENV_TARGET_BRANCH = "CI_MERGE_REQUEST_TARGET_BRANCH_NAME"


class EmptyDiff(Exception):
    """The workspace produced no reviewable diff."""


class WorkspaceError(RuntimeError):
    """Git or the checkout could not produce a review patch."""


def ci_review_refs() -> dict[str, str] | None:
    """Merge-base and HEAD from GitLab CI, if both are set."""
    base = (os.environ.get(ENV_DIFF_BASE) or "").strip()
    head = (os.environ.get(ENV_COMMIT_SHA) or "").strip()
    if not base or not head:
        return None
    return {
        "base": base,
        "head": head,
        "target": (os.environ.get(ENV_TARGET_BRANCH) or "").strip(),
    }


def should_prepare_workspace(
    *,
    patch_file: str | None,
    extra_files: list[str],
) -> bool:
    """True when CI should build the diff from the job checkout.

    Local ``--patch-file`` / explicit file args keep the old stdin-or-file path.
    """
    if patch_file or extra_files:
        return False
    if ci_review_refs():
        return True
    return bool((os.environ.get(ENV_GITLAB_CI) or "").strip())


def prepare_workspace_patch(
    repo_dir: Path,
    out_path: Path,
    *,
    git_run=None,
) -> str:
    """Fetch enough of target for the merge-base, write ``git diff``, return it.

    Three-dot ``base...HEAD`` is the MR delta (same as today's CI yaml). Without
    ``CI_MERGE_REQUEST_DIFF_BASE_SHA``, uses ``HEAD~1...HEAD``.
    """
    repo = Path(repo_dir)
    refs = ci_review_refs()
    if refs:
        if refs["target"]:
            _git(
                repo,
                ["fetch", f"--depth={FETCH_DEPTH}", "origin", refs["target"]],
                check=False,
                git_run=git_run,
            )
        spec = f"{refs['base']}...{refs['head']}"
    else:
        spec = "HEAD~1...HEAD"
    log.info(f"building workspace diff {spec} in {repo}")
    try:
        text = _git(repo, ["diff", spec], check=True, git_run=git_run)
    except WorkspaceError as exc:
        raise WorkspaceError(
            f"could not build git diff {spec} in {repo}: {exc}"
        ) from exc
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    if not text.strip():
        log.info("empty diff; nothing to review")
        raise EmptyDiff("empty diff; nothing to review")
    log.info(f"wrote {out_path} ({len(text)} chars)")
    return text


def load_changed_files(
    repo_dir: Path,
    patch: str,
    limits: InputLimits,
) -> list[LoadedInput]:
    """Read HEAD contents of text files listed in ``patch`` from ``repo_dir``."""
    repo = Path(repo_dir)
    ordered = sorted(changed_file_paths(patch), key=context_file_priority)
    if skip_changed_file_bodies(len(ordered), limits.patch_only_file_count):
        log.info(
            f"{len(ordered)} changed files "
            f"(>= {limits.patch_only_file_count}); sending the patch only"
        )
        return []
    loaded: list[LoadedInput] = []
    for path in ordered:
        if looks_binary_path(path) or skip_file_context(path):
            continue
        full = repo / path
        if not full.is_file():
            log.info(f"skipping {path}: not in the checkout (deleted or missing)")
            continue
        item = load_path(full, limits, name=path)
        if item.binary:
            continue
        loaded.append(item)
        if len(loaded) >= limits.max_files:
            break
    log.info(
        f"attached {len(loaded)} changed file(s) from {repo} "
        f"({len(ordered)} reviewable path(s) in the patch)"
    )
    return loaded


def _git(
    repo: Path,
    args: list[str],
    *,
    check: bool,
    git_run=None,
) -> str:
    run = git_run or subprocess.run
    proc = run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    stdout = _decode(getattr(proc, "stdout", b""))
    stderr = _decode(getattr(proc, "stderr", b"")).strip()
    code = int(getattr(proc, "returncode", 1) or 0)
    if code != 0:
        detail = stderr or f"exit {code}"
        if check:
            raise WorkspaceError(detail)
        log.warn(f"git {' '.join(args)}: {detail}")
        return stdout
    return stdout


def _decode(data: object) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", "replace")
    return ""
