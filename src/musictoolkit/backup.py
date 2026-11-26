"""Copies of the library's database.

The database holds what nothing else does: the ratings, the favorites, the history of what was played, the playlists, the podcasts followed, the stations kept. The
songs can be copied over again from anywhere; these cannot. A copy is made with SQLite's own backup (so it is whole and consistent while the app goes on
writing), looked over (`PRAGMA quick_check`), and only then given its name, so that a copy that is there is a copy that works. It is written under another name
first and renamed, so that a power cut leaves no half-copy that looks like a whole one.

A copy is `library-YYYYmmdd-HHMMSS.db` in a folder: the app's own (`backups`, beside the config), and any folders the person named in `[database] backup_folders`
(the music drive, which is another device than the one the database is on). A copy that is made is copied to the others as it is, and read back to be sure it is
what was written. A folder that is not there is not made (a drive that is not mounted is not a folder to make on the card under it). Each folder keeps the newest
`backup_keep` copies and nothing else of it is touched.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import sqlite3
import stat
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

NAME = re.compile(r"^library-\d{8}-\d{6}\.db$")
PARTIAL = ".partial"
STALE_PARTIAL = 3600.0  # seconds after which a half-written copy that is still lying about is taken to be left by a crash
FUTURE_SLACK = 300.0  # seconds a copy may be dated ahead of the clock (clocks differ a little) before its date is not believed
PRIVATE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP  # the most a copy is open to (0o640): it holds what was listened to, so it is for its owner and the group (the music drive's), not for everyone on the computer
DEFAULT_HOURS = 24
DEFAULT_KEEP = 7
TABLES = {"songs": "SELECT COUNT(*) FROM tracks", "plays": "SELECT COUNT(*) FROM play_history", "playlists": "SELECT COUNT(*) FROM playlists",
          "favorites": "SELECT COUNT(*) FROM tracks WHERE favorite = 1"}


class BackupError(Exception):
    """Something that is said in words to the person (not a traceback)."""


@dataclass(frozen=True)
class Copy:
    path: Path
    size: int
    made: float  # seconds since 1970; 0 for a copy that is dated in the future, which means the clock was wrong when it was written (it is the oldest, not the newest)


@dataclass(frozen=True)
class Outcome:
    """What happened in one folder."""

    folder: Path
    path: Path | None = None  # the copy that was made there
    size: int = 0
    pruned: int = 0  # how many older copies were taken away
    problem: str | None = None


@dataclass(frozen=True)
class Report:
    """What a copy holds, and whether it is sound."""

    ok: bool
    problem: str | None = None
    schema: int = 0  # the number of the last change of the database's layout that it has (PRAGMA user_version)
    counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    hours: float = DEFAULT_HOURS  # a copy by itself this often (0: none by itself)
    keep: int = DEFAULT_KEEP  # how many of the newest copies each folder keeps
    folders: tuple[Path, ...] = ()  # the folders besides the app's own `backups`


def settings_of(database: object) -> Settings:
    """The settings of `[database]` (`backup_hours`, `backup_keep`, `backup_folders`), checked; BackupError, in words, for one that is not what it should be."""
    hours, keep, folders = (getattr(database, name) for name in ("backup_hours", "backup_keep", "backup_folders"))
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or hours < 0:
        raise BackupError("[database] backup_hours must be a number of hours, 0 or more (0: no copies by themselves).")
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise BackupError("[database] backup_keep must be a whole number, 1 or more.")
    if not isinstance(folders, list) or not all(isinstance(folder, str) and folder.strip() for folder in folders):
        raise BackupError('[database] backup_folders must be a list of folders in quotes, for example backup_folders = ["/srv/music/backups"].')
    return Settings(hours, keep, tuple(Path(folder) for folder in folders))


def listing(folder: Path, now: float | None = None) -> list[Copy]:
    """The copies in a folder, newest first (a folder that is not there has none)."""
    now = time.time() if now is None else now
    found: list[Copy] = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return found
    for entry in entries:
        if not NAME.match(entry.name):
            continue
        try:
            if not entry.is_file():
                continue
            info = entry.stat()
        except OSError:  # (gone since the folder was read)
            continue
        found.append(Copy(Path(entry.path), info.st_size, info.st_mtime if info.st_mtime <= now + FUTURE_SLACK else 0.0))
    found.sort(key=lambda copy: (copy.made, copy.path.name), reverse=True)
    return found


def _private(path: Path) -> None:
    """Take away from a copy what the group may not do and what everyone else may (and nothing is added: a person whose umask is stricter keeps it)."""
    os.chmod(path, stat.S_IMODE(path.stat().st_mode) & PRIVATE)


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def snapshot(db_path: Path, folder: Path, now: datetime | None = None, prefix: str = "library") -> Path:
    """One whole copy of the database, in `folder` (which has to be there)."""
    if not Path(db_path).is_file():  # (connecting would make an empty one, and a copy of nothing would look like a copy)
        raise BackupError("there is no database to copy")
    final = folder / f"{prefix}-{now or datetime.now():%Y%m%d-%H%M%S}.db"
    partial = final.with_name(final.name + PARTIAL)
    source = sqlite3.connect(db_path, timeout=30)
    target = None
    try:
        target = sqlite3.connect(partial)
        source.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")  # (the copy is one file by itself, not a file and a log that has to be kept with it)
        try:
            verdict = [row[0] for row in target.execute("PRAGMA quick_check")]
        except sqlite3.DatabaseError as error:  # (some damage is said by an error and not by a line of the answer)
            verdict = [str(error)]
        if verdict != ["ok"]:
            raise BackupError(f"the database did not pass its own check ({verdict[0]}), so no copy of it was kept")
    except BaseException:
        if target is not None:
            target.close()
        source.close()
        partial.unlink(missing_ok=True)  # (not there when the copy could not even be opened)
        raise
    target.close()
    source.close()
    _private(partial)
    os.replace(partial, final)
    return final


def copy_to(copy: Path, folder: Path) -> Path:
    """The same copy in another folder, read back to be sure that what was written is what was read, with the date it was made."""
    final = folder / copy.name
    partial = final.with_name(final.name + PARTIAL)
    try:
        shutil.copyfile(copy, partial)
        if _digest(partial) != _digest(copy):
            raise BackupError("what was written is not what was read back")
        info = copy.stat()
        os.utime(partial, (info.st_atime, info.st_mtime))
        _private(partial)
        os.replace(partial, final)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return final


def prune(folder: Path, keep: int, protect: Path | None = None) -> int:
    """Take away the copies that are not among the newest `keep` (and any half-written copy left by a crash), and nothing else. Returns how many copies."""
    gone = 0
    for index, copy in enumerate(listing(folder)):
        if index >= keep and copy.path != protect:
            copy.path.unlink(missing_ok=True)
            gone += 1
    now = time.time()
    for entry in os.scandir(folder):
        if entry.name.startswith("library-") and entry.name.endswith(".db" + PARTIAL) and now - entry.stat().st_mtime > STALE_PARTIAL:
            Path(entry.path).unlink(missing_ok=True)
    return gone


def _in_words(error: Exception) -> str:
    if isinstance(error, BackupError):
        return str(error)
    if isinstance(error, OSError):
        return error.strerror or str(error)  # ("No space left on device", "Permission denied")
    return f"{type(error).__name__}: {error}"


def _ready(folder: Path, primary: Path) -> None:
    """The primary folder is made if it is not there; any other has to be (a drive that is not mounted is not a folder to make on the card under it)."""
    if folder == primary:
        folder.mkdir(parents=True, exist_ok=True)
    elif not folder.is_dir():
        raise BackupError("the folder is not there (is the drive connected?)")


def run(db_path: Path, primary: Path, others: Sequence[Path] = (), keep: int = DEFAULT_KEEP, now: datetime | None = None) -> list[Outcome]:
    """A copy in the primary folder (made if it is not there) and in each of the others (which have to be there), each kept to its newest `keep`. A folder that
    could not be written says why, in words, and the rest go on."""
    outcomes: list[Outcome] = []
    made: Path | None = None
    seen: set[Path] = set()
    for folder in (primary, *others):
        if folder.resolve() in seen:
            continue
        seen.add(folder.resolve())
        try:
            _ready(folder, primary)
            path = snapshot(db_path, folder, now) if made is None else copy_to(made, folder)
            made = made or path
            outcomes.append(Outcome(folder, path, path.stat().st_size, prune(folder, keep, protect=path)))
        except Exception as error:
            outcomes.append(Outcome(folder, problem=_in_words(error)))
    return outcomes


def catch_up(copy: Path, folders: Sequence[Path], primary: Path, keep: int = DEFAULT_KEEP) -> list[Outcome]:
    """A copy that was already made, put in each of these folders (the primary one made if it is not there, any other has to be), each kept to its newest `keep`.
    For a folder that was not there when the copy was made, or that was only now named: it gets the newest copy as it is, and no new one is made of the database."""
    outcomes: list[Outcome] = []
    for folder in folders:
        try:
            _ready(folder, primary)
            path = copy_to(copy, folder)
            outcomes.append(Outcome(folder, path, path.stat().st_size, prune(folder, keep, protect=path)))
        except Exception as error:
            outcomes.append(Outcome(folder, problem=_in_words(error)))
    return outcomes


def latest_schema() -> int:
    from musictoolkit.db.connection import _migration_files  # noqa: PLC0415

    return _migration_files()[-1][0]


def check(path: Path) -> Report:
    """Whether a copy is sound, which layout it has, and what it holds."""
    try:
        database = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as error:
        return Report(False, str(error))
    try:
        schema = database.execute("PRAGMA user_version").fetchone()[0]
        verdict = [row[0] for row in database.execute("PRAGMA integrity_check")]
        if verdict != ["ok"]:
            return Report(False, f"it did not pass its check ({verdict[0]})", schema)
        counts = {name: database.execute(query).fetchone()[0] for name, query in TABLES.items()}
    except sqlite3.Error as error:
        if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_CORRUPT:
            return Report(False, f"it is damaged ({error})")
        return Report(False, f"this is not a Music Toolkit database ({error})")  # (not a database at all, or not one of this app's)
    finally:
        database.close()
    return Report(True, None, schema, counts)


def restore(copy: Path, db_path: Path, kept_in: Path, now: datetime | None = None) -> Path | None:
    """Put the contents of a copy into the database, all of it, with SQLite's own backup (which does not mind that the app has it open). What the database
    held before is kept first in `kept_in` as `library-before-restore-...` (which pruning leaves alone); that copy is returned (None if there was no database)."""
    report = check(copy)
    if not report.ok:
        raise BackupError(f"{copy.name} cannot be restored: {report.problem}")
    if report.schema > latest_schema():
        raise BackupError(f"{copy.name} was made by a newer version of the app (its layout is {report.schema}, this one knows {latest_schema()}): update the app first")
    kept = None
    if db_path.exists():
        kept_in.mkdir(parents=True, exist_ok=True)
        kept = snapshot(db_path, kept_in, now, prefix="library-before-restore")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"{copy.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    target = sqlite3.connect(db_path, timeout=30)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return kept
