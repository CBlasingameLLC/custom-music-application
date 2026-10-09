"""Background jobs for the long-running operations (scan, tag, sync, ...).

One worker thread runs jobs strictly in submission order, so two operations
that write to the library or the user's files can never overlap. The GUI polls
`GET /api/jobs` to draw progress; nothing here knows about HTTP.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("musictoolkit")

MAX_LOG_LINES = 200
MAX_FINISHED_JOBS = 50


class JobCancelled(Exception):
    """Raised inside a job function (via JobHandle.check) to stop it early."""


@dataclass
class Job:
    id: str
    kind: str
    title: str
    status: str = "queued"  # queued | running | done | error | cancelled
    done: int = 0
    total: int = 0
    message: str = ""
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    log: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))

    def to_dict(self, include_log: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancel_requested": self.cancel_requested,
        }
        if include_log:
            data["log"] = list(self.log)
        return data


class JobHandle:
    """What a job function gets to report progress and notice cancellation."""

    def __init__(self, job: Job) -> None:
        self._job = job

    @property
    def id(self) -> str:
        return self._job.id

    def update(self, done: int | None = None, total: int | None = None, message: str | None = None) -> None:
        if done is not None:
            self._job.done = done
        if total is not None:
            self._job.total = total
        if message is not None:
            self._job.message = message

    def log(self, line: str) -> None:
        self._job.log.append(line)

    @property
    def cancelled(self) -> bool:
        return self._job.cancel_requested

    def check(self) -> None:
        if self._job.cancel_requested:
            raise JobCancelled()


JobFn = Callable[[JobHandle], "dict[str, Any] | None"]


class JobManager:
    """Jobs run one at a time per lane, in submission order.

    The default lane is for anything that writes the library or the user's files, so two such
    operations can never overlap. A slow job that only talks to the network (a MusicBrainz lookup
    can take an hour) goes in its own lane so it does not hold up a quick tag edit."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._queues: dict[str, queue.Queue[tuple[Job, JobFn]]] = {}
        self._lock = threading.Lock()
        self._workers: dict[str, threading.Thread] = {}

    def submit(self, kind: str, title: str, fn: JobFn, lane: str = "main") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, title=title)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune()
            jobs_queue = self._queues.setdefault(lane, queue.Queue())
            if lane not in self._workers:
                self._workers[lane] = threading.Thread(target=self._run, args=(jobs_queue,), name=f"job-worker-{lane}", daemon=True)
                self._workers[lane].start()
        jobs_queue.put((job, fn))
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return [self._jobs[job_id] for job_id in self._order if job_id in self._jobs]

    def active(self) -> list[Job]:
        return [job for job in self.list() if job.status in ("queued", "running")]

    def is_busy(self, kind: str | None = None) -> bool:
        return any(kind is None or job.kind == kind for job in self.active())

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status not in ("queued", "running"):
            return False
        job.cancel_requested = True
        return True

    def wait(self, job_id: str, timeout: float = 30.0) -> Job:
        """Block until the job finishes; for tests and shutdown, not the API."""
        job = self._jobs[job_id]
        deadline = time.time() + timeout
        while job.status in ("queued", "running"):
            if time.time() > deadline:
                raise TimeoutError(f"job {job_id} still {job.status} after {timeout}s")
            time.sleep(0.01)
        return job

    def _prune(self) -> None:
        finished = [job_id for job_id in self._order if self._jobs[job_id].status in ("done", "error", "cancelled")]
        for job_id in finished[:-MAX_FINISHED_JOBS]:
            del self._jobs[job_id]
            self._order.remove(job_id)

    def _run(self, jobs_queue: "queue.Queue[tuple[Job, JobFn]]") -> None:
        while True:
            job, fn = jobs_queue.get()
            if job.cancel_requested:
                job.status = "cancelled"
                job.finished_at = time.time()
                continue
            job.status = "running"
            job.started_at = time.time()
            try:
                job.result = fn(JobHandle(job)) or {}
                job.status = "done"
            except JobCancelled:
                job.status = "cancelled"
            except Exception as exc:
                logger.error("Job %s (%s) failed", job.id, job.title, exc_info=True)
                job.error = f"{type(exc).__name__}: {exc}"
                job.log.append(traceback.format_exc())
                job.status = "error"
            finally:
                job.finished_at = time.time()
