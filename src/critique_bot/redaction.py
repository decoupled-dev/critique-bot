# SPDX-License-Identifier: Apache-2.0
"""Secret handling shared by every layer: what is redacted before text leaves this machine,
and which files usually hold credentials.

This sits in the core layer so the model backends (redacting each outgoing message),
the tools (asking before a secret file is read), and git workspace code can all use it
without depending on each other.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from critique_bot import log

__all__ = [
    "NAME_MARKER",
    "REDACTED",
    "clean_terms",
    "custom_terms",
    "pii_redaction_enabled",
    "redact_outgoing",
    "redact_pii",
    "redact_secrets",
    "redact_terms",
    "sensitive_file",
    "set_custom_terms",
    "set_pii_redaction",
]


#: What a redacted secret looks like in a message to the chat.
REDACTED = "[REDACTED:"
# A pattern with a ``keep`` group keeps that part (the label) and redacts the rest of the match.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"),
    ),
    # A key cut off by a size cap has no END line: hide everything after BEGIN. The lookahead wants
    # key material next (after optional ``Proc-Type:`` style headers), so code that merely names
    # the header in a string is left alone.
    (
        "private key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
            r"(?=(?:\\[rn]|\s)+(?:[+\- ]?[ \t]*[\w-]+:[^\n\\]*(?:\\[rn]|\s)+)*[+\- ]?[ \t]*[A-Za-z0-9+/]{16,})"
            r"[\s\S]*\Z"
        ),
    ),
    ("aws key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    (
        "gitlab token",
        re.compile(r"\bgl(?:pat|rt|dt|cbt|ptt|ft|imt|oas|soat|ffct|agent)-[A-Za-z0-9_-]{20,}\b"),
    ),
    ("slack token", re.compile(r"\bxox[abeprs]-[A-Za-z0-9-]{10,}\b")),
    ("slack webhook", re.compile(r"(?P<keep>hooks\.slack\.com/(?:services|workflows|triggers)/)[A-Za-z0-9/_-]{8,}")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("google oauth secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}")),
    ("google oauth token", re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}")),
    ("api key", re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{32,}\b")),
    ("stripe key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}\b")),
    ("sendgrid key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")),
    ("huggingface token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("pypi token", re.compile(r"\bpypi-[A-Za-z0-9_-]{50,}")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("npm token", re.compile(r"(?P<keep>:_(?:authToken|auth|password)\s*=\s*)(?![\"']?\$\{)[^\s\"']{6,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    (
        "authorization",
        re.compile(r"(?i)(?P<keep>\bauthorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic|token|digest)\s+)[A-Za-z0-9._~+/=-]{8,}"),
    ),
    ("bearer token", re.compile(r"(?P<keep>\bBearer\s+)[A-Za-z0-9._~+/-]{20,}=*")),
    (
        "storage key",
        re.compile(r"(?i)(?P<keep>\b(?:AccountKey|SharedAccessKey|SharedSecret)\s*=\s*)[^;\s\"'<>]{8,}"),
    ),
    ("sas signature", re.compile(r"(?i)(?P<keep>(?:[?&;]|\b)sig=)[A-Za-z0-9%+/=]{16,}")),
    ("jdbc password", re.compile(r"(?i)(?P<keep>\bjdbc:[^\s\"']*?[?&;:](?:password|pwd)=)(?!\$\{)[^&;\s\"']+")),
    ("url password", re.compile(r"(?<=://)[^/\s:@'\"<>]*:[^/\s@'\"<>]{3,}(?=@)")),
    (
        "kubeconfig key",
        re.compile(r"(?im)(?P<keep>^[+\- ]?\s*[\"']?(?:client-key-data|client-certificate-data)[\"']?\s*:\s*[\"']?)[A-Za-z0-9+/=]{16,}"),
    ),
    # Bodies of keys and certificates pasted without their armor: 2+ lines of mixed-case base64.
    (
        "key material",
        re.compile(
            r"(?m)^(?:[+\- ]?[ \t]*(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[0-9])"
            r"[A-Za-z0-9+/]{40,}={0,2}[ \t]*(?:\n|\Z)){2,}"
        ),
    ),
)
_SECRET_ASSIGN_RE = re.compile(
    r"(?im)\b(?P<key>[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|"
    r"client[_-]?secret|auth[_-]?key|storepass|keypass)[\w.-]*)(?P<kq>[\"']?)(?P<sep>[ \t]*(?::=|=>|[:=])[ \t]*)"
    r"(?P<q>[\"']?)(?P<val>[^\s\"'<>{}()\[\]$;,`]{6,})(?P=q)(?=$|[\s\"',;)}\]])"
)
_SECRET_XML_RE = re.compile(
    r"(?i)(name=\"[^\"]*(?:password|secret|token|api[_-]?key|apikey|client[_-]?secret)[^\"]*\"[^>]*>)([^<\s]{8,})(<)"
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*$")
# What precedes a key in a .env / .properties line (diff marker, ``export``) or a YAML line (any indent).
_ENV_LINE_PREFIX_RE = re.compile(r"[+\- ]?(?:export[ \t]+|set[ \t]+)?[\"']?")
_YAML_LINE_PREFIX_RE = re.compile(r"[+\- ]?[ \t]*(?:-[ \t]+)?[\"']?")
# What may follow the value on such a line: nothing, a ``;`` or a comment.
_LINE_REST_RE = re.compile(r"[ \t]*;?[ \t]*(?:#.*)?(?:\r?\n|\Z)")
_PLACEHOLDER_VALUES = frozenset({"true", "false", "null", "none", "required", "optional", "password", "changeit!"})


def _value_is_secret(text: str, match: re.Match[str]) -> bool:
    """Whether an assigned value is a literal credential rather than a reference to one in code."""
    value = match.group("val")
    if value.startswith(REDACTED) or value.lower() in _PLACEHOLDER_VALUES:
        return False
    if match.group("q"):
        return True  # a quoted literal
    if not _IDENTIFIER_RE.match(value):
        return True  # symbols a name cannot hold: ``abc-def/ghi``, ``Hunter2!``
    if "." in value:
        return False  # ``BuildConfig.API_KEY``, ``user.password``
    has_digit = any(ch.isdigit() for ch in value)
    if has_digit and "_" not in value:
        return True  # ``Xk9mP2qL7vR4``, ``android123``
    if not _LINE_REST_RE.match(text, match.end()):
        return False  # more code follows: ``f(token=resolved_token, ...)``
    line_start = text.rfind("\n", 0, match.start()) + 1
    sep = match.group("sep")
    if sep == "=":  # .env / .properties: ``KEY=value`` at the start of a line
        config_line = bool(_ENV_LINE_PREFIX_RE.fullmatch(text, line_start, match.start()))
    elif sep.strip() == ":":  # YAML: ``key: value`` at any indent
        config_line = bool(_YAML_LINE_PREFIX_RE.fullmatch(text, line_start, match.start()))
    else:
        config_line = False
    return config_line and (len(value) >= 8 or has_digit)


#: Exact secret values crit knows at runtime (the local server's API key): always hidden.
_secret_values: set[str] = set()


def add_secret_value(value: str) -> None:
    """Hide ``value`` wherever it appears from now on (messages, transcripts, the log file)."""
    value = (value or "").strip()
    if len(value) >= 4:
        _secret_values.add(value)


def redact_secrets(text: str) -> tuple[str, int]:
    """``text`` with credentials replaced by ``[REDACTED:kind]``, and how many were replaced.

    Applied to every message before it goes to the chat. A value that reads
    like code (``BuildConfig.API_KEY``, ``getToken()``, ``user.password``) is
    left alone; a quoted literal, a value with digits or symbols, or a value in a
    .env / properties / YAML line is not.
    """
    if not text:
        return text, 0
    count = 0
    for value in sorted(_secret_values, key=len, reverse=True):
        if value in text:
            count += text.count(value)
            text = text.replace(value, f"{REDACTED}api_key]")
    for kind, pattern in _SECRET_PATTERNS:
        marker = f"{REDACTED}{kind}]"
        if "keep" in pattern.groupindex:
            text, hits = pattern.subn(lambda m, marker=marker: m.group("keep") + marker, text)
        else:
            text, hits = pattern.subn(marker, text)
        count += hits

    def assignment(match: re.Match[str]) -> str:
        nonlocal count
        if not _value_is_secret(match.string, match):
            return match.group(0)
        count += 1
        quote = match.group("q")
        return (
            f"{match.group('key')}{match.group('kq')}{match.group('sep')}"
            f"{quote}{REDACTED}{match.group('key').lower()}]{quote}"
        )

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
    r"(?i)^(?:\.env(?:\..+)?|.+\.env|\.envrc|.+\.(?:pem|key|p8|p12|pfx|ppk|jks|keystore|bks|kdbx|ovpn|asc|gpg)|"
    r"id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|"
    r"\.netrc|_netrc|\.npmrc|\.yarnrc(?:\.yml)?|\.pypirc|\.pgpass|\.git-credentials|\.htpasswd|"
    r"credentials(?:\.json|\.xml|\.txt|\.ya?ml|\.csv)?|auth\.json|kubeconfig(?:\.ya?ml)?|\.vault-token|\.dockercfg|"
    r".+\.tfvars(?:\.json)?|.+\.tfstate(?:\.backup)?|"
    r"(?:keystore|signing|secrets?|release-signing|local|gradle)\.properties|secrets?\.(?:json|ya?ml|toml|xml)|service-account.*\.json|"
    r"serviceaccountkey.*\.json|firebase-adminsdk.*\.json|client_secret.*\.json|"
    r"google-services\.json|googleservice-info\.plist|\.(?:bash|zsh|python|psql|mysql|node_repl)_history|consolehost_history\.txt|"
    r"cookies|login data|web data|storage_state\.json)$"
)
# Folders whose files are credentials or a signed-in browser.
_SENSITIVE_DIRS = frozenset(
    {".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker", "gcloud", ".edge-profile", "msedge-user-data", ".bot-browser"}
)


def _normalize_part(part: str) -> str:
    """A path part as Windows resolves it: no ``:stream`` / ``::$DATA`` suffix, no trailing dots or spaces."""
    if ":" in part and not re.fullmatch(r"[A-Za-z]:", part):
        part = part.split(":", 1)[0]
    return part.rstrip(". ").lower()


def sensitive_file(path: Path | str) -> bool:
    """True for a file that usually holds credentials: .env, keys and keystores, .netrc, signing properties.

    ``gradle.properties`` and ``local.properties`` count: in Android projects they hold
    signing passwords and repository tokens. Names are compared the way Windows opens
    them (any case, ``:stream`` suffixes and trailing dots or spaces ignored).
    """
    parts = [_normalize_part(part) for part in re.split(r"[\\/]+", str(path)) if part]
    if not parts:
        return False
    if any(part in _SENSITIVE_DIRS for part in parts):
        return True
    return bool(_SENSITIVE_NAMES.match(parts[-1]))


# --------------------------------------------------------------------------- custom terms

#: The marker for a company, customer, or project name from ``redact_terms``.
NAME_MARKER = f"{REDACTED}name]"
#: A term this short hits too much unrelated text.
MIN_TERM_CHARS = 3
MAX_TERM_CHARS = 200
# Spaces, hyphens, underscores, and dots between the words of a term are interchangeable and optional.
_TERM_SPLIT_RE = re.compile(r"[\s_.\-]+")
_TERM_SEP = r"[\s_.\-]{0,3}"
# A term starts and ends where a letter does not continue it, or at a camelCase step
# (``myAcme``, ``AcmeClient``, ``XMLAcme``, ``ACMEClient``). Digits, ``_``, ``.`` and ``-`` are boundaries.
_CAMEL_STEP = r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
_TERM_LEFT = rf"(?:(?<![^\W\d_])|{_CAMEL_STEP})"
_TERM_RIGHT = rf"(?:(?![^\W\d_])|{_CAMEL_STEP})"

# Replaced as a whole so a reader never sees terms from one call and a pattern from another.
_custom: tuple[tuple[str, ...], re.Pattern[str] | None] = ((), None)


def clean_terms(values: Iterable[object]) -> tuple[str, ...]:
    """``values`` as redaction terms: stripped, inner spaces collapsed, without case-insensitive duplicates.

    Raises ValueError for an entry that is not a string, is empty, has fewer than
    :data:`MIN_TERM_CHARS` letters or digits, or is longer than :data:`MAX_TERM_CHARS`.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{value!r} is not text")
        term = " ".join(value.split())
        if not term:
            raise ValueError("an entry is empty")
        if len(term) > MAX_TERM_CHARS:
            raise ValueError(f"{term[:40]!r}... is longer than {MAX_TERM_CHARS} characters")
        if sum(ch.isalnum() for ch in term) < MIN_TERM_CHARS:
            raise ValueError(f"{term!r} is too short (at least {MIN_TERM_CHARS} letters or digits)")
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            terms.append(term)
    return tuple(terms)


