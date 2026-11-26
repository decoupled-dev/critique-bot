"""Read-only Jira: an issue, or a JQL search, as short text for the model.

The instance and token come from :mod:`integrations` (``crit config --jira-url`` and
``--jira-token``; ``--jira-email`` too for Jira Cloud). Jira Server and Data Center take
a personal access token as ``Authorization: Bearer``; Jira Cloud takes the account
email and an API token as Basic. crit only reads: nothing here changes an issue.

The token goes only over https (http only to this PC), only to the configured host,
and never through a redirect.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from critique_bot import egress, integrations, redaction

TIMEOUT = 20.0
KEY_RE = re.compile(r"\b[A-Z][A-Z0-9_]{1,9}-\d{1,7}\b")
_STRICT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,9}-\d{1,7}$")
DESCRIPTION_CHARS = 3000
COMMENT_CHARS = 600
COMMENTS = 5
_FIELDS = "summary,status,issuetype,priority,assignee,reporter,labels,components,fixVersions,description,comment,issuelinks,created,updated,parent"
#: Words that look like keys but are not tickets.
_NOT_KEYS = frozenset({"UTF", "SHA", "ISO", "RFC", "CVE", "AES", "MD", "HTTP", "TLS", "API", "ARM", "X86", "GPL", "LGPL", "ASCII"})


class JiraError(Exception):
    """Jira could not answer (setup, network, token, or a missing issue)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def ready() -> bool:
    return integrations.current().jira_ready


def _auth() -> dict[str, str]:
    settings = integrations.current()
    if settings.jira_email:
        raw = base64.b64encode(f"{settings.jira_email}:{settings.jira_token}".encode()).decode()
        redaction.add_secret_value(raw)
        return {"Authorization": f"Basic {raw}"}
    return {"Authorization": f"Bearer {settings.jira_token}"}


def _get(path: str, query: dict[str, str] | None = None) -> Any:
    settings = integrations.current()
    if not settings.jira_ready:
        raise JiraError("Jira is not set up: crit config --jira-url URL and crit config --jira-token")
    url = settings.jira_url + path + ("?" + urllib.parse.urlencode(query) if query else "")
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" and host not in {"localhost", "127.0.0.1", "::1"}:
        raise JiraError("refusing to send the Jira token without https")
    if not egress.policy().permits(host):
        raise JiraError(f"egress does not allow {host}")
    request = urllib.request.Request(url, headers={**_auth(), "Accept": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise JiraError("Jira did not accept the token (401/403): check crit config --jira-token (and --jira-email on Cloud)") from exc
        if exc.code == 404:
            raise JiraError("not found (or the token cannot see it)") from exc
        raise JiraError(f"Jira answered {exc.code}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise JiraError(f"could not reach Jira: {exc}") from exc
    except ValueError as exc:
        raise JiraError("Jira's answer was not JSON") from exc


def _cloud() -> bool:
    return bool(integrations.current().jira_email)


def get_issue(key: str) -> dict:
    key = key.strip().upper()
    if not _STRICT_KEY.match(key):
        raise JiraError(f"{key!r} is not an issue key such as CAR-123")
    data = _get(f"/rest/api/2/issue/{urllib.parse.quote(key)}", {"fields": _FIELDS})
    return data if isinstance(data, dict) else {}


def search(jql: str, *, limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit), 50))
    fields = "summary,status,issuetype,priority,assignee,updated"
    if _cloud():
        data = _get("/rest/api/3/search/jql", {"jql": jql, "maxResults": str(limit), "fields": fields})
    else:
        data = _get("/rest/api/2/search", {"jql": jql, "maxResults": str(limit), "fields": fields})
    issues = data.get("issues") if isinstance(data, dict) else None
    return issues if isinstance(issues, list) else []


def _name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("displayName") or value.get("name") or value.get("value") or "")
    return str(value or "")


def _text(value: Any) -> str:
    """Plain text of a field: v2 gives wiki text; Cloud v3 search fields may be ADF documents."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "text":
                    parts.append(str(node.get("text") or ""))
                for child in node.get("content") or []:
                    walk(child)
                if node.get("type") in {"paragraph", "heading", "listItem", "codeBlock"}:
                    parts.append("\n")
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(value)
        return "".join(parts).strip()
    return ""


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f" …[+{len(text) - limit} chars]"


def format_issue(issue: dict) -> str:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    key = str(issue.get("key") or "?")
    head = [
        f"{key}: {fields.get('summary') or ''}".strip(),
        " · ".join(item for item in (
            _name(fields.get("issuetype")), f"status {_name(fields.get('status'))}" if fields.get("status") else "",
            f"priority {_name(fields.get('priority'))}" if fields.get("priority") else "",
            f"assignee {_name(fields.get('assignee')) or 'none'}",
            f"parent {fields['parent'].get('key')}" if isinstance(fields.get("parent"), dict) else "",
        ) if item),
    ]
    labels = fields.get("labels") or []
    components = [_name(item) for item in fields.get("components") or []]
    versions = [_name(item) for item in fields.get("fixVersions") or []]
    for title, values in (("labels", labels), ("components", components), ("fix versions", versions)):
        if values:
            head.append(f"{title}: {', '.join(str(v) for v in values)}")
    links = []
    for link in fields.get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        kind = link.get("type") or {}
        if isinstance(link.get("outwardIssue"), dict):
            links.append(f"{kind.get('outward', 'relates to')} {link['outwardIssue'].get('key')}")
        elif isinstance(link.get("inwardIssue"), dict):
            links.append(f"{kind.get('inward', 'relates to')} {link['inwardIssue'].get('key')}")
    if links:
        head.append("links: " + "; ".join(links[:10]))
    description = _text(fields.get("description"))
    if description:
        head += ["description:", _clip(description, DESCRIPTION_CHARS)]
    comments = (fields.get("comment") or {}).get("comments") if isinstance(fields.get("comment"), dict) else None
    if comments:
        head.append(f"comments ({len(comments)}; the last {min(len(comments), COMMENTS)}):")
        for comment in comments[-COMMENTS:]:
            author = _name(comment.get("author")) if isinstance(comment, dict) else ""
            body = _text(comment.get("body")) if isinstance(comment, dict) else ""
            when = str(comment.get("created") or "")[:10] if isinstance(comment, dict) else ""
            head.append(f"- {author} {when}: {_clip(body, COMMENT_CHARS)}")
    return redaction_safe("\n".join(head))


def format_search(issues: list[dict]) -> str:
    rows = []
    for issue in issues:
        fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
        rows.append(
            f"{issue.get('key')}: {fields.get('summary') or ''} · {_name(fields.get('status'))} · "
            f"{_name(fields.get('assignee')) or 'unassigned'}"
        )
    return redaction_safe("\n".join(rows) if rows else "(no issues match)")


def redaction_safe(text: str) -> str:
    return redaction.redact_secrets(text)[0] if hasattr(redaction, "redact_secrets") else text


def keys_in(text: str, *, limit: int = 3) -> list[str]:
    """Issue keys mentioned in ``text`` (CAR-123), without look-alikes such as UTF-8."""
    found: list[str] = []
    for match in KEY_RE.finditer(text or ""):
        key = match.group(0)
        if key.split("-", 1)[0] in _NOT_KEYS or key in found:
            continue
        found.append(key)
        if len(found) >= limit:
            break
    return found


def issues_text(keys: list[str]) -> str:
    """The issues behind ``keys``, formatted; keys Jira does not know are left out quietly."""
    blocks = []
    for key in keys:
        try:
            blocks.append(format_issue(get_issue(key)))
        except JiraError:
            continue
    return "\n\n".join(blocks)
