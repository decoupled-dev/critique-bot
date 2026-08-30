from __future__ import annotations

import signal
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Callable

from critique_bot import log
from critique_bot.browser import BrowserError, connect_job_page, launch_edge
from critique_bot.chat_client import ChatError, submit_review
from critique_bot.config import BotConfig
from critique_bot.output import isoformat, save_failure, write_output
from critique_bot.queue import (
    HEARTBEAT_EVERY_SEC,
    POLL_SEC,
    FileQueue,
    Job,
    JobStatus,
    QueueError,
    RateLimiter,
)

BROWSER_RESTART_SEC = 5.0


def run_worker(config: BotConfig, *, headed: bool) -> int:
    queue = FileQueue(Path(config.queue_dir))
    try:
        lock = queue.acquire_worker_lock()
    except QueueError as exc:
        log.error(str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1

    stop = _install_stop_flag()
    limiter = RateLimiter(config.min_interval_seconds, config.interval_jitter_seconds)
    requeued = queue.requeue_stale_processing()
    queue.beat()
    beater = threading.Thread(
        target=_heartbeat_loop,
        args=(queue, stop),
        name="critique-bot-heartbeat",
        daemon=True,
    )
    beater.start()
    log.info(
        "worker started "
        + log.kv(
            queue_dir=str(queue.root),
            min_interval_seconds=config.min_interval_seconds,
            interval_jitter_seconds=config.interval_jitter_seconds,
            max_parallel_tabs=config.max_parallel_tabs,
            requeued=requeued or None,
            headed=headed,
            url=config.url,
            model=config.model or "(none)",
        )
    )
    print(
        f"critique-bot worker ready (queue {queue.root}, "
        f"{config.max_parallel_tabs} tab(s)). Ctrl-C to stop.",
        flush=True,
    )
    try:
        while not stop():
            try:
                cdp_out: dict[str, str] = {}
                with launch_edge(
                    headed=headed,
                    storage_state=config.storage_state,
                    user_data_dir=config.user_data_dir,
                    cdp_url=config.cdp_url,
                    start_url=config.url,
                    timeout_ms=config.timeout_ms,
                    cdp_out=cdp_out if config.max_parallel_tabs > 1 else None,
                ) as home:
                    _session_loop(
                        home.context,
                        config,
                        queue,
                        limiter,
                        stop,
                        cdp_url=cdp_out.get("url") or config.cdp_url,
                    )
            except BrowserError as exc:
                log.error(f"browser error: {exc}")
                if stop():
                    break
                log.info(f"restarting Edge in {BROWSER_RESTART_SEC:.0f}s")
                time.sleep(BROWSER_RESTART_SEC)
    finally:
        log.info("worker stopping")
        lock.close()
    return 0


def _heartbeat_loop(queue: FileQueue, stop: Callable[[], bool]) -> None:
    while not stop():
        try:
            queue.beat()
        except Exception as exc:
            log.debug(f"heartbeat write failed: {exc}")
        deadline = time.monotonic() + HEARTBEAT_EVERY_SEC
        while not stop() and time.monotonic() < deadline:
            time.sleep(0.25)


def _session_loop(
    context,
    config: BotConfig,
    queue: FileQueue,
    limiter: RateLimiter,
    stop: Callable[[], bool],
    *,
    cdp_url: str | None = None,
) -> None:
    parallel = max(int(config.max_parallel_tabs), 1)
    if parallel > 1 and not cdp_url:
        log.warn(
            f"max_parallel_tabs={parallel} needs Edge remote debugging; "
            "running one tab"
        )
        parallel = 1
    if parallel <= 1:
        _sequential_loop(context, config, queue, limiter, stop)
        return
    _parallel_loop(config, queue, limiter, stop, cdp_url=cdp_url, parallel=parallel)


def _sequential_loop(
    context,
    config: BotConfig,
    queue: FileQueue,
    limiter: RateLimiter,
    stop: Callable[[], bool],
) -> None:
    while not stop():
        job = queue.claim()
        if job is None:
            time.sleep(POLL_SEC)
            continue
        limiter.wait()
        _run_job_on_context(context, config, queue, job)


def _parallel_loop(
    config: BotConfig,
    queue: FileQueue,
    limiter: RateLimiter,
    stop: Callable[[], bool],
    *,
    cdp_url: str,
    parallel: int,
) -> None:
    log.info(f"running up to {parallel} review tabs in parallel")
    inflight: dict[Future[None], str] = {}
    browser_error: BrowserError | None = None

    def collect_done() -> None:
        nonlocal browser_error
        for future in [item for item in inflight if item.done()]:
            job_id = inflight.pop(future)
            try:
                future.result()
            except BrowserError as exc:
                log.error(f"job {job_id} browser error: {exc}")
                browser_error = exc
            except Exception as exc:
                log.exception(f"job {job_id} thread failed: {exc}")

    with ThreadPoolExecutor(
        max_workers=parallel,
        thread_name_prefix="critique-tab",
    ) as pool:
        try:
            while not stop():
                collect_done()
                if browser_error is not None:
                    if inflight:
                        wait(inflight, timeout=POLL_SEC, return_when=FIRST_COMPLETED)
                        continue
                    raise browser_error
                if len(inflight) >= parallel:
                    wait(inflight, timeout=POLL_SEC, return_when=FIRST_COMPLETED)
                    continue
                job = queue.claim()
                if job is None:
                    if inflight:
                        wait(inflight, timeout=POLL_SEC, return_when=FIRST_COMPLETED)
                    else:
                        time.sleep(POLL_SEC)
                    continue
                limiter.wait()
                future = pool.submit(_run_job_on_cdp, cdp_url, config, queue, job)
                inflight[future] = job.id
        finally:
            if inflight:
                log.info(f"waiting for {len(inflight)} in-flight review(s)")
                wait(inflight, timeout=max(config.timeout_ms / 1000.0, 30.0))
                collect_done()


def _run_job_on_context(context, config: BotConfig, queue: FileQueue, job: Job) -> None:
    page = None
    try:
        page = context.new_page()
        _execute_job(page, config, queue, job)
    finally:
        _close_page(page)


def _run_job_on_cdp(
    cdp_url: str, config: BotConfig, queue: FileQueue, job: Job
) -> None:
    try:
        with connect_job_page(cdp_url) as page:
            _execute_job(page, config, queue, job)
    except BrowserError as exc:
        if queue.read_status(job.id) is None:
            queue.fail(
                job.id,
                str(exc),
                stem=job.stem,
                label=job.label,
                meta=job.meta,
            )
        raise


def _close_page(page) -> None:
    if page is None:
        return
    try:
        page.close()
    except Exception as exc:
        log.debug(f"tab close: {exc}")


def _execute_job(page, config: BotConfig, queue: FileQueue, job: Job) -> None:
    from dataclasses import replace

    started = datetime.now(timezone.utc)
    out_dir = queue.result_dir(job.id)
    queue.write_job_record(job)
    job_config = config
    if job.model:
        job_config = replace(config, model=job.model)
    log.info(
        f"running job {job.id} "
        + log.kv(
            label=job.label or None,
            mode=job.mode,
            prompt_chars=len(job.prompt),
            model=job_config.model or "(none)",
            meta=job.meta or None,
        )
    )
    try:
        response = submit_review(page, job_config, job.prompt)
    except BrowserError as exc:
        save_failure(page, out_dir)
        log.error(f"job {job.id} failed: {exc}")
        queue.fail(
            job.id, str(exc), stem=job.stem, label=job.label, meta=job.meta
        )
        raise
    except ChatError as exc:
        save_failure(page, out_dir)
        log.error(f"job {job.id} failed: {exc}")
        queue.fail(
            job.id, str(exc), stem=job.stem, label=job.label, meta=job.meta
        )
        return
    except Exception as exc:
        try:
            save_failure(page, out_dir)
        except Exception:
            pass
        log.exception(f"job {job.id} unexpected failure: {exc}")
        queue.fail(
            job.id,
            f"unexpected failure: {exc}",
            stem=job.stem,
            label=job.label,
            meta=job.meta,
        )
        return

    finished = datetime.now(timezone.utc)
    elapsed = (finished - started).total_seconds()
    if not response.strip():
        queue.fail(
            job.id,
            "assistant returned an empty review",
            stem=job.stem,
            label=job.label,
            meta=job.meta,
        )
        return
    payload = {
        "mode": job.mode,
        "model": job_config.model,
        "url": config.url,
        "prompt_chars": len(job.prompt),
        "response": response,
        "started_at": isoformat(started),
        "finished_at": isoformat(finished),
        "job_id": job.id,
        "label": job.label,
        "meta": job.meta,
    }
    write_output(out_dir, response, payload, stem=job.stem, print_body=False)
    queue.finish(
        job.id,
        JobStatus(
            id=job.id,
            ok=True,
            error=None,
            stem=job.stem,
            started_at=isoformat(started),
            finished_at=isoformat(finished),
            elapsed_seconds=elapsed,
            label=job.label,
            meta=job.meta,
        ),
    )
    log.info(
        f"job {job.id} finished in {elapsed:.1f}s ({len(response)} chars)"
        + (f" label={job.label}" if job.label else "")
    )


def _install_stop_flag() -> Callable[[], bool]:
    stopped = False

    def handle(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stopped
        stopped = True
        log.info("stop signal received")

    signal.signal(signal.SIGINT, handle)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle)
    return lambda: stopped
