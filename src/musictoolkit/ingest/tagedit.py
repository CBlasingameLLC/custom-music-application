"""Edit tags on library tracks and keep an undo log, so a bad bulk edit is one click to take back."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from musictoolkit.ingest import scanner, tagwrite

logger = logging.getLogger("musictoolkit")

KEEP_BATCHES = 100  # undo history; older batches are forgotten


@dataclass
class EditResult:
    batch_id: str
    edited: int = 0
    unchanged: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    edited_ids: list[int] = field(default_factory=list)
    unchanged_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"batch_id": self.batch_id, "edited": self.edited, "unchanged": self.unchanged, "errors": self.errors}


Progress = Callable[[int, int], None]


def refresh_from_file(conn: sqlite3.Connection, track_id: int, path: Path, tag_source: str = "manual") -> None:
    """Re-read a file's tags into its library row (and its new size/mtime, so the next scan sees it as unchanged)."""
    stat = path.stat()
    tags = scanner.read_basic_tags(path)
    conn.execute(
        """
        UPDATE tracks SET file_hash = NULL, file_size = ?, file_mtime = ?, duration_seconds = ?, bitrate = ?,
            title = ?, artist = ?, album_artist = ?, album = ?, track_number = ?, disc_number = ?,
            year = ?, genre = ?, tag_source = ?, date_last_scanned = ?
        WHERE id = ?
        """,
        (
            stat.st_size, stat.st_mtime, tags["duration_seconds"], tags["bitrate"],
            tags["title"], tags["artist"], tags["album_artist"], tags["album"],
            tags["track_number"], tags["disc_number"], tags["year"], tags["genre"],
            tag_source, datetime.now(timezone.utc).isoformat(), track_id,
        ),
    )


def _label(row: sqlite3.Row) -> str:
    return row["title"] or Path(row["file_path"]).name


def edit_each(
    conn: sqlite3.Connection,
    plan: list[tuple[int, dict[str, Any]]],
    on_progress: Progress | None = None,
    tag_source: str = "manual",
) -> EditResult:
    """Write each track's own validated {column: value} changes into its file and library row, as one
    undoable batch. One bad file never stops the rest."""
    result = EditResult(batch_id=uuid.uuid4().hex[:12])
    now = datetime.now(timezone.utc).isoformat()
    for done, (track_id, changes) in enumerate(plan):
        if on_progress:
            on_progress(done, len(plan))
        row = conn.execute("SELECT id, file_path, title FROM tracks WHERE id = ? AND is_missing = 0", (track_id,)).fetchone()
        if row is None:
            result.errors.append({"track_id": track_id, "title": "", "error": "Not in the library any more"})
            continue
        path = Path(row["file_path"])
        try:
            before, after = tagwrite.apply_changes(path, changes)
        except tagwrite.TagWriteError as exc:
            result.errors.append({"track_id": track_id, "title": _label(row), "error": str(exc)})
            continue
        if not after:
            result.unchanged += 1
            result.unchanged_ids.append(track_id)
            continue
        refresh_from_file(conn, track_id, path, tag_source=tag_source)
        conn.execute(
            "INSERT INTO tag_edits (batch_id, track_id, before_json, after_json, edited_at) VALUES (?, ?, ?, ?, ?)",
            (result.batch_id, track_id, json.dumps(before), json.dumps(after), now),
        )
        conn.commit()
        result.edited += 1
        result.edited_ids.append(track_id)
    if on_progress:
        on_progress(len(plan), len(plan))
    _prune(conn)
    return result


def edit_tracks(
    conn: sqlite3.Connection,
    track_ids: list[int],
    changes: dict[str, Any],
    on_progress: Progress | None = None,
) -> EditResult:
    """The same change for every track (the Edit tags dialog)."""
    cleaned = tagwrite.clean_changes(changes)
    return edit_each(conn, [(track_id, cleaned) for track_id in track_ids], on_progress)


def list_batches(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """Recent edits that can still be undone, newest first."""
    batches: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT batch_id, track_id, after_json, edited_at FROM tag_edits ORDER BY id DESC"):
        batch = batches.get(row["batch_id"])
        if batch is None:
            if len(batches) >= limit:
                continue
            batch = batches[row["batch_id"]] = {"batch_id": row["batch_id"], "edited_at": row["edited_at"], "tracks": 0, "fields": set()}
        batch["tracks"] += 1
        batch["fields"].update(json.loads(row["after_json"]))
    return [{**b, "fields": sorted(b["fields"])} for b in batches.values()]


def undo_batch(conn: sqlite3.Connection, batch_id: str, on_progress: Progress | None = None) -> EditResult:
    """Put every file in the batch back the way it was. Files that can't be reached stay in the log to retry."""
    rows = conn.execute("SELECT id, track_id, before_json FROM tag_edits WHERE batch_id = ? ORDER BY id DESC", (batch_id,)).fetchall()
    result = EditResult(batch_id=batch_id)
    for done, edit in enumerate(rows):
        if on_progress:
            on_progress(done, len(rows))
        track = conn.execute("SELECT id, file_path, title FROM tracks WHERE id = ?", (edit["track_id"],)).fetchone()
        if track is None:
            conn.execute("DELETE FROM tag_edits WHERE id = ?", (edit["id"],))
            continue
        path = Path(track["file_path"])
        try:
            tagwrite.restore(path, json.loads(edit["before_json"]))
        except tagwrite.TagWriteError as exc:
            result.errors.append({"track_id": track["id"], "title": _label(track), "error": str(exc)})
            continue
        refresh_from_file(conn, track["id"], path)
        conn.execute("DELETE FROM tag_edits WHERE id = ?", (edit["id"],))
        conn.commit()
        result.edited += 1
    if on_progress:
        on_progress(len(rows), len(rows))
    conn.commit()
    return result


def _prune(conn: sqlite3.Connection) -> None:
    old = conn.execute(
        "SELECT batch_id FROM tag_edits GROUP BY batch_id ORDER BY MAX(id) DESC LIMIT -1 OFFSET ?", (KEEP_BATCHES,)
    ).fetchall()
    if old:
        conn.executemany("DELETE FROM tag_edits WHERE batch_id = ?", [(row["batch_id"],) for row in old])
        conn.commit()
