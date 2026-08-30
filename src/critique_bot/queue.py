from __future__ import annotations

import json
import os
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from critique_bot import log
from critique_bot.output import isoformat

HEARTBEAT_NAME = "worker.heartbeat"
LOCK_NAME = "worker.lock"
HEARTBEAT_EVERY_SEC = 5.0
HEARTBEAT_STALE_SEC = 20.0
POLL_SEC = 0.5


class QueueError(RuntimeError):
    """The on-disk job queue could not be used."""


@dataclass
class Job:
    id: str
    mode: str
    stem: str
    prompt: str
    model: str | None
    created_at: str
    label: str
    meta: dict[str, Any]


@dataclass
class JobStatus:
    id: str
    ok: bool
    error: str | None
    stem: str
    started_at: str | None
    finished_at: str | None
    elapsed_seconds: float | None
    label: str = ""
    meta: dict[str, Any] | None = None


class RateLimiter:
    """Space out sends so the chat site is less likely to treat the worker as spam."""

    def __init__(self, min_interval: float, jitter: float) -> None:
        self.min_interval = max(float(min_interval), 0.0)
        self.jitter = max(float(jitter), 0.0)
        self._last: float | None = None

    def wait(self) -> float:
        if self._last is None or self.min_interval <= 0:
            self._last = time.monotonic()
            return 0.0
        extra = random.uniform(0.0, self.jitter) if self.jitter else 0.0
        delay = (self._last + self.min_interval + extra) - time.monotonic()
        if delay > 0:
            log.info(f"rate limit: waiting {delay:.1f}s before the next review")
            time.sleep(delay)
        self._last = time.monotonic()
        return max(delay, 0.0)


