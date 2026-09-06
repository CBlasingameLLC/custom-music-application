from __future__ import annotations

import sqlite3


def resolve_selection(
    conn: sqlite3.Connection,
    playlist_name: str | None = None,
    tag_filter: str | None = None,
    min_rating: int | None = None,
    select_all: bool = False,
) -> list[sqlite3.Row]:
    """Resolve a sync target set fresh against the live tracks table — never
    cached, so it always reflects the library's current state."""
    modes_selected = sum(bool(x) for x in (playlist_name, tag_filter, min_rating is not None, select_all))
    if modes_selected != 1:
        raise ValueError("Specify exactly one of: playlist, tag_filter, min_rating, or select_all")

    if select_all:
        return conn.execute("SELECT * FROM tracks WHERE is_missing = 0").fetchall()

    if playlist_name:
        return conn.execute(
            """
            SELECT t.* FROM tracks t
            JOIN playlist_tracks pt ON pt.track_id = t.id
            JOIN playlists p ON p.id = pt.playlist_id
            WHERE p.name = ? AND t.is_missing = 0
            ORDER BY pt.position
            """,
            (playlist_name,),
        ).fetchall()

    if tag_filter:
        return conn.execute("SELECT * FROM tracks WHERE is_missing = 0 AND genre = ?", (tag_filter,)).fetchall()

    return conn.execute("SELECT * FROM tracks WHERE is_missing = 0 AND rating >= ?", (min_rating,)).fetchall()
