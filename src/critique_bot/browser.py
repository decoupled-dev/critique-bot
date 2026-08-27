from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from critique_bot import log
from critique_bot.config import system_edge_user_data_dir

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page


class BrowserError(RuntimeError):
    """Edge could not be launched or the page could not be created."""


# Used only for the isolated bot profile (not the desktop system profile).
EDGE_LAUNCH_ARGS = (
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
)

_BLANK_URLS = frozenset(
    {
        "",
        "about:blank",
        "about://blank",
        "chrome://newtab",
        "chrome://newtab/",
        "edge://newtab",
        "edge://newtab/",
        "edge://new-tab-page",
        "chrome://new-tab-page",
    }
)

_LOGIN_HINTS = ("login", "signin", "sign-in", "sso", "oauth", "auth")
_PROFILE_LOCKS = ("SingletonLock", "SingletonSocket", "SingletonCookie")


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


def _is_system_profile(user_data_dir: str | None) -> bool:
    if not user_data_dir:
        return False
    try:
        return Path(user_data_dir).expanduser().resolve() == system_edge_user_data_dir().resolve()
    except OSError:
        return False


def _run_quiet(cmd: list[str]) -> None:
    subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def close_existing_edge_sessions(*, profile_dir: Path | None = None) -> None:
    """Quit desktop Edge so the profile lock is released before a new launch."""
    log.info("closing any open Microsoft Edge sessions")
    if sys.platform == "win32":
        _run_quiet(["taskkill", "/F", "/IM", "msedge.exe", "/T"])
    elif sys.platform == "darwin":
        _run_quiet(["killall", "Microsoft Edge"])
    else:
        for name in (
            "microsoft-edge-stable",
            "microsoft-edge-beta",
            "microsoft-edge-dev",
            "microsoft-edge",
            "msedge",
        ):
            _run_quiet(["killall", "-q", name])
            if shutil.which("pkill"):
                _run_quiet(["pkill", "-x", name])

    deadline = time.time() + 12
    while time.time() < deadline:
        if profile_dir is None or not _profile_locked(profile_dir):
            break
        time.sleep(0.25)
    if profile_dir is not None:
        _clear_profile_locks(profile_dir)


def _profile_locked(profile_dir: Path) -> bool:
    lock = profile_dir / "SingletonLock"
    return lock.exists() or lock.is_symlink()


def _clear_profile_locks(profile_dir: Path) -> None:
    for name in _PROFILE_LOCKS:
        path = profile_dir / name
        if not (path.exists() or path.is_symlink()):
            continue
        try:
            log.debug(f"removing stale profile lock {path}")
            path.unlink()
        except OSError as exc:
            log.warn(f"could not remove {path}: {exc}")


