"""The history of conversions: what was converted, into which format and folder, and the file each song made."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ITEM_COLUMNS = {"status", "message", "size", "mtime", "target"}
LEFT_UNDONE = "The app was closed before this song was finished."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_conversion(
    conn: sqlite3.Connection, *, batch_id: str, title: str, fmt: str, quality: str, destination: str, scheme: str, options: dict[str, Any], left_out: int,
    items: list[dict[str, Any]],
) -> tuple[int, list[int]]:
    """A conversion that is about to start, with every song it will write as a row. `items`: track_id, title, source, target, action.
    Returns the conversion's id and the ids of its rows, in order."""
    cursor = conn.execute(
        "INSERT INTO conversions (batch_id, title, format, quality, destination, scheme, options, songs, left_out, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (batch_id, title, fmt, quality, destination, scheme, json.dumps(options, sort_keys=True), len(items), left_out, _now()),
    )
    conversion_id = int(cursor.lastrowid)
    ids: list[int] = []
    for item in items:
        row = conn.execute(
            "INSERT INTO conversion_items (conversion_id, track_id, title, source, target, action) VALUES (?, ?, ?, ?, ?, ?)",
            (conversion_id, item.get("track_id"), item["title"], item["source"], item["target"], item["action"]),
        )
        ids.append(int(row.lastrowid))
    conn.commit()
    return conversion_id, ids


def set_item(conn: sqlite3.Connection, item_id: int, **fields: Any) -> None:
    unknown = set(fields) - ITEM_COLUMNS
    if unknown:
        raise ValueError(f"unknown columns {sorted(unknown)}")
    if fields:
        assignments = ", ".join(f"{name} = ?" for name in fields)
        conn.execute(f"UPDATE conversion_items SET {assignments} WHERE id = ?", [*fields.values(), item_id])
        conn.commit()


def finish_conversion(conn: sqlite3.Connection, conversion_id: int, status: str, message: str | None = None) -> None:
    """The end of a conversion: the counts are made from its rows."""
    counts = conn.execute(
        "SELECT COALESCE(SUM(status = 'done' AND action = 'convert'), 0) AS converted, COALESCE(SUM(status = 'done' AND action = 'copy'), 0) AS copied, "
        "COALESCE(SUM(status = 'failed'), 0) AS failed, COALESCE(SUM(CASE WHEN status = 'done' THEN size ELSE 0 END), 0) AS bytes "
        "FROM conversion_items WHERE conversion_id = ?", (conversion_id,),
    ).fetchone()
    conn.execute(
        "UPDATE conversions SET status = ?, message = ?, converted = ?, copied = ?, failed = ?, bytes = ?, finished_at = ? WHERE id = ?",
        (status, message, counts["converted"], counts["copied"], counts["failed"], counts["bytes"], _now(), conversion_id),
    )
    conn.commit()


def mark_undone(conn: sqlite3.Connection, conversion_id: int) -> None:
    conn.execute("UPDATE conversions SET undone_at = ? WHERE id = ?", (_now(), conversion_id))
    conn.commit()


def _conversion(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["options"] = json.loads(out.get("options") or "{}")
    return out


def list_conversions(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM conversions ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return [_conversion(row) for row in rows]


ONLY = {"not_made": " AND status IN ('failed', 'skipped')"}  # what a page of a conversion's songs can be narrowed to


def get_conversion(conn: sqlite3.Connection, conversion_id: int, offset: int = 0, limit: int = 100, only: str | None = None) -> dict[str, Any] | None:
    """One conversion with a page of its songs: all of them, or (`only` "not_made") the ones that failed or were left out. `items_total` counts the
    songs that page is taken from. Raises ValueError for an `only` that is not offered."""
    if only is not None and only not in ONLY:
        raise ValueError(f"only must be one of: {', '.join(ONLY)}")
    row = conn.execute("SELECT * FROM conversions WHERE id = ?", (conversion_id,)).fetchone()
    if row is None:
        return None
    out = _conversion(row)
    offset, limit = max(0, offset), max(1, min(limit, 500))
    narrowing = ONLY.get(only or "", "")
    out["items_total"] = conn.execute(f"SELECT COUNT(*) FROM conversion_items WHERE conversion_id = ?{narrowing}", (conversion_id,)).fetchone()[0]
    out["items"] = [
        dict(item) for item in conn.execute(
            f"SELECT id, track_id, title, source, target, action, status, message, size FROM conversion_items WHERE conversion_id = ?{narrowing} ORDER BY id LIMIT ? OFFSET ?",
            (conversion_id, limit, offset),
        )
    ]
    return out


def done_items(conn: sqlite3.Connection, conversion_id: int) -> list[dict[str, Any]]:
    """The files a conversion made, for taking it back."""
    return [dict(r) for r in conn.execute(
        "SELECT id, target, size, mtime FROM conversion_items WHERE conversion_id = ? AND status = 'done' ORDER BY id", (conversion_id,),
    )]


def recover(conn: sqlite3.Connection, scratch: Path | None = None) -> int:
    """A conversion that was running when the app stopped did not finish: say so, and remove the half-written files it left. Returns how many."""
    running = conn.execute("SELECT id, batch_id FROM conversions WHERE status = 'running'").fetchall()
    for conversion in running:
        for item in conn.execute("SELECT target FROM conversion_items WHERE conversion_id = ? AND status IN ('waiting', 'working')", (conversion["id"],)):
            with contextlib.suppress(OSError):
                Path(item["target"] + ".part").unlink(missing_ok=True)
        conn.execute(
            "UPDATE conversion_items SET status = 'failed', message = ? WHERE conversion_id = ? AND status IN ('waiting', 'working')", (LEFT_UNDONE, conversion["id"]),
        )
        finish_conversion(conn, conversion["id"], "interrupted", "The app was closed before the conversion was finished.")
        if scratch is not None and scratch.is_dir():
            for leftover in scratch.glob(f"{conversion['batch_id']}-*"):
                with contextlib.suppress(OSError):
                    leftover.unlink()
    return len(running)