def _term_body(term: str) -> str:
    words: list[str] = []
    for piece in _TERM_SPLIT_RE.split(term):
        words.extend(word for word in re.split(r"(?<=[a-z])(?=[A-Z])", piece) if word)
    return _TERM_SEP.join(re.escape(word) for word in words)


def set_custom_terms(terms: Iterable[object]) -> tuple[str, ...]:
    """Hide these names (company, customer, project) in everything sent from now on; returns the cleaned terms.

    Called on config load with ``redact_terms`` (and ``CRIT_REDACT_TERMS``). An empty list turns it off.
    """
    global _custom
    cleaned = clean_terms(terms)
    if not cleaned:
        _custom = ((), None)
        return cleaned
    bodies = sorted({_term_body(term) for term in cleaned}, key=len, reverse=True)
    pattern = re.compile(rf"{_TERM_LEFT}(?i:{'|'.join(bodies)}){_TERM_RIGHT}")
    _custom = (cleaned, pattern)
    return cleaned


def custom_terms() -> tuple[str, ...]:
    """The names :func:`redact_outgoing` hides now."""
    return _custom[0]


def redact_terms(text: str) -> tuple[str, int]:
    """``text`` with each custom term replaced by ``[REDACTED:name]``, and how many were replaced."""
    pattern = _custom[1]
    if not text or pattern is None:
        return text, 0
    return pattern.subn(NAME_MARKER, text)


