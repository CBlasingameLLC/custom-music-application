"""Looking for new songs by itself, every few hours: on a server nobody presses "Refresh library" after copying music over."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("musictoolkit")

CHECK_EVERY = 60.0  # seconds between asking "is it time?"


class RescanSchedule:
    """Asks for a scan of every library folder every `hours` hours, the first one that long after the app started (the scan
    at the start, when it is on, has just looked). `hours` of 0 or less does nothing."""

    def __init__(self, ctx: Any, hours: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._ctx = ctx
        self._every = float(hours) * 3600.0
        self._clock = clock
        self._last = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self._every > 0

    def tick(self) -> bool:
        """One look at whether it is time. Returns True when a scan was asked for (a scan that is already running counts)."""
        from musictoolkit.web.routers.manage import submit_scan  # noqa: PLC0415

        roots = list(self._ctx.config.library.roots)
        if not self.enabled or not roots:
            return False
        now = self._clock()
        if now - self._last < self._every:
            return False
        self._last = now
        submit_scan(self._ctx, roots, title="Refreshing library")
        return True

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="library-rescan", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(CHECK_EVERY):
            try:
                self.tick()
            except Exception:
                logger.warning("The scheduled look for new songs failed", exc_info=True)
