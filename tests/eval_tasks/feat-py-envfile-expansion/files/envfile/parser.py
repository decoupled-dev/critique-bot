"""Parse ``KEY=value`` lines as used by docker-compose style .env files."""

import os
import re
from pathlib import Path

KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


class EnvFileError(ValueError):
    pass


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


def _parse_value(raw: str, lineno: int) -> str:
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
        return body if quote == "'" else _unquote_double(body, lineno)
    # unquoted: strip an inline comment that follows whitespace
    m = re.search(r"\s#", raw)
    if m:
        raw = raw[: m.start()]
    return raw.strip()


def parse(text: str, environ=None) -> dict:
    """Return the key/value pairs defined in ``text``, in file order."""
    environ = os.environ if environ is None else environ  # used for lookups
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
        result[key] = _parse_value(raw, lineno)
    return result


def load(path, environ=None) -> dict:
    return parse(Path(path).read_text(encoding="utf-8"), environ)
