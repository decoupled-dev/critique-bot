from __future__ import annotations

import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Callable

from critique_bot import log
from critique_bot.browser import BrowserError, launch_edge
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
            requeued=requeued or None,
            headed=headed,
            url=config.url,
            model=config.model or "(none)",
        )
    )
    print(
        f"critique-bot worker ready (queue {queue.root}). Ctrl-C to stop.",
        flush=True,
    )
    try:
        while not stop():
            try:
                with launch_edge(
                    headed=headed,
                    storage_state=config.storage_state,
                    user_data_dir=config.user_data_dir,
                    cdp_url=config.cdp_url,
                    start_url=config.url,
                    timeout_ms=config.timeout_ms,
                ) as home:
                    _session_loop(home.context, config, queue, limiter, stop)
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
) -> None:
    while not stop():
        job = queue.claim()
        if job is None:
            time.sleep(POLL_SEC)
            continue
        limiter.wait()
        _run_job(context, config, queue, job)


def _run_job(context, config: BotConfig, queue: FileQueue, job: Job) -> None:
    from dataclasses import replace

    started = datetime.now(timezone.utc)
    out_dir = queue.result_dir(job.id)
    job_config = config
    if job.model:
        job_config = replace(config, model=job.model)
    log.info(
        f"running job {job.id} "
        + log.kv(
            mode=job.mode,
            prompt_chars=len(job.prompt),
            model=job_config.model or "(none)",
            meta=job.meta or None,
        )
    )
    page = None
    try:
        page = context.new_page()
        response = submit_review(page, job_config, job.prompt)
    except BrowserError as exc:
        if page is not None:
            save_failure(page, out_dir)
        log.error(f"job {job.id} failed: {exc}")
        queue.fail(job.id, str(exc), stem=job.stem)
        raise
    except ChatError as exc:
        if page is not None:
            save_failure(page, out_dir)
        log.error(f"job {job.id} failed: {exc}")
        queue.fail(job.id, str(exc), stem=job.stem)
        return
    except Exception as exc:
        if page is not None:
            try:
                save_failure(page, out_dir)
            except Exception:
                pass
        log.exception(f"job {job.id} unexpected failure: {exc}")
        queue.fail(job.id, f"unexpected failure: {exc}", stem=job.stem)
        return
    finally:
        if page is not None:
            try:
                page.close()
            except Exception as exc:
                log.debug(f"tab close: {exc}")

    finished = datetime.now(timezone.utc)
    elapsed = (finished - started).total_seconds()
    if not response.strip():
        queue.fail(job.id, "assistant returned an empty review", stem=job.stem)
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
        ),
    )
    log.info(f"job {job.id} finished in {elapsed:.1f}s ({len(response)} chars)")


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
