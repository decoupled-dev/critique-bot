"""The model layer's interface: everything above it talks to a model through this module.

A backend (a model served on this PC, or the optional web chat page in Edge) implements
:class:`ChatProvider` and :class:`ChatSession`. The engine, the review apps, and the CI
worker depend only on these types, the errors, and the helpers below, so a backend can
change, or a new one be added, without touching them, and they can be tested with a fake
session (any object with ``send``; the rest is optional).

The local server backend is :mod:`critique_bot.local_model`. Other backends register
themselves with :func:`register_backend`: the web chat in :mod:`critique_bot.webchat`, a
removable package that nothing else imports. :func:`open_provider` picks one from
``backend`` in ``config.json``; :func:`backend_hooks` hands the command line a backend's
extra hooks (sign-in, setup) or None when that backend is not installed.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

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
    "backend_hooks",
    "register_backend",
    "context_tokens",
    "list_models",
    "model_label",
    "model_profile",
    "native_tools_ok",
    "open_provider",
    "probe_tool_turn",
    "profiles_dir",
    "record_probe",
    "server_presets",
    "probe_server",
    "probe_site",
    "save_failure",
    "server_info",
    "tool_mode",
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


#: Backends other than the local server, by ``config.backend`` name: (factory, hooks).
_BACKENDS: dict[str, tuple[Callable[..., ChatProvider], Any]] = {}
#: Optional packages that register a backend when imported, by backend name.
_OPTIONAL_BACKENDS = {"browser": "critique_bot.webchat"}
_MISSING_HINT = {
    "browser": (
        "the web UI backend is not installed in this crit (critique_bot.webchat is missing). "
        "Install it with: pip install 'critique-bot[webchat]', or use a local model: "
        "crit config --backend local --local-url http://127.0.0.1:8080"
    ),
}


def register_backend(name: str, factory: Callable[..., ChatProvider], *, hooks: Any = None) -> None:
    """Make ``backend: <name>`` open ``factory(config, headed=...)``. Called by optional packages."""
    _BACKENDS[name] = (factory, hooks)


def _load_backend(name: str) -> tuple[Callable[..., ChatProvider], Any] | None:
    if name not in _BACKENDS and name in _OPTIONAL_BACKENDS:
        try:
            importlib.import_module(_OPTIONAL_BACKENDS[name])
        except ImportError:
            return None
    return _BACKENDS.get(name)


def backend_hooks(name: str = "browser") -> Any:
    """The extra hooks a backend offers the command line, or None when it is not installed."""
    found = _load_backend(name)
    return found[1] if found else None


def open_provider(config: BotConfig, *, headed: bool = False) -> ChatProvider:
    """The backend ``config.backend`` names, not started yet (use it as a context manager)."""
    if config.is_local:
        from critique_bot import log
        from critique_bot.local_model import LocalModelProvider

        if headed:
            log.info("--headed does nothing with a local model: there is no browser")
        return LocalModelProvider(config)
    found = _load_backend(config.backend)
    if found is None:
        raise BackendError(_MISSING_HINT.get(config.backend, f"unknown backend {config.backend!r}"))
    return found[0](config, headed=headed)


def backend_error(exc: BaseException) -> BackendError | None:
    """``exc`` as a BackendError when it means the backend is gone (a closed browser), else None."""
    if isinstance(exc, BackendError):
        return exc
    for _, hooks in list(_BACKENDS.values()):
        mapper = getattr(hooks, "as_backend_error", None)
        if callable(mapper):
            try:
                mapped = mapper(exc)
            except Exception:  # noqa: BLE001 - a broken mapper must not hide the real error
                continue
            if mapped is not None:
                return mapped
    return None


def save_failure(session: Any, output_dir: Path) -> None:
    """Ask ``session`` to save its failure artifacts. Never raises: it runs while handling an error."""
    saver = getattr(session, "save_failure", None)
    if not callable(saver):
        return
    try:
        saver(Path(output_dir))
    except Exception:  # noqa: BLE001 - debugging help must not hide the real failure
        return


def list_models(config: BotConfig) -> list[str]:
    """The models a local server offers (its /v1/models). Raises ChatError when unreachable."""
    from critique_bot.local_model import LocalModelClient

    return LocalModelClient(config).list_models()


def probe_server(url: str, api_key: str | None = None) -> Any:
    """Whether a model server answers at ``url`` and which models it lists (see local_model.Probe)."""
    from critique_bot.local_model import probe_server as probe

    return probe(url, api_key)


def probe_site(url: str) -> Any:
    """Whether a web page's host resolves and answers (see local_model.Probe)."""
    from critique_bot.local_model import probe_site as probe

    return probe(url)


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
    from critique_bot.local_model import DEFAULT_NUM_CTX, server_context_window

    if config.local.num_ctx:
        return config.local.num_ctx  # the person's own number wins over the profile
    window = server_context_window(config.url, config.model, config.local.api_key) or DEFAULT_NUM_CTX
    limit = model_profile(config).effective_context
    return min(window, limit) if limit else window


def model_profile(config: BotConfig) -> Any:
    """The model profile (:class:`critique_bot.profiles.Profile`) for ``config``: ``local.profile``,
    else the one matching ``model``, else ``generic``. A broken profile set falls back to generic."""
    from critique_bot import log, profiles

    try:
        return profiles.resolve(config.model or "", config.local.profile)
    except profiles.ProfileError as exc:
        log.warn(f"model profiles: {exc}; using the generic profile")
        return profiles.Profile(id=profiles.GENERIC)


def tool_mode(config: BotConfig) -> str:
    """``native`` (the server's tool calling) or ``prompted`` (tools described in the prompt,
    calls written as text) for a local model; the profile decides, and ``auto`` trusts the
    last ``crit doctor model`` probe for this server and model. ``prompted`` for the web chat."""
    if not config.is_local:
        return "prompted"
    from critique_bot import profiles

    return profiles.effective_tool_mode(model_profile(config), config.url, config.model or "")


def native_tools_ok(config: BotConfig) -> bool:
    """Whether the engine should send tools natively (a local model whose profile allows it)."""
    return config.is_local and tool_mode(config) == "native"


def server_info(config: BotConfig) -> dict[str, Any]:
    """What the local server says about itself (type, version, context, KV cache; see
    local_model.detect_server). For ``crit doctor model``."""
    from critique_bot.local_model import detect_server

    return detect_server(config.url, config.local.api_key, config.model or "")


def probe_tool_turn(
    config: BotConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, model: str | None = None,
) -> dict[str, Any]:
    """One native tool-call request and how its calls came back (see local_model.probe_turn)."""
    from critique_bot.local_model import probe_turn

    return probe_turn(config, messages, tools, model=model)


def record_probe(config: BotConfig, model: str, result: dict[str, Any]) -> Path:
    """Cache a ``crit doctor model`` result for this server and model (``tool_mode: auto`` reads it)."""
    from critique_bot import profiles

    return profiles.save_probe(config.url, model, result)


def server_presets() -> list[Any]:
    """The built-in server presets (critique_bot.profiles.ServerPreset), by id."""
    from critique_bot import profiles

    return profiles.server_presets()


def profiles_dir() -> Path:
    """Where a person's own model profiles go (``~/.crit/profiles``)."""
    from critique_bot import profiles

    return profiles.user_dir()