# --------------------------------------------------------------------------- personal data

_pii_enabled = True


def set_pii_redaction(enabled: bool) -> None:
    """Turn personal-data redaction on (the default) or off (``redact_pii: false``)."""
    global _pii_enabled
    _pii_enabled = bool(enabled)


def pii_redaction_enabled() -> bool:
    return _pii_enabled


# Not part of a longer number, word, version (``1.2.3``), or path on either side.
_NUM_LEFT = r"(?<![\w.\-+/])"
_NUM_RIGHT = r"(?![\w\-/]|\.\d)"
# A dot-free domain or a common/country TLD keeps ``x@self.weight`` and ``this@Outer.field`` out.
_EMAIL_TLDS = (
    "com|org|net|edu|gov|mil|int|info|biz|io|ai|dev|app|co|me|tech|cloud|online|site|xyz|email|"
    "mail|pro|name|corp|company|global|group|solutions|systems|digital|[a-z]{2}"
)
_EMAIL_RE = re.compile(
    rf"(?<![\w.%+\-:/])(?P<local>[A-Za-z0-9][A-Za-z0-9._%+\-]{{0,63}})@"
    rf"(?P<domain>(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{{0,61}}[A-Za-z0-9])?\.)+(?i:{_EMAIL_TLDS}))(?![\w\-]|\.[A-Za-z0-9])"
)
_EXAMPLE_DOMAIN_RE = re.compile(r"(?i)(?:^|\.)(?:example\.(?:com|org|net)|localhost)$")
_SERVICE_LOCAL_RE = re.compile(r"(?i)^(?:git|no-?reply)$")
_CARD_RE = re.compile(rf"{_NUM_LEFT}\d(?:[ \-]?\d){{12,18}}{_NUM_RIGHT}")
_IBAN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?(?![A-Za-z0-9])")
_SSN_RE = re.compile(rf"{_NUM_LEFT}(?!000|666|9\d\d)\d{{3}}-(?!00)\d{{2}}-(?!0000)\d{{4}}{_NUM_RIGHT}")
_AADHAAR_RE = re.compile(rf"{_NUM_LEFT}[2-9]\d{{3}}([ \-])\d{{4}}\1\d{{4}}(?![\w\-/]|\.\d|[ \-]\d)")
_E164_RE = re.compile(r"(?<![\w.\-/])\+[1-9](?:[ \-.]?\(?\d{1,5}\)?){2,7}(?![\w\-]|\.\d)")
_NATIONAL_PHONE_RE = re.compile(
    rf"(?:(?<![\w.\-+/(])\(\d{{3}}\) ?\d{{3}}-\d{{4}}"  # (555) 123-4567
    rf"|{_NUM_LEFT}\d{{3}}-\d{{3}}-\d{{4}}"  # 555-123-4567
    rf"|{_NUM_LEFT}\d{{3}}\.\d{{3}}\.\d{{4}}"  # 555.123.4567
    rf"|{_NUM_LEFT}0\d{{2,4}} \d{{3,4}} \d{{3,4}})"  # 020 7946 0958, 01632 960 001
    rf"{_NUM_RIGHT}"
)
_PAN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{3}[ABCFGHJLPT][A-Z]\d{4}[A-Z](?![A-Za-z0-9])")
_NI_RE = re.compile(
    r"(?<![A-Za-z0-9])(?!BG|GB|KN|NK|NT|TN|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]"
    r"(?:\d{6}| \d{2} \d{2} \d{2} )[A-D](?![A-Za-z0-9])"
)
_IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22, "BR": 29,
    "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "DO": 28, "EE": 20, "EG": 29,
    "ES": 24, "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28,
    "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20,
    "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MD": 24, "ME": 22, "MK": 19,
    "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15, "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29,
    "RO": 24, "RS": 22, "SA": 24, "SC": 31, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28,
    "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
}
_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5), (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7), (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3), (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4), (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7), (9, 4, 5, 3, 1, 2, 7, 6, 8, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, ch in enumerate(reversed(digits)):
        value = int(ch)
        if index % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _card_prefix_ok(digits: str) -> bool:
    """An issuer prefix and length real cards use (Visa, Mastercard, Amex, Discover, JCB, Diners, UnionPay, Maestro)."""
    n = len(digits)
    two, four = int(digits[:2]), int(digits[:4])
    return (
        (digits[0] == "4" and n in (13, 16, 19))
        or ((51 <= two <= 55 or 2221 <= four <= 2720) and n == 16)
        or (two in (34, 37) and n == 15)
        or ((digits.startswith(("6011", "65")) or 644 <= int(digits[:3]) <= 649) and 16 <= n <= 19)
        or (3528 <= four <= 3589 and 16 <= n <= 19)
        or ((300 <= int(digits[:3]) <= 305 or two in (36, 38, 39)) and 14 <= n <= 19)
        or (two == 62 and 16 <= n <= 19)
        or ((two == 50 or 56 <= two <= 69) and 12 <= n <= 19)
    )


def _card_number(text: str) -> bool:
    seps = {ch for ch in text if not ch.isdigit()}
    if len(seps) > 1:
        return False
    if seps:
        groups = re.split(r"[ \-]", text)
        if not all(3 <= len(group) <= 6 for group in groups):
            return False
    digits = re.sub(r"\D", "", text)
    return len(set(digits)) > 1 and _card_prefix_ok(digits) and _luhn_ok(digits)


def _verhoeff_ok(digits: str) -> bool:
    check = 0
    for index, ch in enumerate(reversed(digits)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[index % 8][int(ch)]]
    return check == 0


def _iban_ok(compact: str) -> bool:
    if _IBAN_LENGTHS.get(compact[:2]) != len(compact):
        return False
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(ch, 36)) for ch in rearranged)) % 97 == 1


