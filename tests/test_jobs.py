import threading
import time

import pytest

from musictoolkit.web.jobs import JobCancelled, JobManager


def test_jobs_run_one_at_a_time_in_order() -> None:
    jobs = JobManager()
    order: list[str] = []
    running = threading.Event()

    def make(name: str, hold: bool = False):
        def run(handle):
            order.append(f"start {name}")
            if hold:
                running.wait(5)
            order.append(f"end {name}")
        return run

    first = jobs.submit("test", "first", make("a", hold=True))
    second = jobs.submit("test", "second", make("b"))
    time.sleep(0.1)
    assert second.status == "queued"  # waits for the first: file operations never overlap
    running.set()
    jobs.wait(second.id)
    assert order == ["start a", "end a", "start b", "end b"] and first.status == "done"


def test_progress_result_and_log_are_reported() -> None:
    jobs = JobManager()

    def run(handle):
        handle.update(done=1, total=4, message="working")
        handle.log("a line")
        return {"answer": 42}

    job = jobs.wait(jobs.submit("test", "t", run).id)
    assert (job.status, job.done, job.total, job.message, job.result) == ("done", 1, 4, "working", {"answer": 42})
    assert list(job.log) == ["a line"]
    assert job.to_dict(include_log=True)["log"] == ["a line"]
    assert job.finished_at and job.started_at


def test_a_failing_job_reports_the_error_and_the_worker_survives() -> None:
    jobs = JobManager()

    def boom(handle):
        raise ValueError("bad input")

    failed = jobs.wait(jobs.submit("test", "boom", boom).id)
    assert failed.status == "error" and failed.error == "ValueError: bad input"
    assert "Traceback" in "\n".join(failed.log)
    ok = jobs.wait(jobs.submit("test", "after", lambda handle: {"ok": True}).id)
    assert ok.status == "done"


def test_cancel_stops_a_running_job_at_its_next_check() -> None:
    jobs = JobManager()
    started = threading.Event()

    def loop(handle):
        started.set()
        for _ in range(1000):
            handle.check()
            time.sleep(0.01)

    job = jobs.submit("test", "loop", loop)
    assert started.wait(5)
    assert jobs.cancel(job.id) is True
    assert jobs.wait(job.id).status == "cancelled"
    assert jobs.cancel(job.id) is False  # already finished


def test_a_job_cancelled_while_queued_never_runs() -> None:
    jobs = JobManager()
    gate = threading.Event()
    ran: list[str] = []
    jobs.submit("test", "blocker", lambda handle: gate.wait(5))
    queued = jobs.submit("test", "queued", lambda handle: ran.append("ran"))
    jobs.cancel(queued.id)
    gate.set()
    time.sleep(0.2)
    assert queued.status == "cancelled" and ran == []


def test_active_and_busy_queries() -> None:
    jobs = JobManager()
    gate = threading.Event()
    job = jobs.submit("scan", "s", lambda handle: gate.wait(5))
    time.sleep(0.05)
    assert [j.id for j in jobs.active()] == [job.id] and jobs.is_busy("scan") and not jobs.is_busy("sync")
    gate.set()
    jobs.wait(job.id)
    assert jobs.active() == []


def test_wait_times_out_loudly() -> None:
    jobs = JobManager()
    gate = threading.Event()
    job = jobs.submit("test", "slow", lambda handle: gate.wait(5))
    with pytest.raises(TimeoutError):
        jobs.wait(job.id, timeout=0.1)
    gate.set()


def test_old_finished_jobs_are_pruned() -> None:
    jobs = JobManager()
    last = None
    for _ in range(60):
        last = jobs.submit("test", "t", lambda handle: None)
    jobs.wait(last.id)
    jobs.submit("test", "trigger prune", lambda handle: None)
    assert len(jobs.list()) <= 52


def test_job_cancelled_exception_is_exported() -> None:
    assert issubclass(JobCancelled, Exception)
