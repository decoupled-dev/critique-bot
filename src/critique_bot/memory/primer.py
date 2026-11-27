# SPDX-License-Identifier: Apache-2.0
"""KV-cache priming for llama.cpp: save and restore a slot's state for the project primer.

llama.cpp started with ``--slot-save-path DIR`` can write a slot's KV cache to a file and load
it back (``POST /slots/{id}?action=save|restore`` with ``{"filename": ...}``). crit's primer is
the session's fixed prefix (system prompt, tools, memory files, the module repo map). Restoring
its KV state at session start skips processing it again.

The file name comes from :func:`primer_key`: a hash of the primer's exact bytes, the model, its
quantization and the context size, so any change gives a new key and the stale state is never
loaded. Off unless the profile or settings turn it on (``"kv_primer": true``); the server must
have ``--slot-save-path``. This is an optimization only: every failure falls back to a normal start.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

TIMEOUT = 60.0


def enabled(settings: dict[str, Any] | None, profile: dict[str, Any] | None = None) -> bool:
    """``"kv_primer": true`` in settings wins; otherwise the model profile's ``kv_primer``."""
    settings = settings or {}
    if "kv_primer" in settings:
        return settings.get("kv_primer") is True
    return bool((profile or {}).get("kv_primer") is True)


def primer_key(primer: str | bytes, *, model: str, quant: str = "", context: int = 0) -> str:
    data = primer.encode("utf-8") if isinstance(primer, str) else bytes(primer)
    meta = json.dumps({"model": model, "quant": quant, "context": int(context or 0)}, sort_keys=True).encode()
    return hashlib.sha256(meta + b"\0" + data).hexdigest()


def file_name(key: str) -> str:
    return f"crit-primer-{key[:32]}.bin"


def server_root(base_url: str) -> str:
    """``http://host:8080/v1`` -> ``http://host:8080`` (the slot endpoints are not under /v1)."""
    parsed = urllib.parse.urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"not an http(s) server address: {base_url!r}")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


@dataclass
class Result:
    action: str  # restored | built | skipped
    detail: str = ""


class SlotPrimer:
    """Save / restore one slot's KV state on a llama.cpp server."""

    def __init__(self, base_url: str, *, slot: int = 0, timeout: float = TIMEOUT, headers: dict[str, str] | None = None) -> None:
        self.root = server_root(base_url)
        self.slot = int(slot)
        self.timeout = timeout
        self.headers = dict(headers or {})

    def _post(self, action: str, key: str) -> tuple[bool, str]:
        url = f"{self.root}/slots/{self.slot}?action={action}"
        body = json.dumps({"filename": file_name(key)}).encode()
        request = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", **self.headers})
        try:
            # the model server's own address from the config (server_root checked the scheme)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # nosec B310
                text = response.read(64_000).decode("utf-8", "replace")
                return 200 <= response.status < 300, text
        except urllib.error.HTTPError as exc:
            try:
                return False, f"HTTP {exc.code}: {exc.read(2000).decode('utf-8', 'replace')}"
            finally:
                exc.close()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return False, str(exc)

    def save(self, key: str) -> tuple[bool, str]:
        return self._post("save", key)

    def restore(self, key: str) -> tuple[bool, str]:
        return self._post("restore", key)

    def ensure(self, key: str, warm: Callable[[], None]) -> Result:
        """Restore the primer's state; when there is none, run ``warm`` (one request that processes
        the primer in this slot) and save it for next time."""
        done, detail = self.restore(key)
        if done:
            return Result("restored", detail[:200])
        try:
            warm()
        except Exception as exc:  # the primer is an optimization: a failure is a normal start
            return Result("skipped", f"warm-up failed: {exc}")
        saved, why = self.save(key)
        return Result("built" if saved else "skipped", (why if not saved else "")[:200])
