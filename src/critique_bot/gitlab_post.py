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
    comments_from_summary,
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
            "need project and merge request: pass --project-id and --mr-iid "
            "(or --mr-url), or set CI_PROJECT_ID / CI_MERGE_REQUEST_IID"
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
    discussions = f"{gl.mr_api_url(api_url, project_id, mr_iid)}/discussions"
    log.info(
        "GitLab target "
        + log.kv(
            api=api_url,
            project=project_id,
            mr=mr_iid,
            discussions=discussions,
        )
    )
    refs = _diff_refs(api_url, project_id, mr_iid, resolved_token)
    log.info(
        "GitLab diff refs "
        + log.kv(
            base_sha=refs.get("base_sha"),
            start_sha=refs.get("start_sha"),
            head_sha=refs.get("head_sha"),
        )
    )
    inline = 0
    overview = 0
    skipped = 0

    def ensure_diff_lines() -> list[DiffLine]:
        nonlocal remote_lines
        if remote_lines is None:
            remote_lines = parse_diff_lines(
                _fetch_mr_patch(api_url, project_id, mr_iid, resolved_token)
            )
        return remote_lines or local_lines

    def lines_for(comment: InlineComment) -> DiffLine | None:
        rows = ensure_diff_lines()
        if remote_lines:
            row = resolve_comment(comment, remote_lines)
            if row is not None:
                return row
        if local_lines:
            return resolve_comment(comment, local_lines)
        if rows:
            return resolve_comment(comment, rows)
        return None

    if comments:
        log.info(f"parsed {len(comments)} inline comment(s) from JSON")
    else:
        comments = comments_from_summary(review_md, ensure_diff_lines())
        if comments:
            log.info(f"derived {len(comments)} inline comment(s) from summary")
        elif _looks_like_review_json(review_md):
            log.warn(
                "review contains JSON but no inline comments could be parsed; "
                "posting the summary only"
            )

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
                        endpoint=discussions,
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
                + log.kv(endpoint=discussions, path=comment.path, line=comment.line)
                + f" {exc}"
            )
            continue
        overview += 1
        log.info(
            "posted overview thread "
            + log.kv(endpoint=discussions, path=comment.path, line=comment.line)
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
            log.info(
                "posted merge request summary thread "
                + log.kv(endpoint=discussions)
            )
        except GitLabPostError as exc:
            summary_ok = False
            log.error(
                "could not post summary thread "
                + log.kv(endpoint=discussions)
                + f" {exc}"
            )
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
    """POST .../discussions with the position payload GitLab accepts on the diff.

    First try the fields a working PowerShell client uses: base_sha, start_sha,
    head_sha, old_path, new_path, position_type, and new_line (or old_line).
    Some GitLab versions also want line_range; retry with that if needed.
    """
    for position in _position_variants(refs, row):
        try:
            _post_discussion(url, token, body, position=position)
            return True
        except GitLabPostError as exc:
            log.warn(
                "inline thread rejected "
                + log.kv(
                    endpoint=url,
                    path=position.get("new_path"),
                    new_line=position.get("new_line"),
                    old_line=position.get("old_line"),
                    line_range=bool(position.get("line_range")),
                )
                + f" {exc}"
            )
    return False


def _position_variants(refs: dict[str, str], row: DiffLine) -> list[dict[str, Any]]:
    variants = [
        discussion_position(refs, row, with_line_range=False),
        discussion_position(refs, row, with_line_range=True),
    ]
    base = variants[0]
    if "old_line" in base and "new_line" in base:
        slim = dict(base)
        slim.pop("old_line", None)
        variants.append(slim)
        ranged = dict(slim)
        ranged["line_range"] = variants[1]["line_range"]
        variants.append(ranged)
    seen: list[dict[str, Any]] = []
    for item in variants:
        if item not in seen:
            seen.append(item)
    return seen


def _looks_like_review_json(review_md: str) -> bool:
    text = review_md or ""
    if '"path"' not in text and '"file"' not in text:
        return False
    if "```json" in text.lower():
        return True
    return any(f'"{key}"' in text for key in ("comments", "inline_comments", "findings"))


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
