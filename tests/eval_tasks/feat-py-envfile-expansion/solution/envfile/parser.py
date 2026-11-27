"""Parse ``KEY=value`` lines as used by docker-compose style .env files."""

import os
import re
from pathlib import Path

KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}
_REF_RE = re.compile(r"\$(?:\$|\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\})")


class EnvFileError(ValueError):
    pass


def _expand(value: str, lineno: int, defined: dict, environ) -> str:
    """Expand ``${NAME}``, ``${NAME:-fallback}`` and ``$$`` in one pass."""

    def lookup(m: re.Match) -> str:
        if m.group(0) == "$$":
            return "$"
        name, fallback = m.group(1), m.group(2)
        current = defined[name] if name in defined else environ.get(name)
        if fallback is not None:
            return current if current else fallback
        if current is None:
            raise EnvFileError(f"line {lineno}: variable {name} is not set")
        return current

    return _REF_RE.sub(lookup, value)


def _unquote_double(raw: str, lineno: int) -> str:
    out = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append(_ESCAPES.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _parse_value(raw: str, lineno: int) -> tuple[str, bool]:
    """Return ``(value, literal)``; literal (single-quoted) values are not expanded."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        end = raw.find(quote, 1)
        while quote == '"' and end > 0 and raw[end - 1] == "\\":
            end = raw.find(quote, end + 1)
        if end < 0:
            raise EnvFileError(f"line {lineno}: unterminated {quote} quote")
        rest = raw[end + 1:].strip()
        if rest and not rest.startswith("#"):
            raise EnvFileError(f"line {lineno}: text after closing quote")
        body = raw[1:end]
        if quote == "'":
            return body, True
        return _unquote_double(body, lineno), False
    # unquoted: strip an inline comment that follows whitespace
    m = re.search(r"\s#", raw)
    if m:
        raw = raw[: m.start()]
    return raw.strip(), False


def parse(text: str, environ=None) -> dict:
    """Return the key/value pairs defined in ``text``, in file order."""
    environ = os.environ if environ is None else environ
    result: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        if "=" not in stripped:
            raise EnvFileError(f"line {lineno}: expected KEY=value")
        key, raw = stripped.split("=", 1)
        key = key.strip()
        if not KEY_RE.match(key):
            raise EnvFileError(f"line {lineno}: invalid key {key!r}")
        value, literal = _parse_value(raw, lineno)
        result[key] = value if literal else _expand(value, lineno, result, environ)
    return result


def load(path, environ=None) -> dict:
    return parse(Path(path).read_text(encoding="utf-8"), environ)
