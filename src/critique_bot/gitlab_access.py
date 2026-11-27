# SPDX-License-Identifier: Apache-2.0
"""Which GitLab projects crit may use: the ones the person's access token can reach.

There is no separate list to keep: the token decides. crit asks GitLab for the projects
the token's user is a member of (``/projects?membership=true``) and caches the answer
for a few minutes. Those projects, and only those, may be

- reviewed and commented on (``gitlab.require_project_access``);
- cloned, fetched, pulled, pushed, or listed (``ls-remote``) by the agent's commands,
  even when ``"egress": "strict"`` blocks every other network command
  (:func:`git_network_command`).

A git command is let through only when it is one plain ``git <clone|fetch|pull|push|
ls-remote> ...`` (no ``;``, ``&&``, pipes, or substitutions), its remote, after git's own
``insteadOf`` rewriting, is on the configured GitLab host and in the token's projects,
and it uses no option that runs another program (``-c``, ``--upload-pack``, ...). crit
then adds its own ``-c`` settings, so a repository's config cannot swap in another
program or credential helper either, and lets this one command use the network. Over
https the token goes as an ``Authorization`` header scoped to the GitLab host only.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from critique_bot import egress, integrations, redaction

CACHE_SECONDS = 600.0
MAX_PAGES = 50  # 100 projects per page
TIMEOUT = 20.0

_GIT_NETWORK = ("clone", "fetch", "pull", "push", "ls-remote")
#: Options that make git run something else, follow other remotes, or change its config.
_FORBIDDEN = re.compile(
    r"^(?:-c|--config(?:=.*)?|--upload-pack(?:=.*)?|-u|--receive-pack(?:=.*)?|--exec(?:=.*)?|--template(?:=.*)?"
    r"|--recurse-submodules(?:=.*)?|--recursive|--shallow-submodules|--separate-git-dir(?:=.*)?|--server-option(?:=.*)?|-o"
    r"|--reference(?:-if-able)?(?:=.*)?)$"
)
#: Options of these subcommands that take the next word as their value.
_WITH_VALUE = frozenset({
    "-b", "--branch", "--depth", "--origin", "--shallow-since", "--shallow-exclude", "--jobs", "-j", "--filter",
    "--push-option", "--deepen", "--negotiation-tip", "--refmap", "--sort",
})
_SHELL_CONTROL = re.compile(r"[;&|`<>\n]|\$\(")
#: Proxy variables the offline session points at a closed port; restored for an allowed git command.
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy")


class AccessError(Exception):
    """GitLab could not say which projects the token reaches (network, token, or instance)."""


@dataclass
class _Cache:
    key: tuple[str, str] = ("", "")
    at: float = 0.0
    paths: dict[str, str] = field(default_factory=dict)  # lower-case path -> path
    ids: dict[str, str] = field(default_factory=dict)  # project id -> path


_CACHE = _Cache()
_LOCK = threading.Lock()


def _api(base_url: str) -> str:
    return base_url.rstrip("/") + "/api/v4"


def _check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" and host not in {"localhost", "127.0.0.1", "::1"}:
        raise AccessError(f"refusing to send the GitLab token over {parts.scheme or 'no scheme'}://; use https")
    if not egress.policy().permits(host):
        raise AccessError(f"egress does not allow {host}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None  # a redirect would carry the token to another address


def _get(url: str, token: str) -> tuple[object, dict[str, str]]:
    _check_url(url)
    request = urllib.request.Request(url, headers={"PRIVATE-TOKEN": token, "Accept": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            body = response.read()
            headers = {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AccessError("GitLab did not accept the token (401/403): check it with crit config --gitlab-token") from exc
        raise AccessError(f"GitLab answered {exc.code} for {urllib.parse.urlsplit(url).path}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise AccessError(f"could not reach GitLab: {exc}") from exc
    try:
        return json.loads(body.decode("utf-8")), headers
    except ValueError as exc:
        raise AccessError("GitLab's answer was not JSON") from exc


def accessible_projects(*, force: bool = False) -> dict[str, str]:
    """``{lower-case path: path}`` of the projects the configured token's user is a member of."""
    settings = integrations.current()
    if not settings.gitlab_ready:
        raise AccessError("no GitLab address or token: crit config --gitlab-url URL and crit config --gitlab-token")
    key = (settings.gitlab_url, settings.gitlab_token)
    with _LOCK:
        if not force and _CACHE.key == key and time.monotonic() - _CACHE.at < CACHE_SECONDS:
            return dict(_CACHE.paths)
    paths: dict[str, str] = {}
    ids: dict[str, str] = {}
    page = "1"
    for _ in range(MAX_PAGES):
        url = (f"{_api(settings.gitlab_url)}/projects?membership=true&simple=true&archived=false"
               f"&per_page=100&page={page}&order_by=id&sort=asc")
        data, headers = _get(url, settings.gitlab_token)
        for item in data if isinstance(data, list) else []:
            path = str(item.get("path_with_namespace") or "") if isinstance(item, dict) else ""
            if path:
                paths[path.lower()] = path
                ids[str(item.get("id"))] = path
        page = headers.get("x-next-page", "")
        if not page:
            break
    with _LOCK:
        _CACHE.key, _CACHE.at, _CACHE.paths, _CACHE.ids = key, time.monotonic(), paths, ids
    return dict(paths)


