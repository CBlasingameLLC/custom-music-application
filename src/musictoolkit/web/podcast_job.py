"""Checking podcasts for new episodes: as a background job, and by itself every few hours while there is something followed."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from musictoolkit.db.connection import connect
from musictoolkit.podcasts import refresh as podcasts
from musictoolkit.web.jobs import Job, JobHandle

logger = logging.getLogger("musictoolkit")

FIRST_LOOK_AFTER = 45.0  # seconds after the app starts: let the window come up first
CHECK_EVERY = 1800.0  # seconds between asking "is it time?"


def submit_refresh(ctx: Any, only: int | None = None) -> Job | None:
    """Queue a check of the podcasts (network lane), or None if one is already queued or running."""
    if ctx.jobs.is_busy("podcasts"):
        return None

    def run(handle: JobHandle) -> dict[str, Any]:
        conn = connect(ctx.db_path)
        try:
            result = podcasts.refresh_all(
                conn, only=only, progress=lambda done, total, title: handle.update(done=done, total=total, message=title), check=handle.check,
            )
        finally:
            conn.close()
        for message in result["messages"]:
            handle.log(message)
        return result

    return ctx.jobs.submit("podcasts", "Checking podcasts for new episodes", run, lane="network")


class PodcastSchedule:
    """Checks the podcasts every few hours, but only while the switch in Settings is on and there is a podcast to check."""

    def __init__(self, ctx: Any, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._ctx = ctx
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> bool:
        """One look at whether it is time. Returns True when a check was queued."""
        if not self._ctx.config.app.podcast_refresh:
            return False
        with self._ctx.db() as conn:
            row = conn.execute("SELECT COUNT(*) AS n, MIN(last_checked_at) AS oldest, SUM(last_checked_at IS NULL) AS never FROM podcasts").fetchone()
        if not row["n"] or not (row["never"] or podcasts.due(row["oldest"], self._clock())):
            return False
        return submit_refresh(self._ctx) is not None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="podcast-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        delay = FIRST_LOOK_AFTER
        while not self._stop.wait(delay):
            try:
                self.tick()
            except Exception:  # the thread must outlive any one bad moment
                logger.exception("Podcast schedule stumbled")
            delay = CHECK_EVERY
