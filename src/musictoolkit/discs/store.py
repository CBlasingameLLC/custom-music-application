"""The history of rips: which disc, what came of it, and where each song went."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_rip(
    conn: sqlite3.Connection, *, batch_id: str, disc_id: str | None, release_id: str | None, album: str, artist: str, drive: str, fmt: str,
    quality: str, accuracy: str, root: str, folder: str | None, tracks: list[dict[str, Any]],
) -> int:
    """A rip that is about to start, with every song it will make (or leave out) as a row. `tracks`: number, title, file_path, status, message."""
    cursor = conn.execute(
        "INSERT INTO disc_rips (batch_id, disc_id, release_id, album, artist, drive, format, quality, accuracy, root, folder, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (batch_id, disc_id, release_id, album, artist, drive, fmt, quality, accuracy, root, folder, _now()),
    )
    rip_id = cursor.lastrowid
    conn.executemany(
        "INSERT INTO disc_rip_tracks (rip_id, number, title, file_path, status, message) VALUES (?, ?, ?, ?, ?, ?)",
        [(rip_id, t["number"], t["title"], t.get("file_path"), t.get("status", "waiting"), t.get("message")) for t in tracks],
    )
    conn.commit()
    return int(rip_id)


def set_track(conn: sqlite3.Connection, rip_id: int, number: int, **fields: Any) -> None:
    columns = {"file_path", "size", "mtime", "track_id", "rereads", "suspect", "unreadable", "status", "message"}
    unknown = set(fields) - columns
    if unknown:
        raise ValueError(f"unknown columns {sorted(unknown)}")
    if fields:
        assignments = ", ".join(f"{name} = ?" for name in fields)
        conn.execute(f"UPDATE disc_rip_tracks SET {assignments} WHERE rip_id = ? AND number = ?", [*fields.values(), rip_id, number])
        conn.commit()


def set_cover(conn: sqlite3.Connection, rip_id: int, path: str, size: int, mtime: float) -> None:
    conn.execute("UPDATE disc_rips SET cover_path = ?, cover_size = ?, cover_mtime = ? WHERE id = ?", (path, size, mtime, rip_id))
    conn.commit()


def finish_rip(conn: sqlite3.Connection, rip_id: int, status: str, message: str | None = None) -> None:
    problems = conn.execute(
        "SELECT COALESCE(SUM(suspect + unreadable), 0) FROM disc_rip_tracks WHERE rip_id = ?", (rip_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE disc_rips SET status = ?, message = ?, problems = ?, finished_at = ? WHERE id = ?", (status, message, problems, _now(), rip_id)
    )
    conn.commit()


def mark_undone(conn: sqlite3.Connection, rip_id: int) -> None:
    conn.execute("UPDATE disc_rips SET undone_at = ? WHERE id = ?", (_now(), rip_id))
    conn.commit()


def _rip(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def list_rips(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.*, "
        "(SELECT COUNT(*) FROM disc_rip_tracks t WHERE t.rip_id = r.id AND t.status IN ('done', 'problem')) AS songs, "
        "(SELECT COUNT(*) FROM disc_rip_tracks t WHERE t.rip_id = r.id) AS tracks "
        "FROM disc_rips r ORDER BY r.started_at DESC, r.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_rip(row) for row in rows]


def get_rip(conn: sqlite3.Connection, rip_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM disc_rips WHERE id = ?", (rip_id,)).fetchone()
    if row is None:
        return None
    rip = _rip(row)
    rip["tracks"] = [dict(t) for t in conn.execute("SELECT * FROM disc_rip_tracks WHERE rip_id = ? ORDER BY number", (rip_id,)).fetchall()]
    return rip


def recover(conn: sqlite3.Connection, cache_dir: Path | None = None) -> int:
    """A rip that was running when the app stopped did not finish: say so, and remove the half-written files it left. Returns how many rips."""
    rips = conn.execute("SELECT id, batch_id FROM disc_rips WHERE status = 'running'").fetchall()
    for rip in rips:
        for track in conn.execute("SELECT file_path FROM disc_rip_tracks WHERE rip_id = ? AND status IN ('waiting', 'ripping')", (rip["id"],)):
            if track["file_path"]:
                try:
                    Path(track["file_path"] + ".part").unlink(missing_ok=True)
                except OSError:
                    pass
        conn.execute(
            "UPDATE disc_rip_tracks SET status = 'failed', message = 'The app was closed before this song was finished.' "
            "WHERE rip_id = ? AND status IN ('waiting', 'ripping')", (rip["id"],),
        )
        finish_rip(conn, rip["id"], "interrupted", "The app was closed before the rip was finished.")
        if cache_dir is not None and cache_dir.is_dir():
            for leftover in cache_dir.glob(f"{rip['batch_id']}-*.pcm"):
                try:
                    leftover.unlink()
                except OSError:
                    pass
    return len(rips)


# ---------------------------------------------------------------------------------------- burns (a history; a burn cannot be taken back)


def create_burn(
    conn: sqlite3.Connection, *, batch_id: str, kind: str, title: str, drive: str | None, disc_type: str | None = None, label: str | None = None,
    speed: int | None = None, sectors: int | None = None, items: list[dict[str, Any]] | None = None,
) -> int:
    """A burn that is about to start, with every song or file it was asked for as a row (those that will not be written say why).
    `items`: title, artist, source, track_id, sectors, status, message."""
    items = items or []
    cursor = conn.execute(
        "INSERT INTO disc_burns (batch_id, kind, title, drive, disc_type, label, speed, items, sectors, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (batch_id, kind, title, drive, disc_type, label, speed, sum(1 for i in items if i.get("status", "waiting") == "waiting"), sectors, _now()),
    )
    burn_id = cursor.lastrowid
    conn.executemany(
        "INSERT INTO disc_burn_items (burn_id, position, title, artist, source, track_id, sectors, status, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (burn_id, position, i["title"], i.get("artist"), i.get("source"), i.get("track_id"), i.get("sectors"), i.get("status", "waiting"), i.get("message"))
            for position, i in enumerate(items, start=1)
        ],
    )
    conn.commit()
    return int(burn_id)


def mark_spoiled(conn: sqlite3.Connection, burn_id: int) -> None:
    """Writing has begun: from here on the disc cannot be trusted until the burn is done."""
    conn.execute("UPDATE disc_burns SET spoiled = 1 WHERE id = ?", (burn_id,))
    conn.commit()


def finish_burn(
    conn: sqlite3.Connection, burn_id: int, status: str, message: str | None = None, *, sectors: int | None = None, verified: bool | None = None,
    output: str | None = None,
) -> None:
    """The burn is over. A burn that ended well leaves a usable disc, whatever was marked while it was writing."""
    conn.execute(
        "UPDATE disc_burns SET status = ?, message = ?, finished_at = ?, spoiled = CASE WHEN ? = 'done' THEN 0 ELSE spoiled END, "
        "sectors = COALESCE(?, sectors), verified = COALESCE(?, verified), output = COALESCE(?, output) WHERE id = ?",
        (status, message, _now(), status, sectors, None if verified is None else int(verified), output, burn_id),
    )
    if status == "done":
        conn.execute("UPDATE disc_burn_items SET status = 'done' WHERE burn_id = ? AND status = 'waiting'", (burn_id,))
    else:
        conn.execute(
            "UPDATE disc_burn_items SET status = 'failed', message = COALESCE(message, 'Not written.') WHERE burn_id = ? AND status = 'waiting'", (burn_id,)
        )
    conn.commit()


def list_burns(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM disc_burns ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def get_burn(conn: sqlite3.Connection, burn_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM disc_burns WHERE id = ?", (burn_id,)).fetchone()
    if row is None:
        return None
    burn = dict(row)
    burn["tracks"] = [dict(t) for t in conn.execute("SELECT * FROM disc_burn_items WHERE burn_id = ? ORDER BY position", (burn_id,)).fetchall()]
    return burn


def recover_burns(conn: sqlite3.Connection, cache_dir: Path | None = None) -> int:
    """A burn that was running when the app stopped did not finish: say so, and remove the temporary songs it left. Returns how many."""
    burns = conn.execute("SELECT id, batch_id, spoiled FROM disc_burns WHERE status = 'running'").fetchall()
    for burn in burns:
        words = "The app was closed before the burn was finished."
        if burn["spoiled"]:
            words += " Writing had begun, so the disc cannot be trusted. A rewritable disc can be erased and used again."
        conn.execute("UPDATE disc_burn_items SET status = 'failed', message = 'Not written.' WHERE burn_id = ? AND status = 'waiting'", (burn["id"],))
        finish_burn(conn, burn["id"], "interrupted", words)
        if cache_dir is not None and cache_dir.is_dir():
            for leftover in cache_dir.glob(f"{burn['batch_id']}-*.pcm"):
                try:
                    leftover.unlink()
                except OSError:
                    pass
    return len(burns)