def project_allowed(project: str) -> bool:
    """True when ``project`` (a path like group/app, or a numeric id) is one of the token's."""
    text = str(project or "").strip().strip("/")
    if not text:
        return False
    paths = accessible_projects()
    if text.isdigit():
        with _LOCK:
            return text in _CACHE.ids
    return text.lower() in paths


def forget() -> None:
    with _LOCK:
        _CACHE.key, _CACHE.at, _CACHE.paths, _CACHE.ids = ("", ""), 0.0, {}, {}


# --------------------------------------------------------------------------- git remotes


def remote_project(url: str) -> tuple[str, str] | None:
    """``(host, group/project)`` of a git remote URL (https, ssh://, or scp-like git@host:path)."""
    text = str(url or "").strip()
    match = re.match(r"^(?:[\w.-]+@)?([\w.-]+):(?!//)([^\s]+)$", text)
    if match and "://" not in text:
        host, path = match.group(1), match.group(2)
    else:
        parts = urllib.parse.urlsplit(text)
        if parts.scheme not in {"https", "http", "ssh", "git+ssh"} or not parts.hostname:
            return None
        host, path = parts.hostname, parts.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path or ".." in path.split("/"):
        return None
    return host.lower(), path


@dataclass
class GitDecision:
    allowed: bool
    reason: str = ""
    command: str = ""  # the command to run (with crit's own -c settings)
    env: dict[str, str] = field(default_factory=dict)
    project: str = ""


def _remote_url(cwd: Path, words: list[str], sub: str, args: list[str]) -> str:
    """The URL a fetch/pull/push/ls-remote talks to (after git's insteadOf rewriting)."""
    name = args[0] if args else ""
    if name and remote_project(name):
        return name
    git = ["git", *words]
    if not name:
        upstream = subprocess.run(git + ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=str(cwd),
                                  capture_output=True, text=True, timeout=10, check=False)
        name = upstream.stdout.strip().split("/", 1)[0] if upstream.returncode == 0 and upstream.stdout.strip() else "origin"
    flag = ["--push"] if sub == "push" else []
    found = subprocess.run(git + ["remote", "get-url", *flag, name], cwd=str(cwd), capture_output=True, text=True,
                           timeout=10, check=False)
    return found.stdout.strip() if found.returncode == 0 else ""


def _split(text: str) -> list[str]:
    """Words of a command line. On Windows a backslash is a path separator, not an escape."""
    if sys.platform != "win32":
        return shlex.split(text)
    words = shlex.split(text, posix=False)
    return [word[1:-1] if len(word) >= 2 and word[0] == word[-1] and word[0] in "\"'" else word for word in words]


