"""Secret handling shared by every layer: what is redacted before text leaves this machine,
and which files usually hold credentials.

This sits in the core layer so the model backends (redacting each outgoing message),
the tools (asking before a secret file is read), and git workspace code can all use it
without depending on each other.
"""

from __future__ import annotations

import re
from pathlib import Path

from critique_bot import log

__all__ = ["REDACTED", "redact_outgoing", "redact_secrets", "sensitive_file"]


#: What a redacted secret looks like in a message to the chat.
REDACTED = "[REDACTED:"
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")),
    ("aws key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("gitlab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("api key", re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{32,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("storage key", re.compile(r"(?i)(?<=AccountKey=)[A-Za-z0-9+/=]{40,}")),
    ("url password", re.compile(r"(?<=://)[^/\s:@'\"<>]+:[^/\s@'\"<>]{3,}(?=@)")),
)
_SECRET_ASSIGN_RE = re.compile(
    r"(?im)\b([\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|"
    r"client[_-]?secret|auth[_-]?key|storepass|keypass)[\w.-]*)(\s*[:=]\s*)([\"']?)([^\s\"'<>{}()$;,`]{6,})\3(?=$|[\s\"',;])"
)
_SECRET_XML_RE = re.compile(
    r"(?i)(name=\"[^\"]*(?:password|secret|token|api[_-]?key|apikey|client[_-]?secret)[^\"]*\"[^>]*>)([^<\s]{8,})(<)"
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*$")


def redact_secrets(text: str) -> tuple[str, int]:
    """``text`` with credentials replaced by ``[REDACTED:kind]``, and how many were replaced.

    Applied to every message before it goes to the chat. A value that reads
    like code (``BuildConfig.API_KEY``, ``getToken``) is left alone; a quoted
    literal or a properties value with digits or symbols is not.
    """
    if not text:
        return text, 0
    count = 0
    for kind, pattern in _SECRET_PATTERNS:
        text, hits = pattern.subn(f"{REDACTED}{kind}]", text)
        count += hits

    def assignment(match: re.Match[str]) -> str:
        nonlocal count
        value = match.group(4)
        if value.startswith(REDACTED) or (not match.group(3) and _IDENTIFIER_RE.match(value)):
            return match.group(0)
        if value.lower() in {"true", "false", "null", "none", "required", "optional", "password", "changeit!"}:
            return match.group(0)
        count += 1
        return f"{match.group(1)}{match.group(2)}{match.group(3)}{REDACTED}{match.group(1).lower()}]{match.group(3)}"

    text = _SECRET_ASSIGN_RE.sub(assignment, text)

    def xml_value(match: re.Match[str]) -> str:
        nonlocal count
        if match.group(2).startswith(("@string/", "@", REDACTED)):
            return match.group(0)
        count += 1
        return f"{match.group(1)}{REDACTED}secret]{match.group(3)}"

    text = _SECRET_XML_RE.sub(xml_value, text)
    return text, count


_SENSITIVE_NAMES = re.compile(
    r"(?i)^(?:\.env(?:\..+)?|\.envrc|.+\.(?:pem|key|p8|p12|pfx|jks|keystore|bks|kdbx|ovpn|asc|gpg)|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|"
    r"\.netrc|_netrc|\.npmrc|\.yarnrc(?:\.yml)?|\.pypirc|\.pgpass|\.git-credentials|\.htpasswd|credentials(?:\.json|\.xml)?|"
    r"(?:keystore|signing|secrets?|release-signing|local|gradle)\.properties|secrets?\.(?:json|ya?ml|toml|xml)|service-account.*\.json|"
    r"google-services\.json|googleservice-info\.plist|\.(?:bash|zsh|python|psql|mysql|node_repl)_history|consolehost_history\.txt|"
    r"cookies|login data|web data|storage_state\.json)$"
)
# Folders whose files are credentials or a signed-in browser.
_SENSITIVE_DIRS = frozenset(
    {".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker", "gcloud", ".edge-profile", "msedge-user-data", ".bot-browser"}
)


def sensitive_file(path: Path | str) -> bool:
    """True for a file that usually holds credentials: .env, keys and keystores, .netrc, signing properties.

    ``gradle.properties`` and ``local.properties`` count: in Android projects they hold
    signing passwords and repository tokens.
    """
    target = Path(path)
    if any(part.lower() in _SENSITIVE_DIRS for part in target.parts):
        return True
    return bool(_SENSITIVE_NAMES.match(target.name))


def redact_outgoing(prompt: str) -> str:
    """``prompt`` with known secret formats replaced by ``[REDACTED:kind]``; every backend sends through this."""
    text, hidden = redact_secrets(prompt)
    if hidden:
        log.info(f"redacted {hidden} secret(s) before sending")
    return text
