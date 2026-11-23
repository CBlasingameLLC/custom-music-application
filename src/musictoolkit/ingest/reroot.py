"""A library that is somewhere else now: the database has the absolute path of every song, so a library copied from a Windows PC
to a Raspberry Pi (D:\\Music -> /srv/music/library) has to be told where it is. Every song keeps its number, rating, plays and
playlists; only the path changes. Because the size and time of each file are untouched, the next scan finds every song unchanged
and reads no tags.

Everything is done on the strings in the database: nothing here looks at the disk."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Every column of the database that holds the path of something in or beside the library (devices keep the place a drive was
# last seen in, which belongs to that drive and not to the library, so they are not here).
PATH_COLUMNS: tuple[tuple[str, str], ...] = (
    ("tracks", "file_path"),
    ("file_moves", "old_path"),
    ("file_moves", "new_path"),
    ("disc_rips", "root"),
    ("disc_rips", "folder"),
    ("disc_rips", "cover_path"),
    ("disc_rip_tracks", "file_path"),
    ("conversions", "destination"),
    ("conversion_items", "source"),
    ("conversion_items", "target"),
    ("disc_burn_items", "source"),
    ("disc_burns", "output"),
)

_WINDOWS_START = re.compile(r"^(?:[A-Za-z]:|\\\\)")  # a drive letter, or a network share


class RerootError(ValueError):
    """The two folders cannot be used. The message says why."""


class RerootConflict(RerootError):
    """Some songs would get a path that another song already has."""


def style_of(path: str) -> str:
    """"windows" for a drive letter or a network share, else "posix"."""
    return "windows" if _WINDOWS_START.match(path.strip()) else "posix"


def _normalised(path: str, style: str) -> str:
    text = path.strip()
    if style == "windows":
        return text.replace("/", "\\").rstrip("\\")
    return text.rstrip("/")


@dataclass(frozen=True)
class Mapping:
    """From one folder to another: every path that is that folder, or inside it, becomes the same place under the new one."""

    source: str
    target: str
    source_style: str
    target_style: str

    @classmethod
    def of(cls, source: str, target: str) -> Mapping:
        if not source.strip() or not target.strip():
            raise RerootError("Both folders are needed.")
        source_style, target_style = style_of(source), style_of(target)
        for given, style, which in ((source, source_style, "from"), (target, target_style, "to")):
            if style == "posix" and not given.strip().startswith("/"):
                raise RerootError(f"--{which} must be a whole path (a drive letter and a folder, or one that starts with /), not {given!r}.")
        mapping = cls(_normalised(source, source_style), _normalised(target, target_style), source_style, target_style)
        windows = source_style == target_style == "windows"  # (D:\Music and d:/music are one place there)
        if mapping.source == mapping.target or (windows and mapping.source.lower() == mapping.target.lower()):
            raise RerootError("The two folders are the same: there is nothing to change.")
        return mapping

    def map(self, value: str | None) -> str | None:
        """Where `value` is now, or None when it is not this folder or inside it (the boundary is a separator: D:\\Music2 is not inside D:\\Music)."""
        if not value:
            return None
        if self.source_style == "windows":
            probe = value.replace("/", "\\")
            lowered, head = probe.lower(), self.source.lower()  # (Windows names ignore case)
            if lowered != head and not lowered.startswith(head + "\\"):
                return None
        elif value != self.source and not value.startswith(self.source + "/"):
            return None
        rest = value[len(self.source):]
        if self.target_style == "windows":
            rest = rest.replace("/", "\\")
        elif self.source_style == "windows":
            rest = rest.replace("\\", "/")
        return self.target + rest


@dataclass
class Plan:
    mapping: Mapping
    changes: list[tuple[str, str, int, str, str]] = field(default_factory=list)  # table, column, row, old, new
    counts: dict[tuple[str, str], int] = field(default_factory=dict)
    songs: int = 0
    songs_outside: int = 0
    conflicts: list[tuple[str, str]] = field(default_factory=list)  # a song's path now, the path it would get that is already taken

    def examples(self, n: int = 3) -> list[tuple[str, str]]:
        return [(old, new) for table, column, _, old, new in self.changes if (table, column) == ("tracks", "file_path")][:n]


def _columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """The path columns this database has (an older one may lack the tables of a newer feature)."""
    return [
        (table, column)
        for table, column in PATH_COLUMNS
        if any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))  # (no rows for a table that is not there)
    ]


def plan_reroot(conn: sqlite3.Connection, mapping: Mapping) -> Plan:
    """What would change. Touches nothing."""
    plan = Plan(mapping)
    taken = {row[0]: row[1] for row in conn.execute("SELECT file_path, id FROM tracks")}
    plan.songs = len(taken)
    new_song_paths: dict[str, int] = {}
    for table, column in _columns(conn):
        for row_id, old in conn.execute(f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL ORDER BY rowid"):  # (the song with the lower number keeps a path two would get)
            new = mapping.map(old)
            if new is None:
                continue
            if (table, column) == ("tracks", "file_path"):
                # (no song has the path it is about to get: the two folders differ, so a path under the old one never equals one under the new)
                if new in taken or new_song_paths.get(new, row_id) != row_id:
                    plan.conflicts.append((old, new))
                    continue
                new_song_paths[new] = row_id
            plan.changes.append((table, column, row_id, old, new))
            plan.counts[(table, column)] = plan.counts.get((table, column), 0) + 1
    plan.songs_outside = plan.songs - plan.counts.get(("tracks", "file_path"), 0) - len(plan.conflicts)
    return plan


def backup_database(db_path: Path, when: datetime | None = None) -> Path:
    """A whole copy of the database next to it, made with SQLite's own backup (so it is whole even while something reads it)."""
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    copy = Path(db_path).with_name(f"{Path(db_path).name}.before-reroot-{stamp}")
    source, target = sqlite3.connect(db_path), sqlite3.connect(copy)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return copy


def apply_reroot(db_path: Path, plan: Plan, *, skip_conflicts: bool = False) -> Path:
    """Make the changes in one transaction, after a copy of the database was made (returned). A song whose new path is taken stops
    everything unless `skip_conflicts`, which leaves exactly those songs as they are."""
    if plan.conflicts and not skip_conflicts:
        raise RerootConflict(f"{len(plan.conflicts)} songs would get a path that another song already has.")
    copy = backup_database(db_path)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        for table, column, row_id, _old, new in plan.changes:  # (one transaction: sqlite3 opens it at the first UPDATE, and nothing is final before the commit)
            conn.execute(f"UPDATE {table} SET {column} = ? WHERE rowid = ?", (new, row_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return copy


def map_folders(folders: list[str], mapping: Mapping) -> list[tuple[str, str]]:
    """(old, new) for each folder of a settings list that is under the old place (library folders, the rip folder)."""
    return [(old, new) for old in folders if (new := mapping.map(old)) is not None]