def _quote(word: str) -> str:
    """Quote one word for the shell the command runs in (double quotes work in cmd and PowerShell)."""
    if sys.platform != "win32":
        return shlex.quote(word)
    if word and not any(ch in word for ch in ' \t"\'&|<>^%$`;(),'):
        return word
    return '"' + word.replace('"', '""') + '"'


def git_network_command(command: str, cwd: Path) -> GitDecision:
    """Whether ``command`` is a git network command to one of the token's projects, and how to run it."""
    text = str(command or "").strip()
    if not text.startswith("git ") or _SHELL_CONTROL.search(text):
        return GitDecision(False, "not one plain git command")
    try:
        words = _split(text)
    except ValueError:
        return GitDecision(False, "the command could not be parsed")
    settings = integrations.current()
    if not settings.gitlab_ready:
        return GitDecision(False, "no GitLab token is set (crit config --gitlab-token)")
    # git [-C dir]... <sub> args
    index = 1
    prefix: list[str] = []
    while index < len(words) and words[index] == "-C" and index + 1 < len(words):
        prefix += words[index:index + 2]
        index += 2
    if index >= len(words) or words[index] not in _GIT_NETWORK:
        return GitDecision(False, "only git clone, fetch, pull, push, and ls-remote may reach GitLab")
    sub = words[index]
    rest = words[index + 1:]
    positional: list[str] = []
    skip = False
    for word in rest:
        if skip:
            skip = False
            continue
        if _FORBIDDEN.match(word):
            return GitDecision(False, f"git option {word} is not allowed in a network command")
        if word in _WITH_VALUE:
            skip = True
            continue
        if word == "--":
            continue
        if not word.startswith("-"):
            positional.append(word)
    workdir = Path(cwd)
    for position in range(0, len(prefix), 2):
        target = Path(prefix[position + 1])
        workdir = target if target.is_absolute() else workdir / target
    if sub == "clone":
        if not positional:
            return GitDecision(False, "git clone needs the repository URL")
        url = positional[0]
    else:
        try:
            url = _remote_url(workdir, [], sub, positional)
        except (OSError, subprocess.TimeoutExpired):
            url = ""
    found = remote_project(url)
    if found is None:
        return GitDecision(False, f"the remote {url or '(none)'} is not a GitLab project URL")
    host, path = found
    if host != settings.gitlab_host:
        return GitDecision(False, f"the remote is on {host}, not the configured GitLab ({settings.gitlab_host})")
    try:
        if not project_allowed(path):
            return GitDecision(False, f"the GitLab token cannot reach {path} (crit uses only the token's projects)")
    except AccessError as exc:
        return GitDecision(False, str(exc))
    safety = [
        "-c", "core.sshCommand=ssh", "-c", "core.gitProxy=", "-c", "credential.helper=",
        "-c", "submodule.recurse=false", "-c", "fetch.recurseSubmodules=false", "-c", "protocol.file.allow=never",
    ]
    env = {key: os.environ.get(key, "") for key in _PROXY_KEYS}
    env["GIT_ALLOW_PROTOCOL"] = "https:ssh"
    # an earlier export of a trace variable would print the Authorization header
    env.update({name: "" for name in ("GIT_TRACE", "GIT_TRACE_CURL", "GIT_TRACE_PACKET", "GIT_CURL_VERBOSE",
                                       "GIT_TRACE2", "GIT_TRACE2_EVENT", "GIT_TRACE2_PERF", "GIT_SSH_COMMAND", "GIT_SSH",
                                       "GIT_ASKPASS", "SSH_ASKPASS", "GIT_PROXY_COMMAND", "GIT_EXEC_PATH")})
    header = base64.b64encode(f"oauth2:{settings.gitlab_token}".encode()).decode()
    redaction.add_secret_value(header)
    origin = f"https://{settings.gitlab_host}/"
    env.update({
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{origin}.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {header}",
    })
    rebuilt = " ".join(_quote(word) for word in ["git", *prefix, *safety, sub, *rest])
    return GitDecision(True, "", rebuilt, env, path)
