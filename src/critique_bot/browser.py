from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page


class BrowserError(RuntimeError):
    """Edge could not be launched or the page could not be created."""


EDGE_LAUNCH_ARGS = ("--disable-dev-shm-usage",)


def _helpful_edge_error(exc: BaseException) -> BrowserError:
    return BrowserError(
        "Failed to launch Microsoft Edge via Playwright (channel=msedge). "
        "Install Edge (microsoft-edge-stable on Linux; Edge is typically "
        "preinstalled on Windows). On Linux runners you may also need: "
        "playwright install-deps. Original error: "
        f"{exc}"
    )


@contextmanager
def launch_edge(
    *,
    headed: bool = False,
    storage_state: str | None = None,
) -> Iterator[Page]:
    """Launch installed Microsoft Edge and yield a single page."""
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise BrowserError(
            "Playwright is required. Install with: pip install -r requirements.txt"
        ) from exc

    playwright = None
    browser = None
    context = None
    try:
        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.launch(
                channel="msedge",
                headless=not headed,
                args=list(EDGE_LAUNCH_ARGS),
            )
        except Exception as exc:
            raise _helpful_edge_error(exc) from exc

        context_kwargs: dict[str, object] = {}
        if storage_state:
            context_kwargs["storage_state"] = storage_state
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        yield page
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
