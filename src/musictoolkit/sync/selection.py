"""Choosing what goes on a device: one or more sources, each a slice of the library, joined together.

A source is a small dict (what the Sync screen stores per device):
    {"kind": "all"}                      every song in the library
    {"kind": "favorites"}                songs marked as favorites
    {"kind": "playlist", "id": 7}        a playlist, manual or smart (a smart one is worked out fresh each time)
    {"kind": "genre", "value": "Rock"}   one genre
    {"kind": "rating", "min": 4}         songs rated at least this many stars
    {"kind": "recent", "days": 30}       songs added in the last N days
Sources are resolved against the live library every time, never cached, and their results are merged without
repeats (a song in two sources is copied once; a playlist's own order is kept)."""

from __future__ import annotations

import sqlite3
from typing import Any

from musictoolkit.web import queries

MAX_SOURCES = 50


class BadSource(ValueError):
    """A source the Sync screen could not have produced."""


def _rules(*rules: dict[str, Any]) -> dict[str, Any]:
    return {"match": "all", "rules": list(rules)}


def _whole(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def validate_source(source: Any) -> None:
    """Raise BadSource unless this is a source the Sync screen could have produced. Looks only at its shape."""
    if not isinstance(source, dict):
        raise BadSource("each source must be an object")
    kind = source.get("kind")
    if kind in ("all", "favorites"):
        return
    if kind == "playlist":
        if not isinstance(source.get("id"), int) or isinstance(source.get("id"), bool):
            raise BadSource("a playlist source needs the playlist's id")
    elif kind == "genre":
        if not isinstance(source.get("value"), str) or not source["value"].strip():
            raise BadSource("a genre source needs a genre")
    elif kind == "rating":
        if not _whole(source.get("min"), 1, 5):
            raise BadSource("a rating source needs a minimum of 1 to 5 stars")
    elif kind == "recent":
        if not _whole(source.get("days"), 1, 3650):
            raise BadSource("a recent source needs a number of days")
    else:
        raise BadSource(f"unknown source {kind!r}")


def validate_sources(sources: Any) -> None:
    if not isinstance(sources, list) or not sources:
        raise BadSource("choose at least one thing to put on the device")
    if len(sources) > MAX_SOURCES:
        raise BadSource("too many sources")
    for source in sources:
        validate_source(source)


def _selection(conn: sqlite3.Connection, source: dict[str, Any]) -> queries.Selection:
    validate_source(source)
    kind = source["kind"]
    if kind == "all":
        return queries.build_selection(conn)
    if kind == "favorites":
        return queries.build_selection(conn, rules=_rules({"field": "favorite", "op": "is", "value": True}))
    if kind == "playlist":
        return queries.build_selection(conn, playlist_id=source["id"], sort="position")
    if kind == "genre":
        return queries.build_selection(conn, rules=_rules({"field": "genre", "op": "is", "value": source["value"].strip()}))
    if kind == "rating":
        return queries.build_selection(conn, rules=_rules({"field": "rating", "op": ">=", "value": source["min"]}))
    return queries.build_selection(conn, rules=_rules({"field": "added_within_days", "op": "is", "value": source["days"]}))


def resolve_sources(conn: sqlite3.Connection, sources: list[dict[str, Any]]) -> list[sqlite3.Row]:
    """The songs the sources add up to, in the order they first appear. Songs whose files are known missing are left out."""
    validate_sources(sources)
    rows: dict[int, sqlite3.Row] = {}
    for source in sources:
        sel = _selection(conn, source)
        extra = ", pt.position AS pos" if sel.playlist_position else ""
        order = "pt.position, pt.rowid" if sel.playlist_position else "t.id"
        for row in conn.execute(f"SELECT t.*{extra} {sel.from_sql} ORDER BY {order}", sel.params):
            rows.setdefault(row["id"], row)
    return list(rows.values())


def playlist_sources(sources: list[dict[str, Any]]) -> list[int]:
    return [s["id"] for s in sources if isinstance(s, dict) and s.get("kind") == "playlist" and isinstance(s.get("id"), int)]
