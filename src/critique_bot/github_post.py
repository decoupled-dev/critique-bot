"""Post a summary comment and inline diff comments on a GitHub pull request.

Mirrors :mod:`critique_bot.gitlab_post` so both hosts get the same treatment:
the prose goes in the summary, the machine-readable JSON block never does.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from critique_bot import log
from critique_bot.review_comments import (
    parse_diff_lines,
    parse_inline_comments,
    resolve_comment,
    strip_json_block,
)

TOKEN_ENV = ("CRITIQUE_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
DEFAULT_API_URL = "https://api.github.com"


class GitHubPostError(RuntimeError):
    """GitHub rejected or could not complete a review post."""


def post_review(
    *,
    review_file: Path,
    patch_file: Path | None = None,
    repo: str | None = None,
    pr_number: str | None = None,
    api_url: str | None = None,
    token: str | None = None,
) -> int:
    repo = repo or os.environ.get("GITHUB_REPOSITORY") or ""
    pr_number = str(pr_number or _pr_number_from_env() or "")
    api_url = (api_url or os.environ.get("GITHUB_API_URL") or DEFAULT_API_URL).rstrip("/")
    token = token or _resolve_token()
    if not repo or "/" not in repo:
        raise GitHubPostError("need --repo owner/name (or GITHUB_REPOSITORY)")
    if not pr_number:
        raise GitHubPostError(
            "need --pr (or GITHUB_PR_NUMBER / GITHUB_REF / GITHUB_EVENT_PATH)"
        )
    if not token:
        raise GitHubPostError(
            "no GitHub token. In Actions pass GITHUB_TOKEN with "
            "`permissions: pull-requests: write`, or set CRITIQUE_GITHUB_TOKEN"
        )

    review_md = review_file.read_text(encoding="utf-8")
    if not review_md.strip():
        raise GitHubPostError(f"review file is empty: {review_file}")
    patch = (
        patch_file.read_text(encoding="utf-8")
        if patch_file and patch_file.is_file()
        else ""
    )

    comments = parse_inline_comments(review_md)
    diff_lines = parse_diff_lines(patch) if patch else []
    head_sha = _head_sha(api_url, repo, pr_number, token)

    posted = 0
    skipped = 0
    for comment in comments:
        row = resolve_comment(comment, diff_lines) if diff_lines else None
        if row is None:
            skipped += 1
            log.warn(
                "skipping inline comment; line not in diff "
                + log.kv(path=comment.path, line=comment.line, side=comment.side)
            )
            continue
        side = "LEFT" if row.kind == "del" else "RIGHT"
        line = row.old_line if side == "LEFT" else row.new_line
        if line is None:
            skipped += 1
            continue
        payload = {
            "body": comment.body,
            "commit_id": head_sha,
            "path": row.path,
            "line": line,
            "side": side,
        }
        try:
            _request(
                "POST",
                f"{api_url}/repos/{repo}/pulls/{pr_number}/comments",
                token,
                payload,
            )
        except GitHubPostError as exc:
            skipped += 1
            log.warn(f"inline comment rejected: {exc}")
            continue
        posted += 1
        log.info("posted inline comment " + log.kv(path=row.path, line=line, side=side))

    summary = strip_json_block(review_md)
    summary_ok = True
    if summary:
        header = ""
        if posted:
            header = f"_Posted {posted} inline comment(s) on the Files changed tab._\n\n"
        try:
            _request(
                "POST",
                f"{api_url}/repos/{repo}/issues/{pr_number}/comments",
                token,
                {"body": header + summary},
            )
            log.info("posted pull request summary comment")
        except GitHubPostError as exc:
            summary_ok = False
            log.error(f"could not post summary comment: {exc}")
    print(
        f"github-post: {posted} inline comment(s), {skipped} skipped, "
        f"summary={'yes' if summary and summary_ok else 'no'}",
        flush=True,
    )
    return 0 if summary_ok else 1


def _resolve_token() -> str:
    for name in TOKEN_ENV:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _pr_number_from_env() -> str:
    explicit = os.environ.get("GITHUB_PR_NUMBER", "").strip()
    if explicit:
        return explicit
    ref = os.environ.get("GITHUB_REF", "").strip()
    parts = [part for part in ref.strip("/").split("/") if part]
    if len(parts) >= 3 and parts[0] == "refs" and parts[1] == "pull" and parts[2].isdigit():
        return parts[2]
    event_path = os.environ.get("GITHUB_EVENT_PATH", "").strip()
    if event_path and Path(event_path).is_file():
        try:
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        number = (event.get("pull_request") or {}).get("number") if isinstance(event, dict) else None
        if isinstance(number, int):
            return str(number)
    return ""


def _head_sha(api_url: str, repo: str, pr_number: str, token: str) -> str:
    """Inline comments must be anchored to the PR head, not the merge commit."""
    try:
        data = _request("GET", f"{api_url}/repos/{repo}/pulls/{pr_number}", token)
    except GitHubPostError as exc:
        fallback = os.environ.get("GITHUB_HEAD_SHA") or os.environ.get("GITHUB_SHA") or ""
        if fallback:
            log.warn(f"using GITHUB_SHA; could not read the pull request: {exc}")
            return fallback
        raise
    head = data.get("head") if isinstance(data, dict) else None
    sha = str(head.get("sha")) if isinstance(head, dict) and head.get("sha") else ""
    if not sha:
        raise GitHubPostError("pull request has no head sha")
    return sha


def _request(
    method: str, url: str, token: str, payload: dict[str, Any] | None = None
) -> Any:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "critique-bot",
    }
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise GitHubPostError(f"HTTP {exc.code} {method} {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GitHubPostError(f"could not reach {url}: {exc.reason}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
