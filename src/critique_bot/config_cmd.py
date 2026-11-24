"""``crit config``: see and change crit's settings without editing JSON.

``crit config`` with no flags prints the settings in plain words. With flags it changes
them, for scripts, CI, and IT rollout as much as for people:

    crit config --preset chatgpt --redact "Acme, Globex"
    crit config --url https://chat.company.com/ --allow-host static.company-cdn.net
    crit config --backend local --local-url http://127.0.0.1:8080 --model qwen2.5-coder-14b

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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urlsplit

from critique_bot import egress, redaction
from critique_bot.config import (
    BACKEND_BROWSER,
    BACKEND_LOCAL,
    CHATGPT_PRESET,
    DEFAULT_LOCAL_URL,
    ENV_CONFIG,
    PLACEHOLDER_MODEL,
    SELECTOR_KEYS,
    STARTER_SELECTORS,
    ConfigError,
    _host_list,
    _local_url,
    backend_kind,
    clean_gitlab_url,
    clean_https_url,
    load_config,
    read_config_dict,
    user_config_path,
    write_config_file,
)

CONFIG_FILE_NAME = "config.json"

#: Told when a command needs settings and cannot ask (no terminal).
NOT_SET_UP = (
    "crit is not set up yet. Run crit in a terminal to answer a few questions, or set it up "
    'in one line: crit config --preset chatgpt (or crit config --url https://chat.company.com/), '
    'adding --redact "Your Company" for names to hide. See the settings with: crit config'
)


# --------------------------------------------------------------------------- which file


@dataclass(frozen=True)
class ConfigLocation:
    """The config file a command uses, and why that one."""

    path: Path
    #: "flag" (--config), "env" (CRITIQUE_CONFIG), "project" (.bot/settings.json),
    #: "project-root" (config.json next to the code), or "user" (config.user_config_path()).
    source: str

    @property
    def exists(self) -> bool:
        return self.path.is_file()


_SOURCE_WORDS = {
    "flag": "named with --config",
    "env": f"named by {ENV_CONFIG}",
    "project": "stored for this project in .bot/settings.json",
    "project-root": "config.json in this project",
    "user": "your user settings",
}


def resolve_config_path(explicit: str | Path | None = None, repo_dir: str | Path | None = None) -> ConfigLocation:
    """Which config file a command uses.

    ``--config`` > ``CRITIQUE_CONFIG`` > the config stored for the project in
    ``.bot/settings.json`` > an untracked ``config.json`` in the project root > the user
    config (``config.user_config_path()``). The first two count even when the file is
    missing (the caller reports it or writes it); the project's only when the file exists;
    the user config may not exist yet (first run).
    """
    if explicit:
        return ConfigLocation(Path(explicit).expanduser(), "flag")
    from_env = os.environ.get(ENV_CONFIG, "").strip()
    if from_env:
        return ConfigLocation(Path(from_env).expanduser(), "env")
    from critique_bot.bot_home import BotHomeError, find_bot_home, git_tracks

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
    """The settings in ``location`` as plain sentences. Tokens are never in them."""
    path = location.path
    lines = [f"Settings file: {path} ({_SOURCE_WORDS.get(location.source, location.source)})"]
    if not location.exists:
        lines.append("Nothing is set up yet. Run crit to answer a few questions, or: crit config --preset chatgpt")
        return lines
    data = read_config_dict(path)
    try:
        backend = backend_kind(data.get("backend"))
    except ConfigError:
        backend = str(data.get("backend"))
    url = str(data.get("url") or "")
    if backend == BACKEND_LOCAL:
        lines.append("Model runs in: a model server (backend local)")
        lines.append(f"Server address: {url or DEFAULT_LOCAL_URL}")
    else:
        lines.append("Model runs in: a chat page in the browser (backend browser)")
        lines.append(f"Chat address: {url or 'not set (crit config --url https://chat.company.com/)'}")
        selectors = data.get("selectors") if isinstance(data.get("selectors"), dict) else {}
        if not selectors.get("prompt_input"):
            lines.append("Page elements: not set (run crit setup to point at the message box)")
        elif selectors == STARTER_SELECTORS:
            lines.append("Page elements: starter guesses (run crit setup to point at the real ones)")
        else:
            lines.append("Page elements: set")
    model = str(data.get("model") or "")
    lines.append(f"Model: {model or ('the first one the server lists' if backend == BACKEND_LOCAL else 'the chat default')}")
    mode = str(data.get("egress") or egress.STRICT).lower()
    if mode == egress.OPEN:
        lines.append("Network: open (the chat page and builds reach what they need)")
    else:
        lines.append("Network: strict (crit contacts only the chat site and the allowed sites)")
    hosts = data.get("allowed_hosts") if isinstance(data.get("allowed_hosts"), list) else []
    lines.append(f"Allowed sites: {', '.join(str(h) for h in hosts) if hosts else 'none'}")
    terms = data.get("redact_terms") if isinstance(data.get("redact_terms"), list) else []
    if terms:
        lines.append(f"Names hidden: {len(terms)} ({', '.join(str(t) for t in terms)})")
    else:
        lines.append("Names hidden: none (add with crit config --redact \"Your Company\")")
    pii = data.get("redact_pii", True) is not False
    lines.append(f"Personal data hidden: {'on' if pii else 'off'}")
    gitlab = data.get("gitlab") if isinstance(data.get("gitlab"), dict) else {}
    lines.append(f"GitLab: {gitlab.get('base_url') or 'not set'}")
    return lines


# --------------------------------------------------------------------------- changing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crit config",
        description=(
            "See crit's settings (no flags), or change them. Each change is checked before it is "
            "saved. Without --config the file is the one crit uses here: CRITIQUE_CONFIG, the "
            f"project's own, or your user settings ({user_config_path()})."
        ),
        epilog=(
            "examples:\n"
            "  crit config --preset chatgpt --redact \"Acme, Globex\"\n"
            "  crit config --url https://chat.company.com/\n"
            "  crit config --backend local --local-url http://127.0.0.1:8080 --model qwen2.5-coder-14b\n"
            "  crit config --allow-host static.company-cdn.net\n"
            "  crit config --gitlab-url https://gitlab.company.com"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="change this file instead of the one crit would use")
    parser.add_argument("--repo-dir", default=".", help=argparse.SUPPRESS)
    parser.add_argument("--path", action="store_true", help="print the settings file's path and stop")
    parser.add_argument("--preset", choices=("chatgpt", "custom"), help="chatgpt: ChatGPT's address, page elements, and sites; custom: a company chat (with --url)")
    parser.add_argument("--url", help="the chat's https address (a cloud chat in the browser)")
    parser.add_argument("--backend", choices=(BACKEND_BROWSER, BACKEND_LOCAL), help="browser: a chat page; local: a model server")
    parser.add_argument("--local-url", help=f"the model server's address (default {DEFAULT_LOCAL_URL})")
    parser.add_argument("--model", help="the model's name (empty: the default)")
    parser.add_argument("--allow-host", action="append", default=[], metavar="HOST", help="a site the chat page may reach (repeatable)")
    parser.add_argument("--remove-host", action="append", default=[], metavar="HOST", help="stop allowing a site (repeatable)")
    parser.add_argument("--redact", action="append", default=[], metavar="NAME", help="names to hide, comma-separated or repeated")
    parser.add_argument("--unredact", action="append", default=[], metavar="NAME", help="stop hiding a name (repeatable)")
    parser.add_argument("--pii", choices=("on", "off"), help="hide personal data (emails, phone numbers, ...): on (default) or off")
    parser.add_argument("--egress", choices=egress.MODES, help="strict (default): only the chat and allowed sites; open: no limit")
    parser.add_argument("--gitlab-url", metavar="URL", help="your GitLab's https address (empty to remove)")
    parser.add_argument("--selector", action="append", default=[], metavar="KEY=CSS", help=f"a page element ({', '.join(SELECTOR_KEYS)})")
    parser.add_argument("--reset", action="store_true", help="remove the settings file (asks first; --yes without a terminal)")
    parser.add_argument("--yes", "-y", action="store_true", help="with --reset: do not ask")
    return parser


def _changes_requested(args: argparse.Namespace) -> bool:
    return any(
        value not in (None, [], False)
        for value in (
            args.preset, args.url, args.backend, args.local_url, args.model, args.allow_host, args.remove_host,
            args.redact, args.unredact, args.pii, args.egress, args.gitlab_url, args.selector,
        )
    )


def _split_names(values: list[str]) -> list[str]:
    return [item.strip() for value in values for item in value.split(",") if item.strip()]


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def apply_changes(data: dict, args: argparse.Namespace) -> list[str]:
    """Apply the flags to ``data`` in place; what changed, as short phrases. ConfigError on a bad value."""
    done: list[str] = []
    hints: list[str] = []
    if args.preset == "chatgpt":
        preset = json.loads(json.dumps(CHATGPT_PRESET))
        data["backend"] = BACKEND_BROWSER
        data["url"] = preset["url"]
        data["selectors"] = preset["selectors"]
        data.setdefault("egress", preset["egress"])
        for key in ("timeout_ms", "idle_ms"):
            data.setdefault(key, preset[key])
        existing = data.get("allowed_hosts") if isinstance(data.get("allowed_hosts"), list) else []
        data["allowed_hosts"] = list(_host_list(list(existing) + preset["allowed_hosts"]))
        done.append(f"ChatGPT set up (chat address {preset['url']}, its page elements, and the {len(preset['allowed_hosts'])} sites its page needs)")
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
            done.append("the model now runs " + ("in a model server" if args.backend == BACKEND_LOCAL else "in a chat page in the browser"))
    if args.url is not None:
        if args.backend == BACKEND_LOCAL:
            raise ConfigError("--url is the chat page's address; for a model server use --local-url")
        url = clean_https_url(args.url, "chat address")
        data["url"] = url
        data["backend"] = BACKEND_BROWSER
        done.append(f"chat address set to {url} (the only site crit sends prompts to)")
        if args.preset == "chatgpt" and _host(url) != _host(CHATGPT_PRESET["url"]):
            raise ConfigError("--preset chatgpt sets the address itself; drop --url, or use --preset custom")
    if args.preset == "custom":
        url = str(data.get("url") or "")
        if not url or backend_kind(data.get("backend")) != BACKEND_BROWSER:
            raise ConfigError("--preset custom needs the chat's address: add --url https://chat.company.com/")
        if not isinstance(data.get("selectors"), dict) or data.get("selectors") == CHATGPT_PRESET["selectors"]:
            data["selectors"] = dict(STARTER_SELECTORS)
        data.setdefault("egress", egress.STRICT)
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
        url = _local_url(args.local_url, tuple(hosts))
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
        done.append("personal data is hidden" if args.pii == "on" else "personal data is NO LONGER hidden from the chat")
    if args.egress:
        data["egress"] = args.egress
        done.append(
            "network set to strict (only the chat and allowed sites)"
            if args.egress == egress.STRICT
            else "network set to open (no limit on the sites the chat page and builds reach)"
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
    if backend_kind(data.get("backend")) == BACKEND_BROWSER and data.get("url"):
        selectors = data.get("selectors") if isinstance(data.get("selectors"), dict) else {}
        if not selectors.get("prompt_input"):
            if _host(str(data["url"])) == _host(CHATGPT_PRESET["url"]):
                data["selectors"] = {**CHATGPT_PRESET["selectors"], **{k: v for k, v in selectors.items() if v}}
                data["allowed_hosts"] = list(_host_list(list(data.get("allowed_hosts") or []) + CHATGPT_PRESET["allowed_hosts"]))
            else:
                data["selectors"] = {**STARTER_SELECTORS, **{k: v for k, v in selectors.items() if v}}
        if data.get("selectors") == STARTER_SELECTORS:
            hints.append("Next: run crit setup to point crit at the chat's message box and replies.")
    return done + hints


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
        print(f"Removed {path}. crit asks the setup questions on the next run (or use crit config --preset ...).")
        return 0
    if not _changes_requested(args):
        try:
            for line in describe(location):
                print(line)
        except ConfigError as exc:
            print(f"Problem: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        data = read_config_dict(path)
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
    if after is not None:
        print(f"Not ready yet: {after}.")
    for hint in hints:
        print(hint)
    return 0