class WorkerLock:
    """One worker process per queue directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0, os.SEEK_END)
                if self._fh.tell() == 0:
                    self._fh.write(b"0")
                    self._fh.flush()
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise QueueError(
                "another critique-bot worker is already running for this queue"
            ) from exc

    def close(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            self._fh.close()
        finally:
            self._fh = None


class FileQueue:
    """FIFO job inbox on disk. GitLab jobs enqueue; the worker claims."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.inbox = root / "inbox"
        self.processing = root / "processing"
        self.results = root / "results"
        self.heartbeat_path = root / HEARTBEAT_NAME
        self.lock_path = root / LOCK_NAME
        for path in (self.inbox, self.processing, self.results):
            path.mkdir(parents=True, exist_ok=True)

    def acquire_worker_lock(self) -> WorkerLock:
        lock = WorkerLock(self.lock_path)
        lock.acquire()
        return lock

    def beat(self, *, pid: int | None = None) -> None:
        payload = {
            "pid": pid if pid is not None else os.getpid(),
            "updated_at": isoformat(datetime.now(timezone.utc)),
            "monotonic": time.monotonic(),
        }
        _atomic_write_json(self.heartbeat_path, payload)

    def worker_alive(self, *, stale_sec: float = HEARTBEAT_STALE_SEC) -> bool:
        if not self.heartbeat_path.is_file():
            return False
        try:
            age = time.time() - self.heartbeat_path.stat().st_mtime
        except OSError:
            return False
        return age <= stale_sec

    def worker_hint(self) -> str:
        if not self.heartbeat_path.is_file():
            return "no worker heartbeat file"
        try:
            data = json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
            updated = data.get("updated_at") or "unknown"
            pid = data.get("pid") or "?"
        except (OSError, json.JSONDecodeError):
            updated = "unreadable"
            pid = "?"
        try:
            age = time.time() - self.heartbeat_path.stat().st_mtime
            return f"pid={pid} last_beat={updated} ({age:.0f}s ago)"
        except OSError:
            return f"pid={pid} last_beat={updated}"

    def requeue_stale_processing(self) -> int:
        moved = 0
        for path in sorted(self.processing.glob("*.json")):
            dest = self.inbox / path.name
            try:
                path.replace(dest)
                moved += 1
                log.warn(f"requeued interrupted job {path.stem}")
            except OSError as exc:
                log.warn(f"could not requeue {path.name}: {exc}")
        return moved

    def requeue_job(self, job_id: str) -> bool:
        """Move a claimed job back to inbox so it can run after Edge restarts."""
        src = self.processing / f"{job_id}.json"
        dest = self.inbox / f"{job_id}.json"
        if dest.is_file() and not src.is_file():
            return True
        if not src.is_file():
            return False
        try:
            src.replace(dest)
        except OSError as exc:
            log.warn(f"could not requeue {job_id}: {exc}")
            return False
        log.warn(f"requeued job {job_id} after a recoverable browser error")
        return True

    def enqueue(
        self,
        *,
        mode: str,
        stem: str,
        prompt: str,
        model: str | None = None,
        meta: dict[str, Any] | None = None,
        label: str | None = None,
    ) -> str:
        meta = dict(meta or {})
        slug = job_label(meta, explicit=label)
        job_id = _new_job_id(slug)
        payload = {
            "id": job_id,
            "mode": mode,
            "stem": stem,
            "prompt": prompt,
            "model": model or "",
            "created_at": isoformat(datetime.now(timezone.utc)),
            "label": slug,
            "meta": meta,
        }
        dest = self.inbox / f"{job_id}.json"
        _atomic_write_json(dest, payload)
        log.info(
            f"enqueued job {job_id} ({len(prompt)} chars, mode={mode}, label={slug})"
        )
        return job_id

    def claim(self) -> Job | None:
        for path in sorted(self.inbox.glob("*.json")):
            dest = self.processing / path.name
            try:
                path.replace(dest)
            except FileNotFoundError:
                continue
            except OSError as exc:
                log.warn(f"could not claim {path.name}: {exc}")
                continue
            try:
                return _job_from_payload(_read_json(dest), dest.stem)
            except QueueError as exc:
                log.error(f"invalid job file {dest.name}: {exc}")
                self.fail(dest.stem, str(exc), stem="review", label=dest.stem)
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
        return None

    def result_dir(self, job_id: str) -> Path:
        path = self.results / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_job_record(self, job: Job) -> None:
        _atomic_write_json(
            self.result_dir(job.id) / "job.json",
            {
                "id": job.id,
                "label": job.label,
                "mode": job.mode,
                "stem": job.stem,
                "model": job.model or "",
                "created_at": job.created_at,
                "meta": job.meta,
                "prompt_chars": len(job.prompt),
            },
        )

    def finish(self, job_id: str, status: JobStatus) -> None:
        payload = {
            "id": status.id,
            "ok": status.ok,
            "error": status.error,
            "stem": status.stem,
            "started_at": status.started_at,
            "finished_at": status.finished_at,
            "elapsed_seconds": status.elapsed_seconds,
            "label": status.label or "",
            "meta": status.meta or {},
        }
        _atomic_write_json(self.result_dir(job_id) / "status.json", payload)
        processing = self.processing / f"{job_id}.json"
        try:
            processing.unlink(missing_ok=True)
        except OSError:
            pass

    def fail(
        self,
        job_id: str,
        error: str,
        *,
        stem: str,
        label: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        now = isoformat(datetime.now(timezone.utc))
        self.finish(
            job_id,
            JobStatus(
                id=job_id,
                ok=False,
                error=error,
                stem=stem,
                started_at=None,
                finished_at=now,
                elapsed_seconds=None,
                label=label,
                meta=meta,
            ),
        )

    def read_status(self, job_id: str) -> JobStatus | None:
        path = self.results / job_id / "status.json"
        if not path.is_file():
            return None
        data = _read_json(path)
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        return JobStatus(
            id=str(data.get("id") or job_id),
            ok=bool(data.get("ok")),
            error=str(data["error"]) if data.get("error") else None,
            stem=str(data.get("stem") or "review"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            elapsed_seconds=data.get("elapsed_seconds"),
            label=str(data.get("label") or ""),
            meta=meta,
        )

    def wait(
        self,
        job_id: str,
        *,
        timeout_sec: float,
        poll_sec: float = POLL_SEC,
    ) -> JobStatus:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            status = self.read_status(job_id)
            if status is not None:
                return status
            if not self.worker_alive():
                status = self.read_status(job_id)
                if status is not None:
                    return status
                raise QueueError(
                    "critique-bot worker is not running "
                    f"({self.worker_hint()}). On the runner start: "
                    "critique-bot worker --config CONFIG --logs"
                )
            time.sleep(poll_sec)
        raise QueueError(
            f"timed out after {int(timeout_sec)}s waiting for job {job_id}"
        )


def job_label(meta: dict[str, Any] | None = None, *, explicit: str | None = None) -> str:
    """Human-readable slug for a queue job: MR/PR id, CI job, or ``local``."""
    if explicit and str(explicit).strip():
        return _safe_slug(str(explicit).strip(), 48)
    meta = meta or {}
    mr = str(meta.get("CI_MERGE_REQUEST_IID") or "").strip()
    if mr:
        project = _safe_slug(
            str(meta.get("CI_PROJECT_PATH") or "").replace("/", "-"), 32
        )
        return f"{project}-mr{mr}" if project else f"mr{mr}"
    pr = str(meta.get("GITHUB_PR_NUMBER") or "").strip() or _pr_from_github_ref(
        str(meta.get("GITHUB_REF") or "")
    )
    if pr:
        repo = _safe_slug(
            str(meta.get("GITHUB_REPOSITORY") or "").replace("/", "-"), 32
        )
        return f"{repo}-pr{pr}" if repo else f"pr{pr}"
    run = str(meta.get("CI_JOB_ID") or meta.get("GITHUB_RUN_ID") or "").strip()
    if run:
        return f"ci{run}"
    return "local"


def _pr_from_github_ref(ref: str) -> str:
    parts = [part for part in ref.strip("/").split("/") if part]
    if len(parts) >= 3 and parts[0] == "refs" and parts[1] == "pull" and parts[2].isdigit():
        return parts[2]
    return ""


def _safe_slug(text: str, max_len: int) -> str:
    if not text or not text.strip():
        return ""
    cleaned: list[str] = []
    for ch in text:
        if ch.isalnum() or ch in "-_":
            cleaned.append(ch)
        else:
            cleaned.append("-")
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug[:max_len].strip("-")
    return slug or "job"


def _new_job_id(label: str | None = None) -> str:
    stamp = int(time.time() * 1000)
    slug = _safe_slug(label or "job", 48) or "job"
    return f"{stamp:013d}-{slug}-{uuid.uuid4().hex[:8]}"


def _job_from_payload(data: dict[str, Any], fallback_id: str) -> Job:
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise QueueError("job prompt is empty")
    mode = str(data.get("mode") or "review")
    stem = str(data.get("stem") or ("reply" if mode == "general" else "review"))
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    job_id = str(data.get("id") or fallback_id)
    label = str(data.get("label") or "").strip() or job_label(meta)
    return Job(
        id=job_id,
        mode=mode,
        stem=stem,
        prompt=prompt,
        model=str(data["model"]) if data.get("model") else None,
        created_at=str(data.get("created_at") or ""),
        label=label,
        meta=meta,
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QueueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise QueueError(f"{path} must be a JSON object")
    return raw


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
