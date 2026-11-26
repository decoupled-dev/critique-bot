"""``crit config``: see and change crit's settings without editing JSON.

``crit config`` with no flags prints the settings in plain words. With flags it changes
them, for scripts, CI, and IT rollout as much as for people:

    crit config --config-file /path/to/crit.json
    crit config --url https://ai.company.com/ --redact "Acme, Globex"
    crit config --backend local --local-url http://127.0.0.1:8080 --model qwen2.5-coder-14b

With a config file (``config_file``, ``CRIT_CONFIG_FILE``, or the system location), the
keys it sets are layered under the user's own (see ``config.merge_company``); the locked ones
cannot be changed here.

Every change is checked before anything is written, and the file is replaced in one step
(owner-only, 0600 on POSIX). Which file: see :func:`resolve_config_path`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from critique_bot import egress, env_names, redaction
from critique_bot.config import (
    COMPANY_CONFIG_KEY,
    USER_PREF_KEYS,
    BACKEND_BROWSER,
    BACKEND_LOCAL,
    DEFAULT_LOCAL_URL,
    ENV_GITLAB_TOKENS,
    ENV_JIRA_EMAIL,
    ENV_JIRA_TOKENS,
    ENV_SITE_PASSWORDS,
    ENV_SITE_USER,
    ENV_JIRA_URL,
    ENV_LOCAL_KEY,
    ENV_CONFIG,
    PLACEHOLDER_MODEL,
    SELECTOR_KEYS,
    STARTER_SELECTORS,
    ConfigError,
    _api_key_in,
    _host_list,
    _local_url,
    backend_kind,
    clean_gitlab_url,
    clean_https_url,
    check_company_file,
    company_summary,
    find_company_settings,
    has_settings,
    load_config,
    merge_company,
    set_user_preferences,
    read_config_dict,
    user_api_key,
    user_config_path,
    write_config_file,
)

CONFIG_FILE_NAME = "config.json"

#: Told when a command needs settings and cannot ask (no terminal).
NOT_SET_UP = (
    "crit is not set up yet. Run crit in a terminal to answer the setup questions, or set it up in one line: "
    "crit config --url https://ai.company.com/ --selector prompt_input=CSS --selector assistant_messages=CSS (Web UI), "
    "or crit config --backend local --local-url http://IP:PORT (local model), "
    "or crit config --config-file /path/to/crit.json. See the settings with: crit config"
)

#: Said when a change touches a key the config file sets.
LOCKED = "This is set in the config file {path}; change it there."


# --------------------------------------------------------------------------- which file


@dataclass(frozen=True)
class ConfigLocation:
    """The config file a command uses, and why that one."""

    path: Path
    #: "flag" (--config), "env" (CRIT_CONFIG), "project" (.crit/settings.json),
    #: "project-root" (config.json next to the code), or "user" (config.user_config_path()).
    source: str

    @property
    def exists(self) -> bool:
        """The file is there and sets crit up (a user config holding only preferences does not)."""
        if not self.path.is_file():
            return False
        if self.source != "user":
            return True
        try:
            return has_settings(read_config_dict(self.path))
        except ConfigError:
            return True  # load_config reports what is wrong with it


_SOURCE_WORDS = {
    "flag": "named with --config",
    "env": f"named by {ENV_CONFIG}",
    "project": "stored for this project in .crit/settings.json",
    "project-root": "config.json in this project",
    "user": "your user settings",
}


def resolve_config_path(explicit: str | Path | None = None, repo_dir: str | Path | None = None) -> ConfigLocation:
    """Which config file a command uses.

    ``--config`` > ``CRIT_CONFIG`` > the config stored for the project in
    ``.crit/settings.json`` > an untracked ``config.json`` in the project root > the user
    config (``config.user_config_path()``). The first two count even when the file is
    missing (the caller reports it or writes it); the project's only when the file exists;
    the user config may not exist yet (first run).
    """
    if explicit:
        return ConfigLocation(Path(explicit).expanduser(), "flag")
    from_env = (env_names.get(ENV_CONFIG) or "").strip()
    if from_env:
        return ConfigLocation(Path(from_env).expanduser(), "env")
    from critique_bot.project_home import BotHomeError, find_bot_home, git_tracks

    start = Path(repo_dir or ".").resolve()
    home = find_bot_home(start)
    if home is not None:
        try:
            stored = home.config_file()
        except BotHomeError as exc:
            raise ConfigError(str(exc)) from exc
        if stored is not None and stored.is_file():
            return ConfigLocation(stored, "project")
    root = home.root if home is not None else start
    candidate = root / CONFIG_FILE_NAME
    if candidate.is_file() and not git_tracks(root, candidate):
        return ConfigLocation(candidate, "project-root")
    return ConfigLocation(user_config_path(), "user")


# --------------------------------------------------------------------------- showing


def describe(location: ConfigLocation) -> list[str]:
    """The settings in ``location`` as plain sentences. Tokens are never in them.

    With a config file, it is named first; values it sets are marked "(config file)", and
    the ones that cannot be changed here "(config file, locked)".
    """
    path = location.path
    lines = [f"Settings file: {path} ({_SOURCE_WORDS.get(location.source, location.source)})"]
    own = read_config_dict(path) if path.is_file() else {}
    try:
        company = find_company_settings(own)
    except ConfigError as exc:
        lines.append(f"Config file: problem: {exc}")
        company = None
    if company is None and not location.exists:
        lines.append("Nothing is set up yet. Run crit, or: crit config --config-file /path/to/crit.json")
        return lines
    locked: frozenset[str] = frozenset()
    data = own
    if company is not None:
        data, locked = merge_company(company.data, own)
        saved = f" (saved copy from {company.cached_at})" if company.cached_at else ""
        lines.append(f"Config file: {company.path}{saved}")

    def mark(key: str) -> str:
        if company is None:
            return ""
        top = key.split(".", 1)[0]
        if key in locked or top in locked:
            return " (config file, locked)"
        if top in company.data and top not in own:
            return " (config file)"
        return ""

    try:
        backend = backend_kind(data.get("backend"))
    except ConfigError:
        backend = str(data.get("backend"))
    url = str(data.get("url") or "")
    if backend == BACKEND_LOCAL:
        lines.append("Model runs in: a model server (backend local)" + mark("backend"))
        lines.append(f"Server address: {url or DEFAULT_LOCAL_URL}" + mark("url"))
        has_key = bool(os.environ.get(ENV_LOCAL_KEY, "").strip() or user_api_key() or _api_key_in(own))
        lines.append(f"API key: {'set' if has_key else 'not set'}")
    else:
        lines.append("Model runs in: a web page in the browser (backend browser)" + mark("backend"))
        lines.append(f"url: {url or 'not set (crit config --url https://ai.company.com/)'}" + mark("url"))
        selectors = data.get("selectors") if isinstance(data.get("selectors"), dict) else {}
        if not selectors.get("prompt_input"):
            lines.append("Page elements: not set (run crit setup to point at the message box)")
        elif selectors == STARTER_SELECTORS:
            lines.append("Page elements: starter guesses (run crit setup to point at the real ones)")
        else:
            lines.append("Page elements: set" + mark("selectors.prompt_input"))
    model = str(data.get("model") or "")
    lines.append(f"Model: {model or ('the first one the server lists' if backend == BACKEND_LOCAL else 'the default')}" + mark("model"))
    mode = str(data.get("egress") or egress.STRICT).lower()
    page = str(data.get("page_network") or egress.OPEN).lower()
    if mode == egress.OPEN:
        lines.append("Network: open (the page and builds reach what they need)" + mark("egress"))
    else:
        lines.append("Network: strict (crit contacts only the url's site and the allowed sites)" + mark("egress"))
        if page == egress.STRICT:
            lines.append("Page network: strict (the page reaches only the url's site and the allowed sites)" + mark("page_network"))
        else:
            lines.append("Page network: open (the page loads its own sites); crit's own traffic: strict" + mark("page_network"))
    hosts = data.get("allowed_hosts") if isinstance(data.get("allowed_hosts"), list) else []
    lines.append(f"Allowed sites: {', '.join(str(h) for h in hosts) if hosts else 'none'}" + mark("allowed_hosts"))
    terms = data.get("redact_terms") if isinstance(data.get("redact_terms"), list) else []
    if terms:
        from_file = len(company.data.get("redact_terms") or []) if company is not None else 0
        extra = f"; {from_file} from the config file, which cannot be removed" if from_file else ""
        lines.append(f"Names hidden: {len(terms)} ({', '.join(str(t) for t in terms)}{extra})")
    else:
        lines.append("Names hidden: none (add with crit config --redact \"Your Company\")")
    pii = data.get("redact_pii", True) is not False
    lines.append(f"Personal data hidden: {'on' if pii else 'off'}" + mark("redact_pii"))
    gitlab = data.get("gitlab") if isinstance(data.get("gitlab"), dict) else {}
    lines.append(f"GitLab: {gitlab.get('base_url') or 'not set'}" + mark("gitlab.base_url"))
    lines.append("GitLab token: " + ("set" if _secret_set("gitlab", "token", ENV_GITLAB_TOKENS) else
                                     "not set (crit config --gitlab-token; it decides the projects crit may use)"))
    jira = data.get("jira") if isinstance(data.get("jira"), dict) else {}
    lines.append(f"Jira: {jira.get('base_url') or os.environ.get(ENV_JIRA_URL, '') or 'not set'}" + mark("jira.base_url"))
    if jira.get("base_url") or os.environ.get(ENV_JIRA_URL):
        email = jira.get("email") or os.environ.get(ENV_JIRA_EMAIL, "")
        lines.append("Jira token: " + ("set" if _secret_set("jira", "token", ENV_JIRA_TOKENS) else "not set (crit config --jira-token)")
                     + (f", email {email} (Jira Cloud)" if email else ""))
    site_user = os.environ.get(ENV_SITE_USER, "").strip() or _user_setting("site_login", "username")
    if site_user or _secret_set("site_login", "password", ENV_SITE_PASSWORDS):
        host = _user_setting("site_login", "host")
        lines.append(f"Site sign-in: {site_user or 'no username (crit config --site-user)'}, password "
                     + ("set" if _secret_set("site_login", "password", ENV_SITE_PASSWORDS) else "not set (crit config --site-password)")
                     + (f", for {host}" if host else ", for the url's host"))
    return lines


def _user_setting(section: str, key: str) -> str:
    try:
        block = read_config_dict(user_config_path()).get(section)
    except (ConfigError, OSError):
        return ""
    return str(block.get(key) or "").strip() if isinstance(block, dict) else ""


def _secret_set(section: str, key: str, env: tuple[str, ...]) -> bool:
    if any(os.environ.get(name, "").strip() for name in env):
        return True
    try:
        block = read_config_dict(user_config_path()).get(section)
    except (ConfigError, OSError):
        return False
    return isinstance(block, dict) and bool(str(block.get(key) or "").strip())


# --------------------------------------------------------------------------- changing


#: ``--api-key`` given without a value: ask for it without echo.
_ASK_KEY = "\0ask"


def _read_api_key(value: str, prompt: str = "API key: ") -> str | None:
    """The key ``--api-key`` names: asked (getpass), read from stdin (``-``), or given. None: removed."""
    if value == _ASK_KEY:
        import getpass

        return getpass.getpass(prompt).strip()
    if value == "-":
        return sys.stdin.readline().strip()
    if value.strip().lower() == "none":
        return None
    return value.strip()


def set_api_key(value: str) -> int:
    """``crit config --api-key``: store (or remove) ``local.api_key`` in the user config. Never prints it."""
    return set_user_secret("local", "api_key", value, "API key")


def set_user_secret(section: str, key: str, value: str, label: str) -> int:
    """Store (or remove) ``<section>.<key>`` in the person's own config. Never prints it.

    Tokens live only there (``~/.crit/config.json``, private to the user), never in a
    project or a shared config file.
    """
    try:
        secret = _read_api_key(value, f"{label}: ")
    except (EOFError, KeyboardInterrupt):
        print("Not saved.", file=sys.stderr)
        return 1
    path = user_config_path()
    try:
        data = read_config_dict(path) if path.is_file() else {}
    except ConfigError as exc:
        print(f"Not saved: {exc}", file=sys.stderr)
        return 2
    block = data.get(section) if isinstance(data.get(section), dict) else {}
    if secret is None:
        block.pop(key, None)
        what = f"{label} removed"
    elif not secret:
        print(f"Not saved: the {label} is empty (none removes it).", file=sys.stderr)
        return 2
    else:
        block[key] = secret
        redaction.add_secret_value(secret)
        what = f"{label} set"
    if block:
        data[section] = block
    else:
        data.pop(section, None)
    try:
        write_config_file(path, data)
    except OSError as exc:
        print(f"Not saved: could not write {path}: {exc}", file=sys.stderr)
        return 1
    print(f"Saved {path}: {what}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crit config",
        description=(
            "See crit's settings (no flags), or change them. Each change is checked before it is "
            "saved. Without --config the file is the one crit uses here: CRIT_CONFIG, the "
            f"project's own, or your user settings ({user_config_path()})."
        ),
        epilog=(
            "examples:\n"
            "  crit config --config-file /path/to/crit.json\n"
            "  crit config --url https://ai.company.com/ --redact \"Acme, Globex\"\n"
            "  crit config --backend local --local-url http://127.0.0.1:8080 --model qwen2.5-coder-14b\n"
            "  crit config --selector prompt_input=textarea --selector send_button=\n"
            "  crit config --api-key            (asks for the model server's key)\n"
            "  crit config --check\n"
            "  crit config --allow-host static.company-cdn.net\n"
            "  crit config --gitlab-url https://gitlab.company.com"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="change this file instead of the one crit would use")
    parser.add_argument("--repo-dir", default=".", help=argparse.SUPPRESS)
    parser.add_argument("--path", action="store_true", help="print the settings file's path and stop")
    parser.add_argument("--config-file", metavar="PATH", help="use this config file (url, page elements, ...); none: stop using one")
    parser.add_argument("--url", help="the https address of the page crit uses in the browser")
    parser.add_argument("--backend", choices=(BACKEND_BROWSER, BACKEND_LOCAL), help="browser: a web page; local: a model server")
    parser.add_argument("--local-url", help=f"the model server's address, ip:port or http://host:port (default {DEFAULT_LOCAL_URL})")
    parser.add_argument("--model", help="the model's name (empty: the default)")
    parser.add_argument(
        "--api-key", nargs="?", const=_ASK_KEY, metavar="KEY",
        help="the model server's API key, kept in your user settings: no value asks for it (nothing is shown), "
        "- reads it from stdin, none removes it. CRIT_API_KEY overrides it",
    )
    parser.add_argument("--allow-host", action="append", default=[], metavar="HOST", help="a site the page may reach (repeatable)")
    parser.add_argument("--remove-host", action="append", default=[], metavar="HOST", help="stop allowing a site (repeatable)")
    parser.add_argument("--redact", action="append", default=[], metavar="NAME", help="names to hide, comma-separated or repeated")
    parser.add_argument("--unredact", action="append", default=[], metavar="NAME", help="stop hiding a name (repeatable)")
    parser.add_argument("--pii", choices=("on", "off"), help="hide personal data (emails, phone numbers, ...): on (default) or off")
    parser.add_argument("--egress", choices=egress.MODES, help="strict (default): only the url's site and allowed sites; open: no limit")
    parser.add_argument(
        "--page-network", choices=egress.MODES,
        help="open (default): the page loads its own sites, as for a person; strict: the page too reaches only the allowed sites",
    )
    parser.add_argument("--theme", metavar="ID", help="the terminal style (for you, in every project)")
    parser.add_argument("--gitlab-url", metavar="URL", help="your GitLab's https address (empty to remove)")
    parser.add_argument(
        "--gitlab-token", nargs="?", const=_ASK_KEY, metavar="TOKEN",
        help="your GitLab personal access token (scopes read_api and read_repository; add api and write_repository "
        "to post reviews and push), kept in your user settings. The projects it can reach are the ones crit may "
        "use. No value asks for it; - reads stdin; none removes it. CRIT_GITLAB_TOKEN overrides it",
    )
    parser.add_argument("--jira-url", metavar="URL", help="your Jira's https address (empty to remove)")
    parser.add_argument("--jira-email", metavar="EMAIL", help="Jira Cloud only: the account email for the API token (empty to remove)")
    parser.add_argument(
        "--jira-token", nargs="?", const=_ASK_KEY, metavar="TOKEN",
        help="your Jira personal access token (Server/Data Center) or API token (Cloud, with --jira-email), "
        "kept in your user settings; crit only reads issues. No value asks for it; none removes it",
    )
    parser.add_argument(
        "--site-user", metavar="NAME",
        help="your username for the chat site's own sign-in (a Windows/HTTP sign-in box; on Windows the "
        "signed-in account answers it), kept in your user settings; none removes it. CRIT_SITE_USER overrides it",
    )
    parser.add_argument(
        "--site-password", nargs="?", const=_ASK_KEY, metavar="PASSWORD",
        help="the password for --site-user, kept in your user settings and given only to the chat site's host. "
        "No value asks for it; - reads stdin; none removes it. CRIT_SITE_PASSWORD overrides it",
    )
    parser.add_argument(
        "--site-login-host", metavar="HOST",
        help="the host that asks for that sign-in, when it is not the chat url's own host; none removes it",
    )
    parser.add_argument("--selector", action="append", default=[], metavar="KEY=CSS", help=f"a page element ({', '.join(SELECTOR_KEYS)}); KEY= clears an optional one")
    parser.add_argument("--check", action="store_true", help="check that the model server (or the page's site) answers")
    parser.add_argument("--reset", action="store_true", help="remove the settings file (asks first; --yes without a terminal)")
    parser.add_argument("--yes", "-y", action="store_true", help="with --reset: do not ask")
    return parser


def _changes_requested(args: argparse.Namespace) -> bool:
    return any(
        value not in (None, [], False)
        for value in (
            args.url, args.backend, args.local_url, args.model, args.allow_host, args.remove_host,
            args.redact, args.unredact, args.pii, args.egress, args.gitlab_url, args.selector,
            args.config_file, args.page_network, args.theme, getattr(args, "api_key", None),
            getattr(args, "gitlab_token", None), getattr(args, "jira_url", None), getattr(args, "jira_email", None),
            getattr(args, "jira_token", None), getattr(args, "site_user", None), getattr(args, "site_password", None),
            getattr(args, "site_login_host", None),
        )
    )


def _split_names(values: list[str]) -> list[str]:
    return [item.strip() for value in values for item in value.split(",") if item.strip()]


def apply_changes(data: dict, args: argparse.Namespace) -> list[str]:
    """Apply the flags to ``data`` in place; what changed, as short phrases. ConfigError on a bad value."""
    done: list[str] = []
    hints: list[str] = []
    if args.config_file is not None:
        if args.config_file.strip().lower() in ("", "none"):
            data.pop(COMPANY_CONFIG_KEY, None)
            done.append("no config file is used")
        else:
            settings = check_company_file(args.config_file)
            data[COMPANY_CONFIG_KEY] = settings.path
            done.append(f"config file set to {settings.path}")
    if args.page_network:
        data["page_network"] = args.page_network
        done.append(
            "page network set to open (the page loads its own sites)"
            if args.page_network == egress.OPEN
            else "page network set to strict (the page reaches only the url's site and allowed sites)"
        )
    if args.backend:
        before = data.get("backend")
        data["backend"] = args.backend
        if args.backend == BACKEND_LOCAL and not args.local_url:
            current = str(data.get("url") or "")
            try:
                _local_url(current, _host_list(data.get("allowed_hosts"))) if current else None
            except ConfigError:
                current = ""
            data["url"] = current or DEFAULT_LOCAL_URL
        if before != args.backend:
            done.append("the model now runs " + ("in a model server" if args.backend == BACKEND_LOCAL else "in a web page in the browser"))
    if args.url is not None:
        if args.backend == BACKEND_LOCAL:
            raise ConfigError("--url is the page's address; for a model server use --local-url")
        url = clean_https_url(args.url, "url")
        data["url"] = url
        data["backend"] = BACKEND_BROWSER
        done.append(f"url set to {url} (the only site crit sends prompts to)")
    hosts = list(_host_list(data.get("allowed_hosts")))
    if args.allow_host:
        new = [host for host in _host_list(args.allow_host) if host not in hosts]
        hosts += new
        data["allowed_hosts"] = hosts
        if new:
            done.append(f"allowed {', '.join(new)}")
        else:
            done.append("those sites were already allowed")
    if args.remove_host:
        gone = [host for host in _host_list(args.remove_host) if host in hosts]
        hosts = [host for host in hosts if host not in gone]
        data["allowed_hosts"] = hosts
        done.append(f"no longer allowed: {', '.join(gone)}" if gone else "those sites were not on the list")
    if args.local_url is not None:
        from critique_bot.setup_wizard import parse_server

        url = parse_server(args.local_url)
        if url is None:
            raise ConfigError(f"--local-url takes ip:port or http://host:port, not {args.local_url!r}")
        try:
            _local_url(url, ())
        except ConfigError:
            host = (urlsplit(url).hostname or "").lower()
            if host not in hosts:
                hosts.append(host)
                data["allowed_hosts"] = hosts
                done.append(f"crit will send prompts to the model server at {host}")
        url = _local_url(url, tuple(hosts))
        data["url"] = url
        data["backend"] = BACKEND_LOCAL
        done.append(f"model server set to {url}")
    if args.model is not None:
        model = args.model.strip()
        if PLACEHOLDER_MODEL in model:
            raise ConfigError("--model is still the placeholder; give the model's real name")
        data["model"] = model
        done.append(f"model set to {model}" if model else "model set to the default")
    terms = list(data.get("redact_terms") or []) if isinstance(data.get("redact_terms"), list) else []
    if args.redact:
        try:
            cleaned = list(redaction.clean_terms(terms + _split_names(args.redact)))
        except ValueError as exc:
            raise ConfigError(f"--redact: {exc}") from exc
        known = {str(term).casefold() for term in terms}
        added = [term for term in cleaned if term.casefold() not in known]
        data["redact_terms"] = cleaned
        terms = cleaned
        done.append(f"now hiding {', '.join(added)}" if added else "those names were already hidden")
    if args.unredact:
        drop = {name.casefold() for name in _split_names(args.unredact)}
        kept = [term for term in terms if str(term).casefold() not in drop]
        removed = [term for term in terms if str(term).casefold() in drop]
        data["redact_terms"] = kept
        done.append(f"no longer hiding {', '.join(removed)}" if removed else "those names were not hidden")
    if args.pii:
        data["redact_pii"] = args.pii == "on"
        done.append("personal data is hidden" if args.pii == "on" else "personal data is NO LONGER hidden from what crit sends")
    if args.egress:
        data["egress"] = args.egress
        done.append(
            "network set to strict (only the url's site and allowed sites)"
            if args.egress == egress.STRICT
            else "network set to open (no limit on the sites the page and builds reach)"
        )
    if args.gitlab_url is not None:
        gitlab = data.get("gitlab") if isinstance(data.get("gitlab"), dict) else {}
        if args.gitlab_url.strip():
            gitlab["base_url"] = clean_gitlab_url(args.gitlab_url)
            done.append(f"GitLab set to {gitlab['base_url']}")
        else:
            gitlab.pop("base_url", None)
            done.append("GitLab address removed")
        if gitlab:
            data["gitlab"] = gitlab
        else:
            data.pop("gitlab", None)
    for flag_value, field_name, label in ((args.jira_url, "base_url", "Jira"), (args.jira_email, "email", "Jira email")):
        if flag_value is None:
            continue
        jira = data.get("jira") if isinstance(data.get("jira"), dict) else {}
        if flag_value.strip():
            jira[field_name] = clean_gitlab_url(flag_value) if field_name == "base_url" else flag_value.strip()
            done.append(f"{label} set to {jira[field_name]}")
        else:
            jira.pop(field_name, None)
            done.append(f"{label} removed")
        if jira:
            data["jira"] = jira
        else:
            data.pop("jira", None)
    if args.selector:
        selectors = data.get("selectors") if isinstance(data.get("selectors"), dict) else {}
        for item in args.selector:
            key, sep, css = item.partition("=")
            key = key.strip()
            if not sep or key not in SELECTOR_KEYS:
                raise ConfigError(f"--selector takes KEY=CSS with KEY one of {', '.join(SELECTOR_KEYS)}; got {item!r}")
            css = css.strip()
            if not css and key in ("prompt_input", "assistant_messages"):
                raise ConfigError(f"--selector {key} cannot be empty")
            selectors[key] = css
        data["selectors"] = selectors
        done.append(f"{len(args.selector)} page element(s) set")
    if backend_kind(data.get("backend")) == BACKEND_BROWSER and data.get("url") and not data.get(COMPANY_CONFIG_KEY):
        selectors = data.get("selectors") if isinstance(data.get("selectors"), dict) else {}
        if not selectors.get("prompt_input"):
            data["selectors"] = {**STARTER_SELECTORS, **{k: v for k, v in selectors.items() if v}}
        if data.get("selectors") == STARTER_SELECTORS:
            hints.append("Next: run crit setup to point crit at the page's message box and replies.")
    return done + hints


def locked_change(data: dict, args: argparse.Namespace) -> str | None:
    """The refusal when a flag would change what the config file sets (None when nothing is locked)."""
    if args.config_file is not None:
        return None  # choosing (or dropping) the file itself is always the person's
    try:
        company = find_company_settings(data)
    except ConfigError:
        return None
    if company is None:
        return None
    _merged, locked = merge_company(company.data, data)
    cfg = company.data
    flags: list[tuple[str, bool]] = [
        ("--url", args.url is not None and "url" in locked),
        ("--local-url", args.local_url is not None and "url" in locked),
        ("--backend", bool(args.backend) and "backend" in locked),
        ("--allow-host", bool(args.allow_host) and "allowed_hosts" in locked),
        ("--remove-host", bool(args.remove_host) and "allowed_hosts" in locked),
        ("--pii", bool(args.pii) and "redact_pii" in locked),
        ("--egress", args.egress == egress.OPEN and str(cfg.get("egress") or "").lower() == egress.STRICT),
        ("--page-network", args.page_network == egress.OPEN and str(cfg.get("page_network") or "").lower() == egress.STRICT),
        ("--gitlab-url", args.gitlab_url is not None and "gitlab.base_url" in locked),
        ("--jira-url", args.jira_url is not None and "jira.base_url" in locked),
        ("--selector", any(f"selectors.{item.partition('=')[0].strip()}" in locked for item in args.selector)),
    ]
    company_terms = {str(t).casefold() for t in (cfg.get("redact_terms") or [])}
    flags.append(("--unredact", any(name.casefold() in company_terms for name in _split_names(args.unredact))))
    for flag, hit in flags:
        if hit:
            return f"{flag}: " + LOCKED.format(path=company.path)
    return None


@contextlib.contextmanager
def _quiet_globals() -> Iterator[None]:
    """load_config sets the process-wide egress and redaction settings; a check must not."""
    policy = egress.policy()
    terms = redaction.custom_terms()
    pii = redaction.pii_redaction_enabled()
    try:
        yield
    finally:
        egress._policy = policy  # noqa: SLF001 - restore exactly what was there
        redaction.set_custom_terms(terms)
        redaction.set_pii_redaction(pii)


def load_problem(data: dict) -> str | None:
    """Why ``data`` would not load as a config (None when it would)."""
    with _quiet_globals(), tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / CONFIG_FILE_NAME
        path.write_text(json.dumps(data), encoding="utf-8")
        try:
            load_config(path)
        except ConfigError as exc:
            return str(exc)
    return None


def check(location: ConfigLocation, *, probe_server: Callable | None = None, probe_site: Callable | None = None) -> int:
    """``crit config --check``: whether the model server (or the page's site) answers. 0 when it does."""
    from critique_bot import model_api as local_model

    if not location.exists:
        print("Nothing to check: crit is not set up yet. Run crit, or see crit config --help.", file=sys.stderr)
        return 1
    try:
        with _quiet_globals():
            config = load_config(location.path)
    except ConfigError as exc:
        print(f"Problem: {exc}", file=sys.stderr)
        return 1
    if not config.url:
        print("Nothing to check: no url is set (crit config --url https://ai.company.com/).", file=sys.stderr)
        return 1
    probe = (probe_server or local_model.probe_server) if config.is_local else (probe_site or local_model.probe_site)
    print(f"Checking {config.url} ...")
    key = config.local.api_key if config.is_local else ""
    result = probe(config.url, key) if key else probe(config.url)
    print(result.line)
    return 0 if result.reachable else 1


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def _tty() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def main(argv: list[str], *, confirm: Callable[[str], bool] = _confirm, is_tty: Callable[[], bool] = _tty) -> int:
    """``crit config [flags]``. Prints plain sentences; returns the exit code."""
    args = build_parser().parse_args(argv)
    try:
        location = resolve_config_path(args.config, args.repo_dir)
    except ConfigError as exc:
        print(f"Problem: {exc}", file=sys.stderr)
        return 1
    path = location.path
    if args.path:
        print(path)
        return 0
    if args.check:
        return check(location)
    if args.reset:
        if _changes_requested(args):
            print("Problem: --reset cannot be combined with other changes; run it on its own first.", file=sys.stderr)
            return 2
        if not location.exists:
            print(f"Nothing to reset: {path} does not exist.")
            return 0
        if not args.yes:
            if not is_tty():
                print("Problem: --reset removes all settings; add --yes to confirm when there is no terminal.", file=sys.stderr)
                return 2
            if not confirm(f"Remove all settings in {path}? crit asks the setup questions again on the next run."):
                print("Kept the settings.")
                return 0
        path.unlink()
        print(f"Removed {path}. crit asks the setup questions on the next run (or use crit config --config-file /path/to/crit.json).")
        return 0
    if not _changes_requested(args):
        try:
            for line in describe(location):
                print(line)
        except ConfigError as exc:
            print(f"Problem: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.api_key is not None:
        code = set_api_key(args.api_key)
        args.api_key = None
        if code or not _changes_requested(args):
            return code
    for attr, section, key, label in (
        ("gitlab_token", "gitlab", "token", "GitLab token"), ("jira_token", "jira", "token", "Jira token"),
        ("site_user", "site_login", "username", "site username"), ("site_password", "site_login", "password", "site password"),
        ("site_login_host", "site_login", "host", "site sign-in host"),
    ):
        if getattr(args, attr, None) is not None:
            code = set_user_secret(section, key, getattr(args, attr), label)
            setattr(args, attr, None)
            if code or not _changes_requested(args):
                return code
    if args.theme is not None:
        from critique_bot.welcome import THEMES

        ids = [theme.id for theme in THEMES]
        if args.theme not in ids:
            print(f"Not saved: --theme takes one of {', '.join(ids)}", file=sys.stderr)
            return 2
        set_user_preferences(theme=args.theme)
        print(f"Saved {user_config_path()}: style set to {args.theme} (in every project that did not pick its own).")
        args.theme = None
        if not _changes_requested(args):
            return 0
    try:
        data = read_config_dict(path)
        refusal = locked_change(data, args)
        if refusal:
            print(refusal, file=sys.stderr)
            return 2
        before = load_problem(data) if location.exists else "not set up"
        done = apply_changes(data, args)
    except ConfigError as exc:
        print(f"Not saved: {exc}", file=sys.stderr)
        return 2
    after = load_problem(data)
    if after is not None and before is None:
        print(f"Not saved: {after}", file=sys.stderr)
        return 2
    try:
        write_config_file(path, data)
    except OSError as exc:
        print(f"Not saved: could not write {path}: {exc}", file=sys.stderr)
        return 1
    changes = [item for item in done if not item.startswith("Next:")]
    hints = [item for item in done if item.startswith("Next:")]
    print(f"Saved {path}: {'; '.join(changes)}.")
    if args.config_file is not None and data.get(COMPANY_CONFIG_KEY):
        try:
            with _quiet_globals():
                found = find_company_settings(data)
            if found is not None:
                print(company_summary(found))
        except ConfigError:
            pass
    if after is not None:
        print(f"Not ready yet: {after}.")
    for hint in hints:
        print(hint)
    return 0
