from __future__ import annotations

import re

LOG_METHODS = {
    "v": "v",
    "d": "d",
    "i": "i",
    "w": "w",
    "e": "e",
    "wtf": "wtf",
    "verbose": "v",
    "debug": "d",
    "info": "i",
    "warn": "w",
    "warning": "w",
    "error": "e",
}

SHORT_LEVELS = {"v", "d", "i", "w", "e", "wtf"}
PRINT_METHODS = {"print", "println"}

WRAPPER_RECEIVER_RE = re.compile(
    r"(log|logger|timber)",
    re.IGNORECASE,
)

ANDROID_LOG_RE = re.compile(r"(^|\.)Log$")
TIMBER_RE = re.compile(r"(^|\.)Timber(\.|$)|Timber\.tag", re.IGNORECASE)
SYSTEM_OUT_RE = re.compile(r"^System\.out$")
SYSTEM_ERR_RE = re.compile(r"^System\.err$")


def _norm_receiver(receiver: str) -> str:
    return re.sub(r"\s+", "", receiver or "")


def classify_call(receiver: str, method: str) -> tuple[str, str] | None:
    """Return (level, api) when this invocation is a logging call."""
    method_name = (method or "").strip()
    method_key = method_name.lower()
    recv = _norm_receiver(receiver)

    if method_key in PRINT_METHODS:
        if not recv:
            return method_key, method_key
        if SYSTEM_OUT_RE.match(recv):
            return method_key, "System.out"
        if SYSTEM_ERR_RE.match(recv):
            return method_key, "System.err"
        if ANDROID_LOG_RE.search(recv) and method_key == "println":
            return "println", "android.util.Log"
        return None

    level = LOG_METHODS.get(method_key)
    if level is None:
        return None

    if ANDROID_LOG_RE.search(recv):
        return level, "android.util.Log"
    if TIMBER_RE.search(recv):
        return level, "Timber"
    if recv and WRAPPER_RECEIVER_RE.search(recv):
        return level, "wrapper"
    if method_key in SHORT_LEVELS:
        return None
    return None


def snippet_from_text(text: str, limit: int = 220) -> str:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if len(compact) > limit:
        return compact[: limit - 1] + "…"
    return compact
