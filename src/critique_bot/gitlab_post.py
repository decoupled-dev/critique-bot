"""Post summary and inline discussion threads on a GitLab merge request."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from critique_bot import gitlab as gl
from critique_bot import log
from critique_bot.config import GitLabConfig
from critique_bot.review_comments import (
    DiffLine,
    InlineComment,
    discussion_position,
    format_gitlab_comment,
    format_gitlab_summary,
    parse_diff_lines,
    parse_inline_comments,
    parse_review_risk,
    resolve_comment,
    strip_json_block,
)

TOKEN_ENV = gl.TOKEN_ENV
GitLabPostError = gl.GitLabError


def post_review(
    *,
    review_file: Path,
    patch_file: Path | None = None,
    project_id: str | None = None,
    mr_iid: str | None = None,
    api_url: str | None = None,
    mr_url: str | None = None,
    token: str | None = None,
    gitlab: GitLabConfig | None = None,
) -> int:
    target = gl.resolve_target(
        gitlab,
        api_url=api_url,
        project_id=project_id,
        mr_iid=mr_iid,
        mr_url=mr_url,
        token=token,
    )
    if not target.api_url:
        raise GitLabPostError(
            "need GitLab API URL: set gitlab.base_url in config.json, "
            "--api-url, CI_API_V4_URL, or CI_SERVER_URL"
        )
    if not target.project_id or not target.mr_iid:
        raise GitLabPostError(
            "need project and merge request: set gitlab.project_id / "
            "gitlab.mr_iid (or gitlab.mr_url) in config.json, --project-id / "
            "--mr-iid, or CI_PROJECT_ID / CI_MERGE_REQUEST_IID"
        )
    resolved_token = target.token or _resolve_token()
    if not resolved_token:
        raise GitLabPostError(
            "no GitLab token. Create a project access token with scope `api` "
            "and set CI/CD variable CRITIQUE_GITLAB_TOKEN (unprotected, masked)"
        )
    project_id = target.project_id
    mr_iid = target.mr_iid
    api_url = target.api_url
    review_md = review_file.read_text(encoding="utf-8")
    if not review_md.strip():
        raise GitLabPostError(f"review file is empty: {review_file}")
    patch = patch_file.read_text(encoding="utf-8") if patch_file and patch_file.is_file() else ""
    comments = parse_inline_comments(review_md)
    local_lines = parse_diff_lines(patch) if patch else []
    remote_lines: list[DiffLine] | None = None
    refs = _diff_refs(api_url, project_id, mr_iid, resolved_token)
    discussions = f"{gl.mr_api_url(api_url, project_id, mr_iid)}/discussions"
    inline = 0
    overview = 0
    skipped = 0

    def lines_for(comment: InlineComment) -> DiffLine | None:
        nonlocal remote_lines
        row = resolve_comment(comment, local_lines) if local_lines else None
        if row is not None:
            return row
        if remote_lines is None:
            remote_lines = parse_diff_lines(
                _fetch_mr_patch(api_url, project_id, mr_iid, resolved_token)
            )
        if remote_lines:
            return resolve_comment(comment, remote_lines)
        return None

    for comment in comments:
        row = lines_for(comment)
        body = format_gitlab_comment(comment)
        posted = False
        if row is not None:
            posted = _post_diff_thread(
                discussions, resolved_token, body, refs, row
            )
            if posted:
                inline += 1
                log.info(
                    "posted inline thread "
                    + log.kv(
                        path=row.path,
                        line=row.new_line or row.old_line,
                    )
                )
        if posted:
            continue
        fallback = format_gitlab_comment(comment, include_location=True)
        try:
            _post_discussion(discussions, resolved_token, fallback)
        except GitLabPostError as exc:
            skipped += 1
            log.warn(
                "skipping comment; could not post thread "
                + log.kv(path=comment.path, line=comment.line)
                + f" {exc}"
            )
            continue
        overview += 1
        log.info(
            "posted overview thread "
            + log.kv(path=comment.path, line=comment.line)
        )

    summary = strip_json_block(review_md)
    summary_ok = True
    if summary:
        try:
            _post_discussion(
                discussions,
                resolved_token,
                format_gitlab_summary(
                    summary,
                    inline_count=inline,
                    overview_count=overview,
                    risk=parse_review_risk(review_md),
                ),
            )
            log.info("posted merge request summary thread")
        except GitLabPostError as exc:
            summary_ok = False
            log.error(f"could not post summary thread: {exc}")
    print(
        f"gitlab-post: {inline} inline thread(s), {overview} overview thread(s), "
        f"{skipped} skipped, summary={'yes' if summary and summary_ok else 'no'}",
        flush=True,
    )
    return 0 if summary_ok else 1


def _post_diff_thread(
    url: str,
    token: str,
    body: str,
    refs: dict[str, str],
    row: DiffLine,
) -> bool:
    """Try a Changes-tab diff discussion; False if GitLab rejects the position."""
    for with_range in (True, False):
        position = discussion_position(refs, row, with_line_range=with_range)
        try:
            _post_discussion(url, token, body, position=position)
            return True
        except GitLabPostError as exc:
            log.warn(f"inline thread rejected: {exc}")
    return False


def _post_discussion(
    url: str,
    token: str,
    body: str,
    position: dict[str, Any] | None = None,
) -> Any:
    payload: dict[str, Any] = {"body": body}
    if position:
        payload["position"] = position
    return _request("POST", url, token, payload)


def _resolve_token() -> str:
    return gl.resolve_token()


def _diff_refs(api_url: str, project_id: str, mr_iid: str, token: str) -> dict[str, str]:
    return gl.diff_refs(api_url, project_id, mr_iid, token, do_request=_request)


def _fetch_mr_patch(api_url: str, project_id: str, mr_iid: str, token: str) -> str:
    return gl.fetch_mr_patch(api_url, project_id, mr_iid, token, do_request=_request)


def _request(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> Any:
    return gl.request(method, url, token, payload)


def _quote(project_id: str) -> str:
    return gl.quote(project_id)
