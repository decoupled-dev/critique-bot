"""The model layer's interface: everything above it talks to a model through this module.

A backend (the web chat page in Edge, or a model served on this PC) implements
:class:`ChatProvider` and :class:`ChatSession`. The engine, the review apps, and the CI
worker depend only on these types, the errors, and the helpers below, so a backend can
change, or a new one be added, without touching them, and they can be tested with a fake
session (any object with ``send``; the rest is optional).

Concrete backends live in :mod:`critique_bot.provider` (browser) and
:mod:`critique_bot.local_model` (local server); :func:`open_provider` picks one from
``backend`` in ``config.json``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from critique_bot.config import BotConfig

__all__ = [
    "COMPLETION_IDLE",
    "COMPLETION_STOPPED",
    "BackendError",
    "ChatError",
    "ChatProvider",
    "ChatSession",
    "CloudflareBlocked",
    "ModelError",
    "ReplyNotStarted",
    "ReplyStalled",
    "backend_error",
    "context_tokens",
    "model_label",
    "open_provider",
    "save_failure",
]

#: The backend said generation had finished. The reply is whole.
COMPLETION_STOPPED = "stop-signal"
#: The reply merely stopped changing. It may have been cut off mid-answer.
COMPLETION_IDLE = "idle-timeout"


class ModelError(RuntimeError):
    """Any failure of the model layer: the backend could not start, or a reply did not complete."""


class BackendError(ModelError):
    """The backend could not start, or it died (the browser closed). Reopen the provider to go on."""


class ChatError(ModelError):
    """A reply did not complete (web chat page or local server)."""


class ReplyNotStarted(ChatError):
    """The page showed no reply and no generating signal after the send (and a second send)."""


class CloudflareBlocked(ChatError):
    """A Cloudflare challenge that a headless window cannot pass. Retrying headless does not help."""


class ReplyStalled(ChatError):
    """A reply started, then nothing arrived: no text and no generating signal for too long."""


class ChatSession:
    """One conversation: one browser tab, or one message list sent to a local model.

    Only :meth:`send` is required. Callers treat the rest as optional, so a test can pass
    any object with ``send``.
    """

    page: Any = None
    #: How the last reply ended (``{"completion": COMPLETION_STOPPED, ...}``). Callers can
    #: tell a reply the backend declared finished from one that went quiet.
    last_detail: dict[str, Any] | None = None

    def send(self, prompt: str) -> str:
        raise NotImplementedError

    def new_chat(self) -> bool:
        """Start a fresh conversation for the next send. False when this session cannot."""
        return False

    def stop_generation(self) -> bool:
        """Stop a reply the backend is still writing. True when one was running."""
        return False

    def save_failure(self, output_dir: Path) -> None:
        """Save what helps debug a failed turn (a browser saves a screenshot and the page HTML)."""
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> ChatSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class ChatProvider:
    """Process-level backend resources: the Edge process, or nothing for a local model."""

    #: More than one session may run at the same time (helper tabs, parallel review jobs).
    can_parallelize: bool = False

    def session(
        self,
        *,
        isolated: bool = False,
        model: str | None = None,
    ) -> ChatSession:
        raise NotImplementedError

    def close(self) -> None:
        return None

    def __enter__(self) -> ChatProvider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_provider(config: BotConfig, *, headed: bool = False) -> ChatProvider:
    """The backend ``config.backend`` names, not started yet (use it as a context manager)."""
    from critique_bot.provider import open_provider as _open

    return _open(config, headed=headed)


def backend_error(exc: BaseException) -> BackendError | None:
    """``exc`` as a BackendError when it means the backend is gone (a closed browser), else None."""
    if isinstance(exc, BackendError):
        return exc
    try:
        from critique_bot.browser import as_browser_error
    except Exception:  # noqa: BLE001 - no browser support installed: nothing to map
        return None
    return as_browser_error(exc)


def save_failure(session: Any, output_dir: Path) -> None:
    """Ask ``session`` to save its failure artifacts. Never raises: it runs while handling an error."""
    saver = getattr(session, "save_failure", None)
    if not callable(saver):
        return
    try:
        saver(Path(output_dir))
    except Exception:  # noqa: BLE001 - debugging help must not hide the real failure
        return


def model_label(config: BotConfig) -> str:
    """What a header calls the model: the name, and where it runs when it is local."""
    if not config.is_local:
        return config.model or ""
    from critique_bot.local_model import server_label

    return f"{config.model or '(first served model)'} · local at {server_label(config.url)}"


def context_tokens(config: BotConfig) -> int | None:
    """The context window in tokens when the backend has a known one (a local model), else None."""
    if not config.is_local:
        return None
    from critique_bot.local_model import DEFAULT_NUM_CTX

    return config.local.num_ctx or DEFAULT_NUM_CTX
