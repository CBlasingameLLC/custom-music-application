"""Housekeeping for the library's records: forgetting songs for good, and the missing-files report.

A song whose file cannot be found is only flagged (is_missing = 1), never dropped, because the usual cause is a
drive that is not plugged in, and the song's plays, rating and playlist entries should survive that. Forgetting
is the deliberate step that removes them."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

OTHER = "*other*"  # the bucket for missing songs that sit under no library folder (a folder that was removed)
CHUNK = 500


def _chunks(ids: list[int]) -> Iterator[list[int]]:
    for start in range(0, len(ids), CHUNK):
        yield ids[start:start + CHUNK]


def forget_tracks(conn: sqlite3.Connection, track_ids: list[int]) -> int:
    """Remove songs from the library for good, with what points at them. Play history stays (it still says
    what was listened to); playlists lose the entry. Returns how many songs were removed."""
    removed = 0
    for chunk in _chunks(list(dict.fromkeys(track_ids))):
        marks = ",".join("?" * len(chunk))
        conn.execute(f"UPDATE play_history SET track_id = NULL WHERE track_id IN ({marks})", chunk)
        conn.execute(f"DELETE FROM playlist_tracks WHERE track_id IN ({marks})", chunk)
        conn.execute(f"DELETE FROM sync_manifest WHERE track_id IN ({marks})", chunk)
        conn.execute(f"DELETE FROM tag_proposals WHERE track_id IN ({marks})", chunk)
        removed += conn.execute(f"DELETE FROM tracks WHERE id IN ({marks})", chunk).rowcount
    conn.commit()
    return removed


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _bucket(path: str, roots: list[str]) -> str:
    """The deepest library folder containing this path, or OTHER."""
    here = _norm(path)
    inside = [r for r in roots if here.startswith(_norm(r) + os.sep)]
    return max(inside, key=len, default=OTHER)


def _target_bucket(root: str, roots: list[str]) -> str:
    return OTHER if root == OTHER else next((x for x in roots if _norm(x) == _norm(root)), root)


def _missing_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, file_path, title, artist, album, date_last_scanned, file_size, "
        "(SELECT COUNT(*) FROM play_history h WHERE h.track_id = tracks.id) AS plays "
        "FROM tracks WHERE is_missing = 1 ORDER BY file_path"
    ).fetchall()


def missing_summary(conn: sqlite3.Connection, roots: list[str]) -> dict[str, Any]:
    counts = dict.fromkeys(roots, 0)
    elsewhere = 0
    rows = conn.execute("SELECT file_path FROM tracks WHERE is_missing = 1").fetchall()
    for row in rows:
        bucket = _bucket(row["file_path"], roots)
        if bucket == OTHER:
            elsewhere += 1
        else:
            counts[bucket] += 1
    return {
        "total": len(rows),
        "roots": [{"path": r, "count": counts[r], "available": Path(r).is_dir()} for r in roots],
        "elsewhere": elsewhere,
    }


def list_missing(
    conn: sqlite3.Connection, roots: list[str], offset: int = 0, limit: int = 100, root: str | None = None
) -> dict[str, Any]:
    rows = _missing_rows(conn)
    if root is not None:
        wanted = _target_bucket(root, roots)
        rows = [r for r in rows if _bucket(r["file_path"], roots) == wanted]
    page = rows[offset:offset + limit]
    return {
        "total": len(rows),
        "items": [
            {
                "id": r["id"], "path": r["file_path"], "title": r["title"], "artist": r["artist"], "album": r["album"],
                "last_seen": r["date_last_scanned"], "plays": r["plays"], "size": r["file_size"],
            }
            for r in page
        ],
    }


def forget_missing(
    conn: sqlite3.Connection, roots: list[str], track_ids: list[int] | None = None, root: str | None = None
) -> dict[str, int]:
    """Forget songs flagged missing: the given ones, those under one folder (OTHER for folders no longer in the
    library), or every one. A song whose file has turned up again is kept."""
    rows = _missing_rows(conn)
    if track_ids is not None:
        wanted = set(track_ids)
        rows = [r for r in rows if r["id"] in wanted]
    elif root is not None:
        target = _target_bucket(root, roots)
        rows = [r for r in rows if _bucket(r["file_path"], roots) == target]
    gone = [r["id"] for r in rows if not Path(r["file_path"]).exists()]
    forgotten = forget_tracks(conn, gone)
    return {"forgotten": forgotten, "kept": len(rows) - len(gone)}
