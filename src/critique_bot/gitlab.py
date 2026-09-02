"""GitLab API targeting, HTTP, and merge-request context for the review prompt."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from critique_bot import log
from critique_bot.review_comments import parse_diff_lines

TOKEN_ENV = ("CRITIQUE_GITLAB_TOKEN", "GITLAB_TOKEN", "CI_JOB_TOKEN")
ENV_GITLAB_URL = "CRITIQUE_GITLAB_URL"

_MR_URL_RE = re.compile(
    r"(?P<base>https?://[^/\s]+)/(?P<project>.+)/-/merge_requests/(?P<iid>\d+)",
    re.I,
)
_JIRA_TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_GITLAB_ISSUE_RE = re.compile(
    r"\b(?:closes|fixes|resolves)\s+#(\d+)\b|#(\d+)\b",
    re.I,
)

MAX_DESCRIPTION_CHARS = 4_000
MAX_COMMIT_CHARS = 3_000
MAX_COMMITS = 20
RequestFn = Callable[..., Any]


class GitLabError(RuntimeError):
    """GitLab rejected a request or targeting is incomplete."""


@dataclass(frozen=True)
class GitLabTarget:
    api_url: str
    project_id: str
    mr_iid: str
    token: str = ""

    @property
    def mr_api_url(self) -> str:
        """GET/POST root: {base}/api/v4/projects/{id}/merge_requests/{iid}."""
        return mr_api_url(self.api_url, self.project_id, self.mr_iid)


@dataclass(frozen=True)
class MrContext:
    title: str = ""
    description: str = ""
    web_url: str = ""
    iid: str = ""
    source_branch: str = ""
    target_branch: str = ""
    labels: tuple[str, ...] = ()
    tickets: tuple[str, ...] = ()
    commits: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    patch: str = ""


def api_v4_url(base_url: str) -> str:
    """Turn a GitLab host into the API v4 root.

    ``https://gitlab.example.com`` → ``https://gitlab.example.com/api/v4``.
    A value that already ends with ``/api/v4`` is returned unchanged.
    """
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        return ""
    if raw.endswith("/api/v4"):
        return raw
    if raw.endswith("/api"):
        return raw + "/v4"
    return raw + "/api/v4"


def mr_api_url(api_url: str, project_id: str, mr_iid: str) -> str:
    root = (api_url or "").rstrip("/")
    return f"{root}/projects/{quote(project_id)}/merge_requests/{mr_iid}"


def parse_mr_url(url: str) -> tuple[str, str, str] | None:
    """Parse host, project path, and IID from a merge-request web URL."""
    match = _MR_URL_RE.search((url or "").strip())
    if not match:
        return None
    return match.group("base"), match.group("project"), match.group("iid")


def resolve_token() -> str:
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


def resolve_target(
    gitlab_cfg: Any | None = None,
    *,
    api_url: str | None = None,
    project_id: str | None = None,
    mr_iid: str | None = None,
    mr_url: str | None = None,
    token: str | None = None,
) -> GitLabTarget:
    """Resolve API URL, project, and MR IID from CLI, CI, config, or an MR URL.

    CLI arguments win, then GitLab CI variables, then ``config.json`` ``gitlab``.
    An MR web URL fills any piece that is still empty.
    """
    cfg_base = ""
    cfg_project = ""
    cfg_iid = ""
    cfg_mr_url = ""
    if gitlab_cfg is not None:
        cfg_base = _s(getattr(gitlab_cfg, "base_url", ""))
        cfg_project = _s(getattr(gitlab_cfg, "project_id", ""))
        cfg_iid = _s(getattr(gitlab_cfg, "mr_iid", ""))
        cfg_mr_url = _s(getattr(gitlab_cfg, "mr_url", ""))

    parsed = parse_mr_url(
        _s(mr_url)
        or cfg_mr_url
        or os.environ.get("CI_MERGE_REQUEST_URL")
        or ""
    )
    parsed_base = parsed[0] if parsed else ""
    parsed_project = parsed[1] if parsed else ""
    parsed_iid = parsed[2] if parsed else ""

    resolved_api = (
        _s(api_url)
        or _s(os.environ.get("CI_API_V4_URL"))
        or api_v4_url(
            _s(os.environ.get(ENV_GITLAB_URL))
            or cfg_base
            or parsed_base
            or _s(os.environ.get("CI_SERVER_URL"))
        )
    ).rstrip("/")
    resolved_project = (
        _s(project_id)
        or _s(os.environ.get("CI_PROJECT_ID"))
        or _s(os.environ.get("CI_PROJECT_PATH"))
        or cfg_project
        or parsed_project
    )
    resolved_iid = (
        _s(mr_iid)
        or _s(os.environ.get("CI_MERGE_REQUEST_IID"))
        or cfg_iid
        or parsed_iid
    )
    resolved_token = _s(token) or resolve_token()
    return GitLabTarget(
        api_url=resolved_api,
        project_id=resolved_project,
        mr_iid=resolved_iid,
        token=resolved_token,
    )


def quote(project_id: str) -> str:
    if project_id.isdigit():
        return project_id
    return urllib.parse.quote(project_id, safe="")


def request(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    headers = {"PRIVATE-TOKEN": token}
    if token and token == os.environ.get("CI_JOB_TOKEN"):
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
        raise GitLabError(f"HTTP {exc.code} {method} {url}: {detail}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def refs_from_versions(versions: Any) -> dict[str, str] | None:
    if not isinstance(versions, list):
        return None
    for item in versions:
        if not isinstance(item, dict):
            continue
        head = item.get("head_commit_sha")
        base = item.get("base_commit_sha")
        start = item.get("start_commit_sha")
        if head and base:
            return {
                "base_sha": str(base),
                "start_sha": str(start or base),
                "head_sha": str(head),
            }
    return None


def diff_refs(
    api_url: str,
    project_id: str,
    mr_iid: str,
    token: str,
    *,
    do_request: RequestFn | None = None,
) -> dict[str, str]:
    do_request = do_request or request
    env_refs = {
        "base_sha": os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") or "",
        "start_sha": os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") or "",
        "head_sha": os.environ.get("CI_COMMIT_SHA") or "",
    }
    versions_error: GitLabError | None = None
    try:
        versions = do_request(
            "GET",
            f"{mr_api_url(api_url, project_id, mr_iid)}/versions",
            token,
        )
        refs = refs_from_versions(versions)
        if refs:
            return refs
    except GitLabError as exc:
        versions_error = exc
        log.warn(f"could not load MR versions: {exc}")
    try:
        data = do_request(
            "GET",
            mr_api_url(api_url, project_id, mr_iid),
            token,
        )
    except GitLabError as exc:
        if all(env_refs.values()):
            log.warn(f"using CI sha env; could not load MR diff_refs: {exc}")
            return env_refs
        if versions_error:
            raise versions_error from exc
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
    raise GitLabError("merge request has no diff_refs yet; retry the job")


def fetch_mr_patch(
    api_url: str,
    project_id: str,
    mr_iid: str,
    token: str,
    *,
    do_request: RequestFn | None = None,
) -> str:
    do_request = do_request or request
    root = mr_api_url(api_url, project_id, mr_iid)
    endpoints = (f"{root}/diffs?per_page=100", f"{root}/changes")
    for url in endpoints:
        try:
            data = do_request("GET", url, token)
        except GitLabError as exc:
            log.warn(f"could not load MR diffs: {exc}")
            continue
        items: Any = data
        if isinstance(data, dict):
            items = data.get("changes") or data.get("diffs")
        if not isinstance(items, list) or not items:
            continue
        patch = patch_from_changes(items)
        if patch:
            return patch
    return ""


def patch_from_changes(items: list[Any]) -> str:
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        diff = str(item.get("diff") or "")
        if not diff.strip():
            continue
        stripped = diff.lstrip()
        if stripped.startswith("diff --git") or stripped.startswith("--- "):
            parts.append(diff if diff.endswith("\n") else diff + "\n")
            continue
        old_path = str(item.get("old_path") or "")
        new_path = str(item.get("new_path") or "")
        git_old = old_path or new_path
        git_new = new_path or old_path
        old_header = old_path if old_path else "/dev/null"
        new_header = new_path if new_path else "/dev/null"
        parts.append(
            f"diff --git a/{git_old} b/{git_new}\n"
            f"--- a/{old_header}\n"
            f"+++ b/{new_header}\n"
            f"{diff.rstrip()}\n"
        )
    return "".join(parts)


def fetch_mr_context(
    target: GitLabTarget,
    *,
    include_patch: bool = True,
    do_request: RequestFn | None = None,
) -> MrContext:
    """Load title, description, tickets, commits, and changed files from the MR."""
    do_request = do_request or request
    if not target.token:
        raise GitLabError(
            "no GitLab token. Create a project access token with scope `api` "
            "and set CI/CD variable CRITIQUE_GITLAB_TOKEN (unprotected, masked)"
        )
    root = target.mr_api_url
    data = do_request("GET", root, target.token)
    if not isinstance(data, dict):
        data = {}
    title = _s(data.get("title"))
    description = _s(data.get("description"))
    source = _s(data.get("source_branch"))
    target_branch = _s(data.get("target_branch"))
    web_url = _s(data.get("web_url"))
    labels = _labels(data.get("labels"))
    commits = _load_commits(root, target.token, do_request)
    issue_tickets = _load_closes_issues(root, target.token, do_request)
    tickets = extract_tickets(
        title,
        description,
        source,
        "\n".join(commits),
        *issue_tickets,
    )
    patch = ""
    if include_patch:
        patch = fetch_mr_patch(
            target.api_url,
            target.project_id,
            target.mr_iid,
            target.token,
            do_request=do_request,
        )
    changed = summarize_changed_files(patch) if patch else ()
    return MrContext(
        title=title,
        description=description,
        web_url=web_url,
        iid=_s(data.get("iid")) or target.mr_iid,
        source_branch=source,
        target_branch=target_branch,
        labels=labels,
        tickets=tickets,
        commits=commits,
        changed_files=changed,
        patch=patch,
    )


def format_mr_context(ctx: MrContext) -> str:
    """Markdown block injected into the review prompt as SYSTEM context."""
    lines: list[str] = []
    if ctx.title:
        ident = f"!{ctx.iid} " if ctx.iid else ""
        lines.append(f"Title: {ident}{ctx.title}")
    if ctx.web_url:
        lines.append(f"URL: {ctx.web_url}")
    if ctx.source_branch or ctx.target_branch:
        lines.append(
            f"Branches: {ctx.source_branch or '?'} → {ctx.target_branch or '?'}"
        )
    if ctx.tickets:
        lines.append("Tickets: " + ", ".join(ctx.tickets))
    if ctx.labels:
        lines.append("Labels: " + ", ".join(ctx.labels))
    description = _truncate(ctx.description.strip(), MAX_DESCRIPTION_CHARS)
    if description:
        lines.append("")
        lines.append("Description:")
        lines.append(description)
    if ctx.commits:
        lines.append("")
        lines.append("Commits:")
        for item in ctx.commits:
            commit_lines = item.splitlines() or [item]
            lines.append(f"- {commit_lines[0]}")
            for extra in commit_lines[1:]:
                lines.append(f"  {extra}")
    if ctx.changed_files:
        lines.append("")
        lines.append(
            "Changed files (use these paths and line numbers for inline comments):"
        )
        for item in ctx.changed_files:
            lines.append(f"- {item}")
    return "\n".join(lines).strip()


def extract_tickets(*texts: str) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        for match in _JIRA_TICKET_RE.finditer(text):
            ticket = match.group(0)
            if ticket not in seen:
                seen.add(ticket)
                found.append(ticket)
        for match in _GITLAB_ISSUE_RE.finditer(text):
            number = match.group(1) or match.group(2)
            if not number:
                continue
            ticket = f"#{number}"
            if ticket not in seen:
                seen.add(ticket)
                found.append(ticket)
    return tuple(found)


def summarize_changed_files(patch: str) -> tuple[str, ...]:
    rows = parse_diff_lines(patch)
    added: dict[str, list[int]] = {}
    deleted: dict[str, list[int]] = {}
    for row in rows:
        if row.kind == "add" and row.new_line:
            added.setdefault(row.path, []).append(row.new_line)
        elif row.kind == "del" and row.old_line:
            deleted.setdefault(row.path, []).append(row.old_line)
    paths = list(dict.fromkeys([*added, *deleted]))
    out: list[str] = []
    for path in paths:
        bits: list[str] = []
        add_span = _collapse_lines(added.get(path, []), "+")
        del_span = _collapse_lines(deleted.get(path, []), "-")
        if add_span:
            bits.append(add_span)
        if del_span:
            bits.append(del_span)
        out.append(f"{path}  {' '.join(bits)}".rstrip())
    return tuple(out)


def _collapse_lines(numbers: list[int], prefix: str) -> str:
    if not numbers:
        return ""
    ordered = sorted(set(numbers))
    spans: list[str] = []
    start = prev = ordered[0]
    for value in ordered[1:]:
        if value == prev + 1:
            prev = value
            continue
        spans.append(_span(prefix, start, prev))
        start = prev = value
    spans.append(_span(prefix, start, prev))
    return " ".join(spans)


def _span(prefix: str, start: int, end: int) -> str:
    if start == end:
        return f"{prefix}{start}"
    return f"{prefix}{start}-{end}"


def _load_commits(root: str, token: str, do_request: RequestFn) -> tuple[str, ...]:
    try:
        data = do_request("GET", f"{root}/commits?per_page=50", token)
    except GitLabError as exc:
        log.warn(f"could not load MR commits: {exc}")
        return ()
    if not isinstance(data, list):
        return ()
    items: list[str] = []
    used = 0
    for row in data[:MAX_COMMITS]:
        if not isinstance(row, dict):
            continue
        sha = _s(row.get("short_id") or row.get("id"))[:8]
        title = _s(row.get("title"))
        message = _s(row.get("message"))
        block = f"{sha} {title}".strip()
        body = message
        if title and message.startswith(title):
            body = message[len(title) :].strip()
        if body and body != title:
            extra = _truncate(body, 400)
            block = f"{block}\n{extra}" if extra else block
        remaining = MAX_COMMIT_CHARS - used
        if remaining <= 0:
            break
        if len(block) > remaining:
            block = _truncate(block, remaining)
        items.append(block)
        used += len(block)
    return tuple(items)


def _load_closes_issues(root: str, token: str, do_request: RequestFn) -> tuple[str, ...]:
    try:
        data = do_request("GET", f"{root}/closes_issues", token)
    except GitLabError as exc:
        log.warn(f"could not load related issues: {exc}")
        return ()
    if not isinstance(data, list):
        return ()
    tickets: list[str] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        iid = row.get("iid")
        if iid:
            tickets.append(f"#{iid}")
        title = _s(row.get("title"))
        if title:
            tickets.append(title)
    return tuple(tickets)


def _labels(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, list):
        return tuple(_s(item) for item in raw if _s(item))
    if isinstance(raw, str) and raw.strip():
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    return ()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0]
    if len(cut) < limit // 2:
        cut = text[:limit]
    return cut.rstrip() + "\n…"


def _s(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()
