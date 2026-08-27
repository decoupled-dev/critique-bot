from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from critique_bot import log

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page


class BrowserError(RuntimeError):
    """Edge could not be launched or the page could not be created."""


EDGE_LAUNCH_ARGS = (
    "--disable-dev-shm-usage",
    "--hide-crash-restore-bubble",
)

_LOGIN_HINTS = ("login", "signin", "sign-in", "sso", "oauth", "auth")


def _helpful_edge_error(exc: BaseException) -> BrowserError:
    message = str(exc).lower()
    if any(token in message for token in ("singleton", "profile", "lock", "in use")):
        return BrowserError(
            "Microsoft Edge profile is already in use by another Edge window. "
            "Close that Edge, or start Edge with --remote-debugging-port=9222 "
            "and set cdp_url (or CRITIQUE_CDP_URL) so the bot attaches to the "
            "signed-in instance instead of launching a new one. Original error: "
            f"{exc}"
        )
    return BrowserError(
        "Failed to launch Microsoft Edge via Playwright (channel=msedge). "
        "Install Edge (microsoft-edge-stable on Linux; Edge is typically "
        "preinstalled on Windows). On Linux runners you may also need: "
        "playwright install-deps. Original error: "
        f"{exc}"
    )


def _page_from_context(context: BrowserContext) -> Page:
    for page in context.pages:
        if not page.is_closed():
            log.debug(f"reusing open tab {describe_page(page)}")
            return page
    log.debug("no open tab in context; creating a new page")
    return context.new_page()


def describe_page(page: Page) -> str:
    try:
        title = page.title()
    except Exception as exc:
        title = f"<title error: {exc}>"
    try:
        url = page.url
    except Exception as exc:
        url = f"<url error: {exc}>"
    return f"url={url!r} title={title!r}"


def warn_if_login_page(page: Page) -> None:
    try:
        url = page.url.lower()
    except Exception:
        return
    if any(hint in url for hint in _LOGIN_HINTS):
        log.warn(
            "current URL looks like a login/SSO page; the session may not be signed in "
            f"({page.url})"
        )


def attach_page_debug(page: Page) -> None:
    def on_console(msg) -> None:
        text = log.preview(msg.text, 240)
        location = ""
        try:
            loc = msg.location
            if isinstance(loc, dict):
                location = f" @ {loc.get('url', '')}:{loc.get('lineNumber', '?')}"
            elif loc:
                location = f" @ {loc}"
        except Exception:
            pass
        line = f"browser console [{msg.type}] {text}{location}"
        if msg.type in ("error", "warning"):
            log.warn(line)
        else:
            log.debug(line)

    def on_page_error(err) -> None:
        log.error(f"page error: {err}")

    def on_request_failed(request) -> None:
        failure = request.failure or {}
        error_text = failure.get("errorText") if isinstance(failure, dict) else failure
        log.warn(
            "request failed "
            f"{request.method} {log.preview(request.url, 180)} "
            f"error={error_text!r}"
        )

    def on_response(response) -> None:
        status = response.status
        if status >= 400:
            log.warn(
                f"http {status} {response.request.method} "
                f"{log.preview(response.url, 180)}"
            )

    def on_frame_nav(frame) -> None:
        if frame.parent_frame is None:
            log.debug(f"navigated {frame.url}")

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)
    page.on("framenavigated", on_frame_nav)
    log.debug("attached browser console/network debug listeners")


def _log_context(context: BrowserContext, *, via: str) -> None:
    pages = list(context.pages)
    log.info(f"{via}: {len(pages)} existing tab(s)")
    for index, page in enumerate(pages):
        log.debug(f"  tab[{index}] {describe_page(page)}")
    try:
        cookies = context.cookies()
        log.info(f"{via}: {len(cookies)} cookie(s) in this profile/context")
        hosts = sorted({c.get("domain", "?") for c in cookies})
        if hosts:
            log.debug(f"{via}: cookie domains={hosts}")
    except Exception as exc:
        log.warn(f"{via}: could not read cookies: {exc}")


@contextmanager
def launch_edge(
    *,
    headed: bool = False,
    storage_state: str | None = None,
    user_data_dir: str | None = None,
    cdp_url: str | None = None,
) -> Iterator[Page]:
    """Open Microsoft Edge on a persistent signed-in profile (or attach via CDP)."""
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise BrowserError(
            "Playwright is required. Install with: pip install -r requirements.txt"
        ) from exc

    playwright = None
    context = None
    attached_page = None
    try:
        log.info("starting Playwright")
        playwright = sync_playwright().start()
        if cdp_url:
            log.info(f"attaching to running Edge via CDP {cdp_url}")
            try:
                browser = playwright.chromium.connect_over_cdp(cdp_url)
            except Exception as exc:
                log.exception(f"CDP attach failed: {exc}")
                raise BrowserError(
                    f"Failed to attach to a running Edge at {cdp_url}. "
                    "Start Edge with --remote-debugging-port=9222 (and the "
                    "matching --user-data-dir if you use a custom profile), "
                    "then retry. Original error: "
                    f"{exc}"
                ) from exc
            log.info(
                f"CDP connected: {len(browser.contexts)} context(s) "
                f"version={getattr(browser, 'version', '?')}"
            )
            if not browser.contexts:
                raise BrowserError(
                    f"Connected to Edge at {cdp_url} but found no browser context. "
                    "Open a window in that Edge instance and retry."
                )
            context = browser.contexts[0]
            _log_context(context, via="cdp")
            attached_page = context.new_page()
            attach_page_debug(attached_page)
            log.info(f"opened new tab on signed-in instance {describe_page(attached_page)}")
            yield attached_page
            return

        profile_dir = Path(user_data_dir or ".edge-profile").expanduser()
        profile_dir.mkdir(parents=True, exist_ok=True)
        existing = profile_dir.exists() and any(profile_dir.iterdir())
        log.info(
            "launching persistent Edge "
            + log.kv(
                channel="msedge",
                headed=headed,
                headless=not headed,
                user_data_dir=str(profile_dir),
                profile_has_data=existing,
                storage_state=storage_state,
                args=list(EDGE_LAUNCH_ARGS),
            )
        )
        launch_kwargs: dict[str, object] = {
            "channel": "msedge",
            "headless": not headed,
            "args": list(EDGE_LAUNCH_ARGS),
        }
        if storage_state:
            launch_kwargs["storage_state"] = storage_state
            log.debug(f"seeding profile with storage_state {storage_state}")
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                **launch_kwargs,
            )
        except Exception as exc:
            log.exception(f"Edge launch failed: {exc}")
            raise _helpful_edge_error(exc) from exc
        _log_context(context, via="persistent")
        page = _page_from_context(context)
        attach_page_debug(page)
        log.info(f"Edge ready {describe_page(page)}")
        yield page
    finally:
        log.info("closing Edge / Playwright")
        if attached_page is not None:
            try:
                log.debug(f"closing attached tab {describe_page(attached_page)}")
                attached_page.close()
            except Exception as exc:
                log.debug(f"attached tab already closed: {exc}")
        elif context is not None:
            try:
                context.close()
            except Exception as exc:
                log.debug(f"context close: {exc}")
        if playwright is not None:
            playwright.stop()
        log.debug("Playwright stopped")
