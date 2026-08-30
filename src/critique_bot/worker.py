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
from critique_bot.browser import BrowserError
from critique_bot.config import BotConfig
from critique_bot.llm import LLMError, LLMProvider, open_provider
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
            backend=config.backend,
            min_interval_seconds=config.min_interval_seconds,
            interval_jitter_seconds=config.interval_jitter_seconds,
            max_parallel_tabs=config.max_parallel_tabs,
            requeued=requeued or None,
            headed=headed if config.uses_browser else None,
            url=config.url or None,
            base_url=config.base_url or None,
            model=config.model or "(none)",
        )
    )
    print(
        f"critique-bot worker ready ({config.backend}, queue {queue.root}, "
        f"{config.max_parallel_tabs} job(s)). Ctrl-C to stop.",
        flush=True,
    )
    try:
        if config.uses_browser:
            _browser_provider_loop(config, queue, limiter, stop, headed=headed)
        else:
            with open_provider(config, headed=headed) as provider:
                _session_loop(provider, config, queue, limiter, stop)
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


def _browser_provider_loop(
    config: BotConfig,
    queue: FileQueue,
    limiter: RateLimiter,
    stop: Callable[[], bool],
    *,
    headed: bool,
) -> None:
    while not stop():
        try:
            with open_provider(config, headed=headed) as provider:
                _session_loop(provider, config, queue, limiter, stop)
        except BrowserError as exc:
            log.error(f"browser error: {exc}")
            if stop():
                break
            log.info(f"restarting Edge in {BROWSER_RESTART_SEC:.0f}s")
            time.sleep(BROWSER_RESTART_SEC)


def _session_loop(
    provider: LLMProvider,
    config: BotConfig,
    queue: FileQueue,
    limiter: RateLimiter,
    stop: Callable[[], bool],
) -> None:
    parallel = max(int(config.max_parallel_tabs), 1)
    if parallel > 1 and not provider.can_parallelize:
        if config.uses_browser:
            log.warn(
                f"max_parallel_tabs={parallel} needs Edge remote debugging; "
                "running one tab"
            )
        parallel = 1
    if parallel <= 1:
        _sequential_loop(provider, config, queue, limiter, stop)
        return
    _parallel_loop(provider, config, queue, limiter, stop, parallel=parallel)


def _sequential_loop(
    provider: LLMProvider,
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
        _run_job(provider, config, queue, job, isolated=False)


def _parallel_loop(
    provider: LLMProvider,
    config: BotConfig,
    queue: FileQueue,
    limiter: RateLimiter,
    stop: Callable[[], bool],
    *,
    parallel: int,
) -> None:
    log.info(f"running up to {parallel} reviews in parallel")
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
        thread_name_prefix="critique-job",
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
                future = pool.submit(
                    _run_job, provider, config, queue, job, True
                )
                inflight[future] = job.id
        finally:
            if inflight:
                log.info(f"waiting for {len(inflight)} in-flight review(s)")
                wait(inflight, timeout=max(config.timeout_ms / 1000.0, 30.0))
                collect_done()


def _run_job(
    provider: LLMProvider,
    config: BotConfig,
    queue: FileQueue,
    job: Job,
    isolated: bool = False,
) -> None:
    try:
        _execute_job(provider, config, queue, job, isolated=isolated)
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


def _save_job_failure(session, out_dir: Path) -> None:
    page = getattr(session, "page", None) if session is not None else None
    if page is None:
        return
    try:
        save_failure(page, out_dir)
    except Exception:
        pass


def _execute_job(
    provider: LLMProvider,
    config: BotConfig,
    queue: FileQueue,
    job: Job,
    *,
    isolated: bool,
) -> None:
    started = datetime.now(timezone.utc)
    out_dir = queue.result_dir(job.id)
    queue.write_job_record(job)
    model = job.model or config.model
    log.info(
        f"running job {job.id} "
        + log.kv(
            label=job.label or None,
            mode=job.mode,
            backend=config.backend,
            prompt_chars=len(job.prompt),
            model=model or "(none)",
            meta=job.meta or None,
        )
    )
    response = ""
    try:
        with provider.session(isolated=isolated, model=job.model) as session:
            try:
                response = session.send(job.prompt)
            except Exception:
                _save_job_failure(session, out_dir)
                raise
    except BrowserError as exc:
        log.error(f"job {job.id} failed: {exc}")
        queue.fail(
            job.id, str(exc), stem=job.stem, label=job.label, meta=job.meta
        )
        raise
    except LLMError as exc:
        log.error(f"job {job.id} failed: {exc}")
        queue.fail(
            job.id, str(exc), stem=job.stem, label=job.label, meta=job.meta
        )
        return
    except Exception as exc:
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
        "model": model,
        "backend": config.backend,
        "url": config.url or config.base_url,
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