def _phone_digits_ok(text: str) -> bool:
    digits = sum(ch.isdigit() for ch in text)
    return 9 <= digits <= 15 and text.count("(") == text.count(")")


def redact_pii(text: str) -> tuple[str, int]:
    """``text`` with personal data replaced by ``[REDACTED:kind]``, and how many were replaced.

    Emails (not @example.com/.org/.net, @localhost, ``git@``, or ``noreply@``), phone numbers
    (``+`` international, and US / UK national formats with separators), payment cards
    (issuer prefix and Luhn check), IBANs (mod-97 check), US SSNs, Aadhaar numbers (4-4-4
    with the Verhoeff check), Indian PANs, and UK National Insurance numbers. IPv4 addresses,
    versions, dates, and plain numbers are left alone.
    """
    if not text or not _pii_enabled:
        return text, 0
    count = 0

    def replace(pattern: re.Pattern[str], kind: str, valid) -> None:
        nonlocal text, count
        marker = f"{REDACTED}{kind}]"

        def one(match: re.Match[str]) -> str:
            nonlocal count
            if not valid(match):
                return match.group(0)
            count += 1
            return marker

        text = pattern.sub(one, text)

    replace(
        _EMAIL_RE,
        "email",
        lambda m: not _EXAMPLE_DOMAIN_RE.search(m.group("domain")) and not _SERVICE_LOCAL_RE.match(m.group("local")),
    )

    def iban(match: re.Match[str]) -> str:
        nonlocal count
        found = match.group(0)
        compact = found.replace(" ", "")
        length = _IBAN_LENGTHS.get(compact[:2], 0)
        if not length or len(compact) < length or not _iban_ok(compact[:length]):
            return found
        # Keep anything matched past the IBAN's own length (a word that followed it).
        seen = end = 0
        while seen < length:
            if found[end] != " ":
                seen += 1
            end += 1
        if end < len(found) and found[end] != " ":
            return found
        count += 1
        return f"{REDACTED}iban]" + found[end:]

    text = _IBAN_RE.sub(iban, text)
    replace(_CARD_RE, "card number", lambda m: _card_number(m.group(0)))
    replace(_AADHAAR_RE, "aadhaar", lambda m: _verhoeff_ok(re.sub(r"\D", "", m.group(0))))
    replace(_SSN_RE, "ssn", lambda m: True)

    def e164(match: re.Match[str]) -> bool:
        found = match.group(0)
        start = match.start()
        # A diff line that adds a bare run of digits is not a phone number.
        if (start == 0 or match.string[start - 1] == "\n") and found[1:].isdigit():
            return False
        return _phone_digits_ok(found)

    replace(_E164_RE, "phone", e164)
    replace(_NATIONAL_PHONE_RE, "phone", lambda m: 10 <= sum(ch.isdigit() for ch in m.group(0)) <= 11)
    replace(_PAN_RE, "pan", lambda m: True)
    replace(_NI_RE, "ni number", lambda m: True)
    return text, count


def redact_outgoing(prompt: str, *, report: bool = True) -> str:
    """``prompt`` with secrets, personal data, and custom names replaced by ``[REDACTED:kind]``.

    Every backend sends through this. Order: secrets, then personal data (so an email at a
    company domain goes as a whole), then the ``redact_terms`` names. ``report=False`` (the
    log file's own lines) leaves out the "redacted N ..." log lines.
    """
    text, hidden = redact_secrets(prompt)
    if not report:
        return redact_terms(redact_pii(text)[0])[0]
    if hidden:
        log.info(f"redacted {hidden} secret(s) before sending")
    text, personal = redact_pii(text)
    if personal:
        log.info(f"redacted {personal} personal data item(s) before sending")
    text, names = redact_terms(text)
    if names:
        log.info(f"redacted {names} custom name(s) before sending")
    return text
