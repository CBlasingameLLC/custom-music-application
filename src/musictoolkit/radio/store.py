"""The radio stations the person has kept (favorites, and the ones played lately)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

MAX_NAME = 200
MAX_TAGS = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _tags(value: Any) -> str:
    """Tags as the comma-separated text kept in the database: lower case, no repeats, no commas inside one, a few of them."""
    if isinstance(value, str):
        value = value.split(",")
    seen: list[str] = []
    for tag in value or []:
        tag = " ".join(str(tag).replace(",", " ").split()).lower()[:40]
        if tag and tag not in seen:
            seen.append(tag)
    return ",".join(seen[:MAX_TAGS])


def station_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": "station",
        "name": row["name"],
        "stream_url": row["stream_url"],
        "homepage": row["homepage"],
        "has_art": bool(row["art_url"]),
        "tags": [t for t in (row["tags"] or "").split(",") if t],
        "country": row["country"],
        "country_code": row["country_code"],
        "language": row["language"],
        "codec": row["codec"],
        "bitrate": row["bitrate"],
        "favorite": bool(row["favorite"]),
        "source": row["source"],
        "added_at": row["added_at"],
        "last_played_at": row["last_played_at"],
        "play_count": row["play_count"],
    }


def add_station(
    conn: sqlite3.Connection,
    *,
    name: str,
    stream_url: str,
    source: str = "manual",
    source_id: str | None = None,
    homepage: str | None = None,
    art_url: str | None = None,
    tags: Any = (),
    country: str | None = None,
    country_code: str | None = None,
    language: str | None = None,
    codec: str | None = None,
    bitrate: int | None = None,
    favorite: bool | None = None,
) -> dict[str, Any]:
    """Keep a station. One that is already kept (same stream address) has its details refreshed, and keeps its favorite mark and plays."""
    name = " ".join(str(name).split())[:MAX_NAME] or stream_url
    existing = conn.execute("SELECT id FROM radio_stations WHERE stream_url = ?", (stream_url,)).fetchone()
    values = (
        source, source_id, name, homepage or None, art_url or None, _tags(tags), country or None, (country_code or "").upper()[:2] or None,
        language or None, codec or None, int(bitrate) if bitrate else None,
    )
    if existing is None:
        cursor = conn.execute(
            "INSERT INTO radio_stations (source, source_id, name, homepage, art_url, tags, country, country_code, language, codec, bitrate,"
            " stream_url, favorite, added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*values, stream_url, 1 if favorite else 0, _now()),
        )
        station_id = cursor.lastrowid
    else:
        station_id = existing["id"]
        conn.execute(
            "UPDATE radio_stations SET source=?, source_id=?, name=?, homepage=COALESCE(?, homepage), art_url=COALESCE(?, art_url), tags=?,"
            " country=COALESCE(?, country), country_code=COALESCE(?, country_code), language=COALESCE(?, language), codec=COALESCE(?, codec),"
            " bitrate=COALESCE(?, bitrate) WHERE id=?",
            (*values, station_id),
        )
        if favorite is not None:
            conn.execute("UPDATE radio_stations SET favorite=? WHERE id=?", (1 if favorite else 0, station_id))
    conn.commit()
    return station_json(conn.execute("SELECT * FROM radio_stations WHERE id = ?", (station_id,)).fetchone())


def get_station(conn: sqlite3.Connection, station_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM radio_stations WHERE id = ?", (station_id,)).fetchone()
    return station_json(row) if row else None


def station_row(conn: sqlite3.Connection, station_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM radio_stations WHERE id = ?", (station_id,)).fetchone()


def list_stations(conn: sqlite3.Connection, scope: str = "all", limit: int = 500) -> list[dict[str, Any]]:
    """`favorites` (by name), `recent` (the ones played, latest first) or `all` (favorites first, then by name)."""
    if scope == "favorites":
        sql = "SELECT * FROM radio_stations WHERE favorite = 1 ORDER BY name COLLATE NOCASE"
    elif scope == "recent":
        sql = "SELECT * FROM radio_stations WHERE last_played_at IS NOT NULL ORDER BY last_played_at DESC, id DESC"
    else:
        sql = "SELECT * FROM radio_stations ORDER BY favorite DESC, name COLLATE NOCASE"
    return [station_json(row) for row in conn.execute(sql + " LIMIT ?", (limit,)).fetchall()]


def update_station(conn: sqlite3.Connection, station_id: int, *, name: str | None = None, favorite: bool | None = None) -> dict[str, Any] | None:
    if station_row(conn, station_id) is None:
        return None
    if name is not None:
        cleaned = " ".join(name.split())[:MAX_NAME]
        if cleaned:
            conn.execute("UPDATE radio_stations SET name = ? WHERE id = ?", (cleaned, station_id))
    if favorite is not None:
        conn.execute("UPDATE radio_stations SET favorite = ? WHERE id = ?", (1 if favorite else 0, station_id))
    conn.commit()
    return get_station(conn, station_id)


def delete_station(conn: sqlite3.Connection, station_id: int) -> bool:
    cursor = conn.execute("DELETE FROM radio_stations WHERE id = ?", (station_id,))
    conn.commit()
    return cursor.rowcount > 0


def record_play(conn: sqlite3.Connection, station_id: int) -> None:
    conn.execute("UPDATE radio_stations SET last_played_at = ?, play_count = play_count + 1 WHERE id = ?", (_now(), station_id))
    conn.commit()
