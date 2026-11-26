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

from critique_bot import egress, env_names, log
from critique_bot import gitlab_access, integrations, jira
from critique_bot.review_comments import parse_diff_lines

TOKEN_ENV = ("CRIT_GITLAB_TOKEN", "GITLAB_TOKEN", "CI_JOB_TOKEN")
ENV_GITLAB_URL = "CRIT_GITLAB_URL"
#: GitLab calls ignore http(s)_proxy by default, so a proxy set for other
#: traffic never sees the PRIVATE-TOKEN. Set this to a proxy URL to send GitLab
#: calls through it, or to ``env`` to use the usual proxy variables.
ENV_GITLAB_PROXY = "CRIT_GITLAB_PROXY"
REQUEST_TIMEOUT_SECONDS = 60

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
    #: The Jira issues behind ``tickets`` (summary, status, description), when Jira is set up.
    ticket_details: str = ""
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
    """CRIT_GITLAB_TOKEN or GITLAB_TOKEN, else the token in the person's own config
    (crit config --gitlab-token), else CI_JOB_TOKEN."""
    for name in TOKEN_ENV:
        value = (env_names.get(name) or "").strip()
        if name == "CI_JOB_TOKEN" and integrations.current().gitlab_token:
            return integrations.current().gitlab_token
        if value:
            if name == "CI_JOB_TOKEN":
                log.warn(
                    "CI_JOB_TOKEN cannot create MR notes; "
                    "set CRIT_GITLAB_TOKEN to a project access token (scope api)"
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
    """Resolve API URL, project, and MR IID from CLI, CI, or an MR URL.

    ``gitlab.base_url`` in config (or ``CRIT_GITLAB_URL``) is the instance.
    Project ID and MR IID are always taken from the current job: CLI flags,
    ``CI_PROJECT_ID`` / ``CI_MERGE_REQUEST_IID``, or ``--mr-url`` /
    ``CI_MERGE_REQUEST_URL``.
    """
    cfg_base = ""
    if gitlab_cfg is not None:
        cfg_base = _s(getattr(gitlab_cfg, "base_url", ""))

    parsed = parse_mr_url(
        _s(mr_url) or os.environ.get("CI_MERGE_REQUEST_URL") or ""
    )
    parsed_base = parsed[0] if parsed else ""
    parsed_project = parsed[1] if parsed else ""
    parsed_iid = parsed[2] if parsed else ""

    # The token only goes to a host someone configured: never to the host of a
    # pasted merge-request URL alone.
    resolved_api = (
        _s(api_url)
        or _s(os.environ.get("CI_API_V4_URL"))
        or api_v4_url(
            _s(env_names.get(ENV_GITLAB_URL))
            or cfg_base
            or _s(os.environ.get("CI_SERVER_URL"))
        )
    ).rstrip("/")
    if parsed_base:
        if not resolved_api:
            raise GitLabError(
                f"refusing to trust the host of the merge-request URL ({_host(parsed_base)}): "
                "set gitlab.base_url in config.json (or CRIT_GITLAB_URL / --api-url) "
                "to your GitLab instance first"
            )
        if _host(parsed_base) != _host(resolved_api):
            raise GitLabError(
                f"merge-request URL host {_host(parsed_base)} does not match the "
                f"configured GitLab host {_host(resolved_api)}; refusing to send the token there"
            )
    resolved_project = (
        _s(project_id)
        or _s(os.environ.get("CI_PROJECT_ID"))
        or _s(os.environ.get("CI_PROJECT_PATH"))
        or parsed_project
    )
    resolved_iid = (
        _s(mr_iid)
        or _s(os.environ.get("CI_MERGE_REQUEST_IID"))
        or parsed_iid
    )
    resolved_token = _s(token) or resolve_token()
    return GitLabTarget(
        api_url=resolved_api,
        project_id=resolved_project,
        mr_iid=resolved_iid,
        token=resolved_token,
    )


def require_project_access(target: "GitLabTarget") -> None:
    """Refuse a project the person's GitLab token cannot reach (as a member).

    Applies when the target uses the configured personal access token on the configured
    host; a CI job token is scoped to its own project by GitLab already.
    """
    settings = integrations.current()
    if not settings.gitlab_ready or target.token != settings.gitlab_token:
        return
    if _host(target.api_url).split(":")[0] != settings.gitlab_host:
        return
    try:
        allowed = gitlab_access.project_allowed(target.project_id)
    except gitlab_access.AccessError as exc:
        raise GitLabError(f"could not check which projects the GitLab token reaches: {exc}") from exc
    if not allowed:
        raise GitLabError(
            f"the GitLab token cannot reach project {target.project_id}: crit uses only the projects its token's "
            "user is a member of"
        )


def _host(url: str) -> str:
    parts = urllib.parse.urlsplit(url or "")
    host = (parts.hostname or "").lower()
    return f"{host}:{parts.port}" if parts.port else host


def quote(project_id: str) -> str:
    if project_id.isdigit():
        return project_id
    return urllib.parse.quote(project_id, safe="")


def _require_safe_url(url: str) -> None:
    """The token goes only over https (http only to this PC, for a local test instance)."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if (parts.scheme == "https" and host) or (parts.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}):
        try:
            egress.require(host, "posting to GitLab")
        except PermissionError as exc:
            raise GitLabError(str(exc)) from exc
        return
    raise GitLabError(
        f"refusing to send the GitLab token to {parts.scheme or '(no scheme)'}://{host or '?'}: "
        "gitlab.base_url must start with https://"
    )


def request(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    _require_safe_url(url)
    headers = {"PRIVATE-TOKEN": token}
    if token and token == os.environ.get("CI_JOB_TOKEN"):
        headers = {"JOB-TOKEN": token}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    extra = _payload_log(payload)
    log.info(f"GitLab {method} {_endpoint_label(url)} {url}" + extra)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        # https checked by _require_safe_url
        with _urlopen(req) as resp:  # nosec B310
            status = _http_status(resp)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        log.error(
            f"GitLab {exc.code} {method} {_endpoint_label(url)} {url}: {detail}"
        )
        raise GitLabError(f"HTTP {exc.code} {method} {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        log.error(f"GitLab {method} {_endpoint_label(url)} {url} failed: {reason}")
        raise GitLabError(f"{method} {url} failed: {reason}") from exc
    result = None
    if raw:
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = None
    summary = _response_log(result)
    log.info(
        f"GitLab {status} {method} {_endpoint_label(url)} {url}"
        + (f" {summary}" if summary else "")
    )
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would carry PRIVATE-TOKEN to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise GitLabError(
            f"GitLab answered HTTP {code} redirect to {newurl}; refusing to follow it with "
            "the token. Set gitlab.base_url to the final https:// address of your instance"
        )


def _proxy_handler() -> urllib.request.ProxyHandler:
    """No proxy unless CRIT_GITLAB_PROXY opts in (a URL, or ``env``)."""
    choice = (env_names.get(ENV_GITLAB_PROXY) or "").strip()
    if not choice:
        return urllib.request.ProxyHandler({})
    if choice.lower() == "env":
        return urllib.request.ProxyHandler()
    return urllib.request.ProxyHandler({"http": choice, "https": choice})


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_proxy_handler(), _NoRedirect())


def _urlopen(req: urllib.request.Request) -> Any:
    return _opener().open(req, timeout=REQUEST_TIMEOUT_SECONDS)


def _endpoint_label(url: str) -> str:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    if path.endswith("/discussions"):
        return "discussions"
    if path.endswith("/versions"):
        return "versions"
    if "/diffs" in path:
        return "diffs"
    if path.endswith("/changes"):
        return "changes"
    if "/merge_requests/" in path:
        return "merge_request"
    return "api"


def _payload_log(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    parts: list[str] = []
    position = payload.get("position")
    body = payload.get("body")
    if isinstance(position, dict):
        parts.append("inline")
        parts.append(
            log.kv(
                path=position.get("new_path") or position.get("old_path"),
                new_line=position.get("new_line"),
                old_line=position.get("old_line"),
            )
        )
    elif isinstance(body, str):
        parts.append("overview")
    if isinstance(body, str):
        parts.append(f"chars={len(body)}")
        preview = log.preview(body, 80)
        if preview:
            parts.append(f"body={preview!r}")
    packed = " ".join(part for part in parts if part)
    return f" {packed}" if packed else ""


def _response_log(data: Any) -> str:
    if isinstance(data, list):
        return f"items={len(data)}"
    if not isinstance(data, dict):
        return ""
    parts: list[str] = []
    if data.get("id") is not None:
        parts.append(f"id={data['id']}")
    if data.get("iid") is not None:
        parts.append(f"iid={data['iid']}")
    if data.get("web_url"):
        parts.append(f"url={data['web_url']}")
    notes = data.get("notes")
    if isinstance(notes, list) and notes and isinstance(notes[0], dict):
        note = notes[0]
        if note.get("id") is not None:
            parts.append(f"note={note['id']}")
        if note.get("web_url"):
            parts.append(f"note_url={note['web_url']}")
    return " ".join(parts)


def _http_status(resp: Any) -> str:
    status = getattr(resp, "status", None)
    if status is None:
        status = getattr(resp, "code", None)
    if status is None and hasattr(resp, "getcode"):
        try:
            status = resp.getcode()
        except Exception:
            status = None
    return str(status) if status is not None else "ok"


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    reason = str(getattr(exc, "reason", "") or "").strip()
    try:
        raw = exc.read().decode("utf-8", "replace")[:1500]
    except Exception:
        raw = ""
    parsed = _gitlab_error_message(raw)
    bits = [reason] if reason else []
    retry = ""
    headers = getattr(exc, "headers", None)
    if exc.code == 429 and headers is not None:
        retry = str(headers.get("Retry-After") or headers.get("retry-after") or "")
        if retry:
            bits.append(f"Retry-After={retry}")
    if parsed:
        bits.append(parsed)
    elif raw.strip():
        bits.append(raw.strip())
    return " | ".join(bit for bit in bits if bit) or f"HTTP {exc.code}"


def _gitlab_error_message(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text[:500]
    if not isinstance(obj, dict):
        return text[:500]
    parts: list[str] = []
    for key in ("error_code", "error", "error_description", "message"):
        value = obj.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, dict):
            nested = []
            for name, item in value.items():
                if isinstance(item, list):
                    nested.append(f"{name}: {', '.join(str(v) for v in item)}")
                else:
                    nested.append(f"{name}: {item}")
            if nested:
                parts.append("; ".join(nested))
        elif isinstance(value, list):
            parts.append(", ".join(str(item) for item in value))
        else:
            parts.append(str(value))
    return " | ".join(parts)[:500]


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
            "and set CI/CD variable CRIT_GITLAB_TOKEN (unprotected, masked)"
        )
    require_project_access(target)
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
    details = ""
    jira_keys = [ticket for ticket in tickets if not ticket.startswith("#")][:5]
    if jira_keys and jira.ready():
        details = jira.issues_text(jira_keys)
    return MrContext(
        title=title,
        description=description,
        web_url=web_url,
        iid=_s(data.get("iid")) or target.mr_iid,
        source_branch=source,
        target_branch=target_branch,
        labels=labels,
        tickets=tickets,
        ticket_details=details,
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
    if ctx.ticket_details:
        lines.append("")
        lines.append("Jira issues (what the change is for):")
        lines.append(ctx.ticket_details)
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
