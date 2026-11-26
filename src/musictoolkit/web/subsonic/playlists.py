"""The playlists. A manual one (the songs, in an order the person chose) can be made, changed and deleted from a client of this protocol. A smart one fills itself
from rules, and so can be listened to and deleted, not changed song by song: that is what error 50, "not authorized", says for it."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from musictoolkit.web import queries
from musictoolkit.web.routers.playlists import MAX_TRACKS, ordered_ids, write_order
from musictoolkit.web.subsonic import catalog, ids
from musictoolkit.web.subsonic.dispatch import Call, endpoint
from musictoolkit.web.subsonic.errors import GENERIC, NOT_AUTHORIZED, SubsonicError, missing, not_found

MAX_NAME = 200  # (what the app allows a playlist's name to be)


def _selection(conn: sqlite3.Connection, number: int) -> queries.Selection:
    """The playable songs of a playlist, in its own order."""
    return catalog.only_playable(queries.build_selection(conn, playlist_id=number, sort="position"))


def _row(conn: sqlite3.Connection, given: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM playlists WHERE id = ?", (ids.to_playlist(given),)).fetchone()
    if row is None:
        raise not_found("playlist")
    return row


def _manual(row: sqlite3.Row) -> None:
    if row["kind"] != "manual":
        raise SubsonicError(NOT_AUTHORIZED, "A smart playlist fills itself from its rules: its songs cannot be changed")


def _name(value: str | None) -> str:
    name = (value or "").strip()
    if not name:
        raise missing("name")
    if len(name) > MAX_NAME:
        raise SubsonicError(GENERIC, f"A playlist's name can be at most {MAX_NAME} characters")
    return name


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _playlist(conn: sqlite3.Connection, row: sqlite3.Row, user: str, sel: queries.Selection) -> dict[str, Any]:
    totals = conn.execute(f"SELECT COUNT(*) AS n, COALESCE(SUM(t.duration_seconds), 0) AS dur {sel.from_sql}", sel.params).fetchone()
    identifier = ids.playlist_id(row["id"])
    created = catalog.iso(row["created_at"]) or catalog.EPOCH
    return {
        "id": identifier, "name": row["name"] or f"Playlist {row['id']}", "owner": user, "public": False, "songCount": totals["n"],
        "duration": int(round(totals["dur"])), "created": created, "changed": catalog.iso(row["updated_at"]) or created, "coverArt": identifier,
        "readonly": True if row["kind"] == "smart" else None,
    }


def _with_songs(call: Call, conn: sqlite3.Connection, number: int) -> dict[str, Any]:
    row = _row(conn, ids.playlist_id(number))
    sel = _selection(conn, number)
    return {"playlist": {**_playlist(conn, row, call.user, sel), "entry": catalog.songs(conn, sel, 0, MAX_TRACKS, list(call.ctx.config.library.roots))}}


@endpoint("getPlaylists")
def get_playlists(call: Call) -> dict[str, Any]:
    """Every playlist (there is one person, so `username` is not looked at)."""
    with call.ctx.db() as conn:
        rows = conn.execute("SELECT * FROM playlists ORDER BY LOWER(name), id").fetchall()
        return {"playlists": {"playlist": [_playlist(conn, row, call.user, _selection(conn, row["id"])) for row in rows]}}


@endpoint("getPlaylist")
def get_playlist(call: Call) -> dict[str, Any]:
    with call.ctx.db() as conn:
        return _with_songs(call, conn, _row(conn, call.params.require("id"))["id"])


def _known_songs(conn: sqlite3.Connection, given: list[str]) -> list[int]:
    """The songs named, as numbers, in the order named; a name that is no song of the library fails the call before anything is changed."""
    numbers = [ids.to_song(each) for each in given]
    if len(catalog.playable(conn, numbers)) != len(set(numbers)):
        raise not_found("song")
    return numbers


@endpoint("createPlaylist")
def create_playlist(call: Call) -> dict[str, Any]:
    """With `name`, a new playlist of the songs (`songId`, as many as there are). With `playlistId`, the songs of that playlist are replaced by them (and
    it is renamed, if `name` came too). The answer is the playlist."""
    given = call.params.get("playlistId")
    with call.ctx.db() as conn:
        numbers = _known_songs(conn, call.params.getlist("songId"))
        if given is None:
            name, now = _name(call.params.get("name")), _stamp()
            number = conn.execute(
                "INSERT INTO playlists (name, source, kind, rules_json, created_at, updated_at) VALUES (?, 'manual', 'manual', NULL, ?, ?)", (name, now, now)
            ).lastrowid
        else:
            row = _row(conn, given)
            _manual(row)
            number = row["id"]
            if call.params.get("name") is not None:
                conn.execute("UPDATE playlists SET name = ? WHERE id = ?", (_name(call.params.get("name")), number))
        write_order(conn, number, numbers)
        conn.commit()
        return _with_songs(call, conn, number)


def _positions(call: Call, name: str) -> list[int]:
    out = []
    for value in call.params.getlist(name):
        try:
            out.append(int(value))
        except ValueError:
            raise SubsonicError(GENERIC, f"{name} is not a whole number") from None
    return out


@endpoint("updatePlaylist")
def update_playlist(call: Call) -> dict[str, Any]:
    """Rename it (`name`), take songs out (`songIndexToRemove`: their places in the list the client was shown), then put songs at the end (`songIdToAdd`). A
    comment, and whether it is public, are not kept: the playlist has no comment, and there is one person to see it."""
    with call.ctx.db() as conn:
        row = _row(conn, call.params.require("playlistId"))
        number = row["id"]
        adding, removing = call.params.getlist("songIdToAdd"), _positions(call, "songIndexToRemove")
        name = call.params.get("name")
        if adding or removing:
            _manual(row)
        added = _known_songs(conn, adding)
        if name is not None:
            conn.execute("UPDATE playlists SET name = ?, updated_at = ? WHERE id = ?", (_name(name), _stamp(), number))
        if adding or removing:
            current = ordered_ids(conn, number)
            playable = catalog.playable(conn, current)
            shown = [place for place, track in enumerate(current) if track in playable]  # (a song that is gone from the library is not in what the client saw)
            gone = {shown[place] for place in removing if 0 <= place < len(shown)}
            write_order(conn, number, [track for place, track in enumerate(current) if place not in gone] + added)
        conn.commit()
    return {}


@endpoint("deletePlaylist")
def delete_playlist(call: Call) -> dict[str, Any]:
    with call.ctx.db() as conn:
        number = _row(conn, call.params.require("id"))["id"]
        conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (number,))
        conn.execute("DELETE FROM playlists WHERE id = ?", (number,))
        conn.commit()
    return {}