def find_edge_executable() -> str:
    candidates: list[str] = []
    if sys.platform == "win32":
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(
                    str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
                )
    elif sys.platform == "darwin":
        candidates.append(
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
        )
    else:
        candidates.extend(
            [
                "/usr/bin/microsoft-edge-stable",
                "/usr/bin/microsoft-edge",
                "/opt/microsoft/msedge/msedge",
            ]
        )
        for name in (
            "microsoft-edge-stable",
            "microsoft-edge",
            "msedge",
        ):
            found = shutil.which(name)
            if found:
                return found

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise BrowserError(
        "Microsoft Edge was not found. Install microsoft-edge-stable on Linux "
        "or Microsoft Edge on Windows/macOS."
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _wait_for_cdp(cdp_url: str, timeout_s: float = 30) -> None:
    version_url = cdp_url.rstrip("/") + "/json/version"
    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(version_url, timeout=1) as response:
                if 200 <= response.status < 300:
                    log.debug(f"Edge remote debugging is ready at {cdp_url}")
                    return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(0.2)
    raise BrowserError(
        f"Edge did not open remote debugging at {cdp_url}. "
        f"Last error: {last_error}"
    )


def _start_system_edge(*, headed: bool, start_url: str | None) -> tuple[str, bool]:
    """Start desktop Edge the same way a user would, plus CDP so we can drive it."""
    executable = find_edge_executable()
    port = _free_port()
    cdp_url = f"http://127.0.0.1:{port}"
    cmd = [
        executable,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--hide-crash-restore-bubble",
    ]
    if not headed:
        log.info(
            "system profile requested; opening a real Edge window instead of a "
            "headless scripted instance"
        )
    if start_url:
        cmd.append(start_url)
    log.info(
        "launching desktop Edge "
        + log.kv(executable=executable, cdp_url=cdp_url, start_url=start_url)
    )
    log.debug(f"Edge command: {cmd}")
    kwargs: dict[str, object] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(cmd, **kwargs)
    _wait_for_cdp(cdp_url)
    return cdp_url, True


def _attach_over_cdp(
    playwright,
    cdp_url: str,
    *,
    start_url: str | None,
    timeout_ms: int,
):
    log.info(f"attaching to Edge via CDP {cdp_url}")
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
    page = _page_for_cdp(context, start_url)
    attach_page_debug(page)
    if start_url:
        navigate(page, start_url, timeout_ms)
    log.info(f"using desktop Edge tab {describe_page(page)}")
    return context, page


def _page_url(page: Page) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def is_blank_url(url: str) -> bool:
    return url.strip().rstrip("/").lower() in _BLANK_URLS or url.strip().lower() in _BLANK_URLS


def _urls_match(left: str, right: str) -> bool:
    def parts(url: str) -> tuple[str, str, str]:
        parsed = urlsplit(url)
        return (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
        )

    try:
        return parts(left) == parts(right)
    except Exception:
        return left.rstrip("/") == right.rstrip("/")


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


def _wait_for_pages(context: BrowserContext) -> list[Page]:
    pages = [page for page in context.pages if not page.is_closed()]
    if pages:
        return pages
    log.debug("no tab yet after launch; waiting for Edge to create one")
    try:
        context.wait_for_event("page", timeout=15_000)
    except Exception as exc:
        log.debug(f"timed out waiting for first tab: {exc}")
    return [page for page in context.pages if not page.is_closed()]


def _adopt_page(
    context: BrowserContext,
    *,
    prefer_url: str | None = None,
    create_if_missing: bool = True,
) -> Page:
    """Drive the visible tab instead of opening a second about:blank window."""
    pages = _wait_for_pages(context)
    if prefer_url:
        for page in pages:
            if _urls_match(_page_url(page), prefer_url):
                log.debug(f"reusing tab already on target {describe_page(page)}")
                page.bring_to_front()
                return page
    usable = [
        page
        for page in pages
        if not is_blank_url(_page_url(page))
        and not _page_url(page).startswith(("devtools://", "chrome-extension://"))
    ]
    if usable:
        page = usable[0]
        log.debug(f"reusing non-blank tab {describe_page(page)}")
        page.bring_to_front()
        return page
    if pages:
        page = pages[0]
        log.debug(f"reusing open tab {describe_page(page)}")
        page.bring_to_front()
        return page
    if not create_if_missing:
        raise BrowserError("Edge has no open tab to automate.")
    log.debug("no open tab in context; creating a new page")
    page = context.new_page()
    page.bring_to_front()
    return page


def _page_for_cdp(context: BrowserContext, start_url: str | None) -> Page:
    if start_url:
        for page in context.pages:
            if page.is_closed():
                continue
            if _urls_match(_page_url(page), start_url):
                log.debug(f"reusing desktop tab already on target {describe_page(page)}")
                page.bring_to_front()
                return page
    pages = [page for page in context.pages if not page.is_closed()]
    if pages:
        page = pages[0]
        log.debug(f"reusing desktop tab {describe_page(page)}")
        page.bring_to_front()
        return page
    log.debug("opening new tab in desktop Edge")
    page = context.new_page()
    page.bring_to_front()
    return page


def _close_extra_blank_pages(context: BrowserContext, keep: Page) -> None:
    for page in list(context.pages):
        if page is keep or page.is_closed():
            continue
        if not is_blank_url(_page_url(page)):
            continue
        try:
            log.debug(f"closing extra blank tab {describe_page(page)}")
            page.close()
        except Exception as exc:
            log.debug(f"could not close extra blank tab: {exc}")


def navigate(page: Page, url: str, timeout_ms: int) -> None:
    """Navigate the visible tab off about:blank onto the chat UI."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    current = _page_url(page)
    if current and _urls_match(current, url):
        log.info(f"already on target {describe_page(page)}")
        return
    try:
        page.bring_to_front()
    except Exception as exc:
        log.debug(f"bring_to_front failed: {exc}")
    log.info(f"navigating to {url} (from {current or 'unknown'})")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        log.error(f"timed out loading chat UI: {url} ({describe_page(page)})")
        raise BrowserError(f"timed out loading chat UI: {url}") from exc
    loaded = _page_url(page)
    log.info(f"loaded {describe_page(page)}")
    if is_blank_url(loaded) and not is_blank_url(url):
        raise BrowserError(
            "Edge stayed on about:blank after navigation. Another Edge process "
            "is probably using this profile (Startup boost / background Edge). "
            "Fully quit Edge and retry."
        )


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
    start_url: str | None = None,
    timeout_ms: int = 180_000,
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
    started_desktop_edge = False
    profile_dir = Path(user_data_dir or ".edge-profile").expanduser()
    use_system_profile = _is_system_profile(str(profile_dir))
    try:
        log.info("starting Playwright")
        playwright = sync_playwright().start()
        if cdp_url:
            context, attached_page = _attach_over_cdp(
                playwright,
                cdp_url,
                start_url=start_url,
                timeout_ms=timeout_ms,
            )
            yield attached_page
            return

        close_existing_edge_sessions(profile_dir=profile_dir)

        if use_system_profile:
            if storage_state:
                log.warn(
                    "ignoring storage_state; system profile already has the desktop login"
                )
            cdp_url, started_desktop_edge = _start_system_edge(
                headed=headed,
                start_url=start_url,
            )
            context, attached_page = _attach_over_cdp(
                playwright,
                cdp_url,
                start_url=start_url,
                timeout_ms=timeout_ms,
            )
            yield attached_page
            return

        profile_dir.mkdir(parents=True, exist_ok=True)
        existing = any(profile_dir.iterdir())
        args = list(EDGE_LAUNCH_ARGS)
        if headed:
            args.append("--start-maximized")
        log.info(
            "launching persistent Edge "
            + log.kv(
                channel="msedge",
                headed=headed,
                headless=not headed,
                chromium_sandbox=False,
                user_data_dir=str(profile_dir),
                profile_has_data=existing,
                storage_state=storage_state,
                args=args,
            )
        )
        launch_kwargs: dict[str, object] = {
            "channel": "msedge",
            "headless": not headed,
            "chromium_sandbox": False,
            "args": args,
            "ignore_default_args": ["--enable-automation"],
        }
        if headed:
            launch_kwargs["no_viewport"] = True
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
        page = _adopt_page(context, prefer_url=start_url)
        attach_page_debug(page)
        if start_url:
            navigate(page, start_url, timeout_ms)
            _close_extra_blank_pages(context, page)
        log.info(f"Edge ready {describe_page(page)}")
        yield page
    finally:
        log.info("closing Playwright connection")
        if attached_page is not None and not started_desktop_edge:
            try:
                log.debug(f"closing attached tab {describe_page(attached_page)}")
                attached_page.close()
            except Exception as exc:
                log.debug(f"attached tab already closed: {exc}")
        elif context is not None and not started_desktop_edge:
            try:
                context.close()
            except Exception as exc:
                log.debug(f"context close: {exc}")
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as exc:
                log.debug(f"Playwright stop: {exc}")
        if started_desktop_edge:
            close_existing_edge_sessions(profile_dir=profile_dir)
        log.debug("Playwright stopped")
