"""Copying the database by itself, every day (every `backup_hours`): on a server nobody presses "Back up the database now"."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from musictoolkit import backup
from musictoolkit.backup import BackupError, Outcome

logger = logging.getLogger("musictoolkit")

CHECK_EVERY = 60.0  # seconds between asking "is it time?"
RETRY_AFTER = 3600.0  # seconds before what there is to do is tried again after a try (so that trouble is not met every minute)
GRACE = 600.0  # seconds after the start during which a copy that is late is not yet called overdue (the first one is made a minute after the start)
OVERDUE_AFTER = 3  # times the interval since the newest copy, after which the health check says so
STOP_WAIT = 5.0  # seconds that stopping waits for a copy that is being made (the app's own stop is not held up longer; the thread is a daemon)


class BackupSchedule:
    """Makes a copy of the database in the app's `backups` folder and in each of `[database] backup_folders` when the newest copy in any of them is older than
    `backup_hours` (the first one a minute after the app started, if there is none that new). A folder that does not have that newest copy (its drive was away, or it
    was only now named) is given it as it is, and no new copy is made of the database for that. It does nothing when `backup_hours` is 0. The age of a copy is the date
    of its file, so that a restart does not start the count again. The settings are read each time, so that a change of them (the file is read at the start) is not
    needed twice."""

    def __init__(self, ctx: Any, clock: Any = time.time) -> None:
        self._ctx = ctx
        self._clock = clock
        self._started = clock()
        self._tried: float | None = None
        self._last: dict[str, Any] | None = None
        self._lock = threading.Lock()  # (one copy at a time: the schedule's and the button's)
        self._said: set[str] = set()  # the settings that were said to be wrong, each once
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def primary(self) -> Path:
        return self._ctx.data_dir / "backups"

    def settings(self) -> backup.Settings:
        """What the config says; a setting that is not what it should be is said in the log and replaced by the usual one (the server refuses to start with one)."""
        try:
            return backup.settings_of(self._ctx.config.database)
        except BackupError as error:
            if str(error) not in self._said:
                self._said.add(str(error))
                logger.warning("%s The usual settings are used.", error)
            return backup.Settings()

    def newest(self, folder: Path, now: float) -> float:
        copies = backup.listing(folder, now)
        return copies[0].made if copies else 0.0

    def tick(self) -> list[Outcome] | None:
        """One look at whether there is something to do. The outcomes, when a copy was made or given to a folder that lacked it; None when there was nothing to do.
        What there is to do waits an hour after a try that had trouble (it is not tried every minute), or the time between copies when that is shorter."""
        settings = self.settings()
        now = self._clock()
        interval = settings.hours * 3600.0
        if settings.hours <= 0 or (self._tried is not None and now - self._tried < min(interval, RETRY_AFTER)):
            return None
        places = [self.primary, *settings.folders]
        held = {folder: backup.listing(folder, now) for folder in places}
        newest = max((copies[0] for copies in held.values() if copies), key=lambda copy: (copy.made, copy.path.name), default=None)
        if newest is None or now - newest.made >= interval:
            return self.run_now()
        # A copy is there and new enough. A folder without it is given it: the drive was away, or the folder was named only now. A folder that is not there is not asked
        # about every minute (it is said when a copy is made, and the health check says so when no copy has come to it for three times the interval).
        behind = [folder for folder in places if (folder == self.primary or folder.is_dir()) and all(copy.path.name != newest.path.name for copy in held[folder])]
        if not behind:
            return None
        with self._lock:
            return self._record(backup.catch_up(newest.path, behind, self.primary, settings.keep))

    def run_now(self) -> list[Outcome]:
        """A new copy in every folder now, whether or not it is time (the button, and `tick`). Raises nothing for a folder that could not be written: its outcome says why."""
        settings = self.settings()
        with self._lock:
            return self._record(backup.run(Path(self._ctx.db_path), self.primary, settings.folders, settings.keep, datetime.fromtimestamp(self._clock())))

    def _record(self, outcomes: list[Outcome]) -> list[Outcome]:
        """Remember when it was tried and what went wrong (for the throttle and for Settings), and say it in the log. Called with the lock held."""
        self._tried = self._clock()
        self._last = {"when": datetime.now(timezone.utc).isoformat(), "problems": [{"folder": str(o.folder), "problem": o.problem} for o in outcomes if o.problem]}
        for outcome in outcomes:
            if outcome.problem:
                logger.warning("Copy of the database: %s: %s", outcome.folder, outcome.problem)
            else:
                logger.info("Copy of the database: %s (%.1f MB), %d older taken away", outcome.path, outcome.size / 1e6, outcome.pruned)
        return outcomes

    def status(self) -> dict[str, Any]:
        """What Settings shows: the settings, what each folder holds, the last run, and whether the copies are late."""
        settings = self.settings()
        now = self._clock()
        folders = []
        for folder in [self.primary, *settings.folders]:
            copies = backup.listing(folder, now)
            folders.append({
                "folder": str(folder), "there": folder.is_dir(), "copies": len(copies), "size": sum(copy.size for copy in copies),
                "newest": datetime.fromtimestamp(copies[0].made, timezone.utc).isoformat() if copies and copies[0].made else None,
            })
        return {"hours": settings.hours, "keep": settings.keep, "folders": folders, "last": self._last, "overdue": self.overdue(settings, now)}

    def overdue(self, settings: backup.Settings | None = None, now: float | None = None) -> bool:
        """Are copies on, and does any folder (the app's own, or one the config names, there or not) have no copy newer than three times the interval, the app having
        been up for a while? A copy that was asked for and is not being made is what a monitor has to hear about."""
        settings = settings or self.settings()
        now = self._clock() if now is None else now
        limit = OVERDUE_AFTER * settings.hours * 3600.0
        return settings.hours > 0 and now - self._started > GRACE and any(now - self.newest(folder, now) > limit for folder in (self.primary, *settings.folders))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="database-backup", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=STOP_WAIT)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(CHECK_EVERY):
            try:
                self.tick()
            except Exception:
                logger.warning("The scheduled copy of the database failed", exc_info=True)
