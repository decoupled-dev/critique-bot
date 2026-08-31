"""Post a summary note and inline diff discussions on a GitLab merge request."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from critique_bot import log
from critique_bot.review_comments import (
    parse_diff_lines,
    parse_inline_comments,
    position_for,
    resolve_comment,
    strip_json_block,
)

TOKEN_ENV = ("CRITIQUE_GITLAB_TOKEN", "GITLAB_TOKEN", "CI_JOB_TOKEN")


class GitLabPostError(RuntimeError):
    """GitLab rejected or could not complete a review post."""


def post_review(
    *,
    review_file: Path,
    patch_file: Path | None = None,
    project_id: str | None = None,
    mr_iid: str | None = None,
    api_url: str | None = None,
    token: str | None = None,
) -> int:
    project_id = project_id or os.environ.get("CI_PROJECT_ID") or ""
    mr_iid = mr_iid or os.environ.get("CI_MERGE_REQUEST_IID") or ""
    api_url = (api_url or os.environ.get("CI_API_V4_URL") or "").rstrip("/")
    token = token or _resolve_token()
    if not api_url:
        raise GitLabPostError("need --api-url or CI_API_V4_URL")
    if not project_id or not mr_iid:
        raise GitLabPostError("need --project-id and --mr-iid (or CI_PROJECT_ID / CI_MERGE_REQUEST_IID)")
    if not token:
        raise GitLabPostError(
            "no GitLab token. Create a project access token with scope `api` "
            "and set CI/CD variable CRITIQUE_GITLAB_TOKEN (unprotected, masked)"
        )
    review_md = review_file.read_text(encoding="utf-8")
    if not review_md.strip():
        raise GitLabPostError(f"review file is empty: {review_file}")
    patch = patch_file.read_text(encoding="utf-8") if patch_file and patch_file.is_file() else ""
    comments = parse_inline_comments(review_md)
    diff_lines = parse_diff_lines(patch) if patch else []
    refs = _diff_refs(api_url, project_id, mr_iid, token)
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
        position = {
            "base_sha": refs["base_sha"],
            "start_sha": refs["start_sha"],
            "head_sha": refs["head_sha"],
            "old_path": row.old_path or row.path,
            "new_path": row.path,
            "position_type": "text",
            **position_for(row),
        }
        try:
            _request(
                "POST",
                f"{api_url}/projects/{_quote(project_id)}/merge_requests/{mr_iid}/discussions",
                token,
                {"body": comment.body, "position": position},
            )
        except GitLabPostError as exc:
            skipped += 1
            log.warn(f"inline comment rejected: {exc}")
            continue
        posted += 1
        log.info(
            "posted inline comment "
            + log.kv(path=row.path, line=position.get("new_line") or position.get("old_line"))
        )
    summary = strip_json_block(review_md)
    summary_ok = True
    if summary:
        header = ""
        if posted:
            header = f"_Posted {posted} inline comment(s) on the Changes tab._\n\n"
        try:
            _request(
                "POST",
                f"{api_url}/projects/{_quote(project_id)}/merge_requests/{mr_iid}/notes",
                token,
                {"body": header + summary},
            )
            log.info("posted merge request summary note")
        except GitLabPostError as exc:
            summary_ok = False
            log.error(f"could not post summary note: {exc}")
    print(
        f"gitlab-post: {posted} inline comment(s), {skipped} skipped, "
        f"summary={'yes' if summary and summary_ok else 'no'}",
        flush=True,
    )
    return 0 if summary_ok else 1


def _resolve_token() -> str:
    for name in TOKEN_ENV:
        value = os.environ.get(name, "").strip()
        if value:
            if name == "CI_JOB_TOKEN":
                log.warn(
                    "CI_JOB_TOKEN cannot create MR notes; "
                    "set CRITIQUE_GITLAB_TOKEN to a project access token (scope api)"
                )
            return value
    return ""


def _diff_refs(api_url: str, project_id: str, mr_iid: str, token: str) -> dict[str, str]:
    env_refs = {
        "base_sha": os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") or "",
        "start_sha": os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") or "",
        "head_sha": os.environ.get("CI_COMMIT_SHA") or "",
    }
    try:
        data = _request(
            "GET",
            f"{api_url}/projects/{_quote(project_id)}/merge_requests/{mr_iid}",
            token,
        )
    except GitLabPostError as exc:
        if all(env_refs.values()):
            log.warn(f"using CI sha env; could not load MR diff_refs: {exc}")
            return env_refs
        raise
    refs = data.get("diff_refs") if isinstance(data, dict) else None
    if isinstance(refs, dict) and refs.get("base_sha") and refs.get("head_sha"):
        return {
            "base_sha": str(refs["base_sha"]),
            "start_sha": str(refs.get("start_sha") or refs["base_sha"]),
            "head_sha": str(refs["head_sha"]),
        }
    if all(env_refs.values()):
        return env_refs
    raise GitLabPostError("merge request has no diff_refs yet; retry the job")


def _request(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> Any:
    headers = {"PRIVATE-TOKEN": token}
    if token == os.environ.get("CI_JOB_TOKEN"):
        headers = {"JOB-TOKEN": token}
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
        raise GitLabPostError(f"HTTP {exc.code} {method} {url}: {detail}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _quote(project_id: str) -> str:
    if project_id.isdigit():
        return project_id
    return urllib.parse.quote(project_id, safe="")
