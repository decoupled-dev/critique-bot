from __future__ import annotations

import json
import os
import random
import re
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from critique_bot import env_names, log
from critique_bot.output import private_dir, isoformat

HEARTBEAT_NAME = "worker.heartbeat"
LOCK_NAME = "worker.lock"
HEARTBEAT_EVERY_SEC = 5.0
HEARTBEAT_STALE_SEC = 20.0
POLL_SEC = 0.5
#: How many times one job may be handed back to the queue after a recoverable
#: browser failure before it is failed for good. Without a ceiling, a browser
#: that is permanently broken (expired login, say) requeues forever.
DEFAULT_MAX_ATTEMPTS = 3
#: Completed job folders kept under results/ before the oldest are removed.
DEFAULT_RESULT_RETENTION = 200
#: Job ids and output stems become folder and file names under results/, and the
#: job file in inbox/ is written by the CI user, so both are checked against this.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


#: Set to 1 on the worker (see packaging/crit-worker.service) when the worker
#: runs as its own user and CI jobs reach the queue through a shared group. A queue
#: folder created with the setgid bit is treated the same way.
ENV_QUEUE_SHARED = "CRIT_QUEUE_SHARED"
#: Shared-group modes (POSIX). CI may write job files into inbox/ and read results/;
#: processing/ and failure snapshots stay with the worker user only.
SHARED_DIR_MODES = {
    "root": 0o2750,
    "inbox": 0o2770,
    "processing": 0o700,
    "results": 0o2750,
    "result": 0o2750,
}
SHARED_FILE_MODE = 0o640


