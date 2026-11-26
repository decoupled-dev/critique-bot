"""The GitLab and Jira instances crit may use, and their tokens.

``config.load_config`` sets them once per process (like the egress policy); the review,
the agent's tools, and ``gitlab-post`` read them here. Tokens come only from the person's
own ``~/.crit/config.json`` (``crit config --gitlab-token`` / ``--jira-token``) or the
environment, never from a shared or company config file. They are registered with
redaction, so they never reach a transcript, the chat, or the log file.

- GitLab: ``gitlab.base_url`` and a personal access token. The projects the token can
  reach (as a member) are the projects crit may review, post to, and clone, fetch,
  pull, or push in agent commands (see ``gitlab_access``).
- Jira: ``jira.base_url``, a token (a personal access token on Jira Server/Data Center,
  sent as Bearer; an API token on Jira Cloud, sent with ``jira.email`` as Basic), read
  only (see ``jira``).
- The chat site's own sign-in (``site_login``): a username and password the browser gives
  when the site asks for an HTTP or Windows (Negotiate/NTLM) sign-in, which Windows answers
  with the signed-in account and a Linux machine without SSO cannot. Sent only to
  ``site_login.host`` (the chat url's host unless set).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Integrations:
    gitlab_url: str = ""
    gitlab_token: str = field(default="", repr=False)
    jira_url: str = ""
    jira_email: str = ""
    jira_token: str = field(default="", repr=False)
    site_user: str = ""
    site_password: str = field(default="", repr=False)
    site_host: str = ""

    @property
    def site_login_ready(self) -> bool:
        return bool(self.site_user and self.site_password and self.site_host)

    @property
    def gitlab_host(self) -> str:
        return _host(self.gitlab_url)

    @property
    def jira_host(self) -> str:
        return _host(self.jira_url)

    @property
    def gitlab_ready(self) -> bool:
        return bool(self.gitlab_url and self.gitlab_token)

    @property
    def jira_ready(self) -> bool:
        return bool(self.jira_url and self.jira_token)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".") if url else ""


_current = Integrations()


def configure(
    *, gitlab_url: str = "", gitlab_token: str = "", jira_url: str = "", jira_email: str = "", jira_token: str = "",
    site_user: str = "", site_password: str = "", site_host: str = "",
) -> Integrations:
    """Set the process-wide instances and tokens (called by config.load_config)."""
    global _current
    from critique_bot import redaction

    for secret in (gitlab_token, jira_token, site_password):
        if secret:
            redaction.add_secret_value(secret)
    _current = Integrations(
        gitlab_url=gitlab_url.rstrip("/"), gitlab_token=gitlab_token,
        jira_url=jira_url.rstrip("/"), jira_email=jira_email, jira_token=jira_token,
        site_user=site_user, site_password=site_password, site_host=(site_host or "").lower().rstrip("."),
    )
    return _current


def current() -> Integrations:
    return _current
