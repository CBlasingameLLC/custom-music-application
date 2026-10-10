"""Running the release radar: as a background job, and by itself about once a day when the person has turned it on."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from musictoolkit.db.connection import connect
from musictoolkit.recommend import radar
from musictoolkit.web.jobs import Job, JobHandle

logger = logging.getLogger("musictoolkit")

FIRST_LOOK_AFTER = 60.0  # seconds after the app starts: let the window come up first
CHECK_EVERY = 1800.0  # seconds between asking "is it time?"


def submit_refresh(ctx: Any) -> Job | None:
    """Queue a look for new releases (network lane), or None if one is already queued or running."""
    if ctx.jobs.is_busy("radar"):
        return None

    def run(handle: JobHandle) -> dict[str, Any]:
        conn = connect(ctx.db_path)
        try:
            result = radar.refresh(conn, ctx.config, progress=lambda message: handle.update(message=message), check=handle.check)
        finally:
            conn.close()
        for message in result["messages"]:
            handle.log(message)
        return result

    return ctx.jobs.submit("radar", "Looking for new releases", run, lane="network")


def configured(cfg: Any) -> bool:
    """Is there anywhere to look? (An automatic look that could only fail is not started.)"""
    return bool(cfg.listenbrainz.enabled and cfg.listenbrainz.username) or bool(cfg.musicbrainz.contact.strip())


class RadarSchedule:
    """Looks for new releases about once a day, but only while the radar switch in Settings is on."""

    def __init__(self, ctx: Any, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._ctx = ctx
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> bool:
        """One look at whether it is time. Returns True when a look was queued."""
        cfg = self._ctx.config
        if not cfg.app.release_radar or not configured(cfg):
            return False
        with self._ctx.db() as conn:
            last = radar.state(conn)["last_run"]
        if not radar.due(last, self._clock()):
            return False
        return submit_refresh(self._ctx) is not None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="release-radar", daemon=True)
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
                logger.exception("Release radar schedule stumbled")
            delay = CHECK_EVERY