def _shared_requested(root: Path) -> bool:
    if os.name == "nt":
        return False
    if (env_names.get(ENV_QUEUE_SHARED) or "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    try:
        return bool(root.stat().st_mode & stat.S_ISGID)
    except OSError:
        return False


def _shared_dir(path: Path, mode: int) -> Path:
    """mkdir, then set ``mode`` only on a folder this user owns (the worker's)."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_uid == os.geteuid():
            os.chmod(path, mode)
    except OSError as exc:
        log.warn(f"could not set permissions on {path}: {exc}")
    return path


def valid_name(value: object) -> bool:
    """True for a safe job id / output stem: no separators, no ``..``, ≤128 chars."""
    return isinstance(value, str) and bool(_NAME_RE.match(value)) and ".." not in value


def _file_job_id(stem: str) -> str:
    """A job id for a queue file, even one whose name is unsafe (to record its failure)."""
    if valid_name(stem):
        return stem
    return f"invalid-{_safe_slug(stem, 40)}-{uuid.uuid4().hex[:8]}"


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
    attempts: int = 0
    files: dict[str, str] = field(default_factory=dict)
    patch: str = ""


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
                "another crit worker is already running for this queue"
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

    def __init__(self, root: Path, *, shared: bool | None = None) -> None:
        self.root = root
        self.inbox = root / "inbox"
        self.processing = root / "processing"
        self.results = root / "results"
        self.heartbeat_path = root / HEARTBEAT_NAME
        self.lock_path = root / LOCK_NAME
        # Jobs hold whole prompts (code) and failure snapshots of the chat page.
        self.shared = _shared_requested(root) if shared is None else bool(shared)
        #: Mode for files the CI user must read (status, review); None keeps the umask.
        self.file_mode: int | None = SHARED_FILE_MODE if self.shared else None
        if self.shared:
            _shared_dir(root, SHARED_DIR_MODES["root"])
            _shared_dir(self.inbox, SHARED_DIR_MODES["inbox"])
            _shared_dir(self.processing, SHARED_DIR_MODES["processing"])
            _shared_dir(self.results, SHARED_DIR_MODES["results"])
        else:
            # Windows: the queue keeps the ACL the administrator set (worker-start.ps1).
            private_dir(root, windows_acl=False)
            for path in (self.inbox, self.processing, self.results):
                private_dir(path, windows_acl=False)

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
        _atomic_write_json(self.heartbeat_path, payload, mode=self.file_mode)

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

    def requeue_stale_processing(
        self, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS
    ) -> int:
        """Recover jobs a crashed worker left claimed."""
        moved = 0
        for path in sorted(self.processing.glob("*.json")):
            if self._requeue_path(
                path,
                max_attempts=max_attempts,
                reason="the worker stopped while it was running",
            ):
                moved += 1
                log.warn(f"requeued interrupted job {path.stem}")
        return moved

    def requeue_job(
        self,
        job_id: str,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        reason: str = "a recoverable browser error",
    ) -> bool:
        """Move a claimed job back to inbox so it can run after Edge restarts.

        Returns False when the job ran out of attempts; it is failed instead, so
        a permanently broken browser cannot spin the queue forever.
        """
        if not valid_name(job_id):
            return False
        src = self.processing / f"{job_id}.json"
        dest = self.inbox / f"{job_id}.json"
        if dest.is_file() and not src.is_file():
            return True
        if not src.is_file():
            return False
        return self._requeue_path(src, max_attempts=max_attempts, reason=reason)

    def _requeue_path(self, src: Path, *, max_attempts: int, reason: str) -> bool:
        job_id = _file_job_id(src.stem)
        if job_id != src.stem:
            log.error(f"unsafe job file name {src.name!r}; failing it as {job_id}")
            self.fail(job_id, "unsafe job file name", stem="review")
            src.unlink(missing_ok=True)
            return False
        try:
            payload = _read_json(src)
        except QueueError as exc:
            log.error(f"cannot requeue unreadable job {job_id}: {exc}")
            self.fail(job_id, f"unreadable job file: {exc}", stem="review")
            src.unlink(missing_ok=True)
            return False
        attempts = _attempt_count(payload) + 1
        if attempts >= max(int(max_attempts), 1):
            log.error(
                f"job {job_id} failed {attempts} time(s) ({reason}); giving up"
            )
            self.fail(
                job_id,
                f"gave up after {attempts} attempt(s): {reason}",
                stem=_safe_stem(payload.get("stem")),
                label=str(payload.get("label") or ""),
                meta=payload.get("meta") if isinstance(payload.get("meta"), dict) else None,
            )
            return False
        payload["attempts"] = attempts
        dest = self.inbox / f"{job_id}.json"
        try:
            _atomic_write_json(dest, payload)
            src.unlink(missing_ok=True)
        except OSError as exc:
            log.warn(f"could not requeue {job_id}: {exc}")
            return False
        log.warn(
            f"requeued job {job_id} after {reason} (attempt {attempts}/{max_attempts})"
        )
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
        files: dict[str, str] | None = None,
        patch: str | None = None,
    ) -> str:
        if stem and not valid_name(stem):
            raise QueueError(f"output stem {stem!r} is not a plain file name")
        meta = dict(meta or {})
        file_map = _job_files({"files": files} if files else {})
        patch_text = patch if isinstance(patch, str) and patch.strip() else ""
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
            "attempts": 0,
        }
        if file_map:
            payload["files"] = file_map
        if patch_text:
            payload["patch"] = patch_text
        dest = self.inbox / f"{job_id}.json"
        # The worker (another user when shared) must be able to read the job.
        _atomic_write_json(dest, payload, mode=self.file_mode)
        log.info(
            f"enqueued job {job_id} ({len(prompt)} chars, mode={mode}, label={slug}"
            + (f", {len(file_map)} staged file(s)" if file_map else "")
            + (f", patch {len(patch_text)} chars" if patch_text else "")
            + ")"
        )
        return job_id

    def has_waiting(self) -> bool:
        """True when the inbox has at least one job file."""
        try:
            return next(self.inbox.glob("*.json"), None) is not None
        except OSError:
            return False

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
                log.error(f"invalid job file {dest.name!r}: {exc}")
                failed_id = _file_job_id(dest.stem)
                self.fail(failed_id, str(exc), stem="review", label=failed_id)
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
        return None

    def result_dir(self, job_id: str) -> Path:
        if not valid_name(job_id):
            raise QueueError(f"invalid job id {job_id!r}")
        base = self.results.resolve()
        target = (base / job_id).resolve()
        if target.parent != base:
            raise QueueError(f"job id {job_id!r} leaves {base}")
        if self.shared:
            return _shared_dir(target, SHARED_DIR_MODES["result"])
        return private_dir(target, windows_acl=False)

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
                "staged_files": len(job.files) or None,
                "patch_chars": len(job.patch) or None,
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
        _atomic_write_json(
            self.result_dir(job_id) / "status.json", payload, mode=self.file_mode
        )
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
        if not valid_name(job_id):
            return None
        path = self.results / job_id / "status.json"
        if not path.is_file():
            return None
        data = _read_json(path)
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        return JobStatus(
            id=str(data.get("id") or job_id),
            ok=bool(data.get("ok")),
            error=str(data["error"]) if data.get("error") else None,
            stem=_safe_stem(data.get("stem")),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            elapsed_seconds=data.get("elapsed_seconds"),
            label=str(data.get("label") or ""),
            meta=meta,
        )

    def prune_results(self, *, keep: int = DEFAULT_RESULT_RETENTION) -> int:
        """Drop the oldest finished job folders so results/ stays bounded."""
        keep = max(int(keep), 1)
        try:
            entries = [item for item in self.results.iterdir() if item.is_dir()]
        except OSError:
            return 0
        if len(entries) <= keep:
            return 0
        # Job ids start with a millisecond timestamp, so name order is age order.
        entries.sort(key=lambda item: item.name)
        removed = 0
        for item in entries[: len(entries) - keep]:
            try:
                shutil.rmtree(item)
                removed += 1
            except OSError as exc:
                log.debug(f"could not remove old result {item.name}: {exc}")
        if removed:
            log.info(f"pruned {removed} old result folder(s), keeping {keep}")
        return removed

    def snapshot(self, *, recent: int = 10) -> dict[str, Any]:
        """Everything an operator or the setup UI needs to see queue state."""
        return {
            "queue_dir": str(self.root),
            "worker_alive": self.worker_alive(),
            "worker_hint": self.worker_hint(),
            "waiting": self._briefs(self.inbox),
            "processing": self._briefs(self.processing),
            "recent": self.recent_results(limit=max(int(recent), 0)),
        }

    def _briefs(self, folder: Path) -> list[dict[str, Any]]:
        briefs: list[dict[str, Any]] = []
        try:
            paths = sorted(folder.glob("*.json"))
        except OSError:
            return briefs
        for path in paths:
            brief: dict[str, Any] = {"id": path.stem}
            try:
                data = _read_json(path)
            except QueueError:
                brief["error"] = "unreadable job file"
                briefs.append(brief)
                continue
            brief.update(
                {
                    "label": str(data.get("label") or ""),
                    "mode": str(data.get("mode") or ""),
                    "created_at": str(data.get("created_at") or ""),
                    "attempts": _attempt_count(data),
                    "prompt_chars": len(str(data.get("prompt") or "")),
                }
            )
            try:
                brief["age_seconds"] = round(time.time() - path.stat().st_mtime, 1)
            except OSError:
                pass
            briefs.append(brief)
        return briefs

    def recent_results(self, *, limit: int = 10) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        try:
            folders = [item for item in self.results.iterdir() if item.is_dir()]
        except OSError:
            return []
        folders.sort(key=lambda item: item.name, reverse=True)
        out: list[dict[str, Any]] = []
        for folder in folders[:limit]:
            status = self.read_status(folder.name)
            if status is None:
                out.append({"id": folder.name, "state": "running or interrupted"})
                continue
            out.append(
                {
                    "id": status.id,
                    "label": status.label,
                    "ok": status.ok,
                    "error": status.error,
                    "stem": status.stem,
                    "finished_at": status.finished_at,
                    "elapsed_seconds": status.elapsed_seconds,
                }
            )
        return out

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
                    "crit worker is not running "
                    f"({self.worker_hint()}). On the runner start: "
                    "crit worker --config CONFIG --logs"
                )
            time.sleep(poll_sec)
        raise QueueError(
            f"timed out after {int(timeout_sec)}s waiting for job {job_id}"
        )


def job_label(meta: dict[str, Any] | None = None, *, explicit: str | None = None) -> str:
    """Human-readable slug for a queue job: GitLab MR id, CI job, or ``local``."""
    if explicit and str(explicit).strip():
        return _safe_slug(str(explicit).strip(), 48)
    meta = meta or {}
    mr = str(meta.get("CI_MERGE_REQUEST_IID") or "").strip()
    if mr:
        project = _safe_slug(
            str(meta.get("CI_PROJECT_PATH") or "").replace("/", "-"), 32
        )
        return f"{project}-mr{mr}" if project else f"mr{mr}"
    run = str(meta.get("CI_JOB_ID") or "").strip()
    if run:
        return f"ci{run}"
    return "local"


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


_stamp_lock = threading.Lock()
_last_stamp = 0


def _next_stamp() -> int:
    """Strictly increasing millisecond stamp.

    Job ids sort lexicographically to give the worker FIFO order. Two jobs
    enqueued in the same millisecond would otherwise be ordered by their random
    suffix, so the stamp is nudged forward instead.
    """
    global _last_stamp
    with _stamp_lock:
        stamp = int(time.time() * 1000)
        if stamp <= _last_stamp:
            stamp = _last_stamp + 1
        _last_stamp = stamp
        return stamp


def _new_job_id(label: str | None = None) -> str:
    slug = _safe_slug(label or "job", 48) or "job"
    return f"{_next_stamp():013d}-{slug}-{uuid.uuid4().hex[:8]}"


def _job_from_payload(data: dict[str, Any], fallback_id: str) -> Job:
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise QueueError("job prompt is empty")
    mode = str(data.get("mode") or "review")
    stem = data.get("stem") or ("reply" if mode == "general" else "review")
    if not valid_name(stem):
        raise QueueError(f"job stem {stem!r} is not a plain file name")
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    job_id = data.get("id") or fallback_id
    if not valid_name(job_id):
        if not valid_name(fallback_id):
            raise QueueError(f"job id {job_id!r} is not a plain name")
        log.warn(f"job id {job_id!r} is not a plain name; using {fallback_id}")
        job_id = fallback_id
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
        attempts=_attempt_count(data),
        files=_job_files(data),
        patch=_job_patch(data),
    )


def _safe_stem(value: object) -> str:
    return value if valid_name(value) else "review"


def _job_patch(data: dict[str, Any]) -> str:
    raw = data.get("patch")
    if not raw:
        return ""
    if not isinstance(raw, str):
        raise QueueError("job patch must be a string")
    return raw


def _job_files(data: dict[str, Any]) -> dict[str, str]:
    raw = data.get("files")
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise QueueError("job files must be an object of path to text")
    out: dict[str, str] = {}
    for key, value in raw.items():
        path = str(key).strip()
        if not path:
            continue
        if not isinstance(value, str):
            raise QueueError(f"job file {path!r} must be a string")
        out[path] = value
    return out


def _attempt_count(data: dict[str, Any]) -> int:
    try:
        return max(int(data.get("attempts") or 0), 0)
    except (TypeError, ValueError):
        return 0


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QueueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise QueueError(f"{path} must be a JSON object")
    return raw


def _atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if mode is not None and os.name != "nt":
        os.chmod(tmp, mode)
    tmp.replace(path)
