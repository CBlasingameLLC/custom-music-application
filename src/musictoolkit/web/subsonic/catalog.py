"""The library as Subsonic describes it: songs, albums and artists, built with the same selections and the same names the rest of the app uses
(so an album here is the album there), and leaving out the files that are only tags (there is nothing in them to play)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from musictoolkit.web import queries
from musictoolkit.web.queries import (
    ALBUM_ARTIST_SQL,
    ALBUM_SQL,
    TRACK_COLUMNS,
    UNKNOWN_ALBUM,
    UNKNOWN_ARTIST,
    Selection,
    album_key,
)
from musictoolkit.web.routers.playback import MEDIA_TYPES
from musictoolkit.web.subsonic import ids
from musictoolkit.web.subsonic.errors import SubsonicError, not_found

SONG_COLUMNS = TRACK_COLUMNS + ", t.file_size, t.musicbrainz_recording_id"
EPOCH = "1970-01-01T00:00:00.000Z"
IGNORED_ARTICLES = "The El La Los Las Le Les"
MAX_PAGE = 500  # what the protocol allows for one page of anything
CHUNK = 500  # how many songs one statement names (SQLite limits the number of marks in one)


def iso(value: Any) -> str | None:
    """A time as Subsonic writes one (UTC, to the millisecond, a Z at the end), from what the library keeps (an ISO text, or seconds since 1970)."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(value, timezone.utc)
        else:
            moment = datetime.fromisoformat(str(value))  # (a Z at the end is read as UTC, from Python 3.11)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def base() -> Selection:
    """Every song that can be played: not missing, and with audio in it."""
    return Selection(where=["t.is_missing = 0", "t.no_audio = 0"])


def only_playable(sel: Selection) -> Selection:
    sel.where.append("t.no_audio = 0")
    return sel


def _relative(path: str, roots: Sequence[str]) -> str:
    for root in roots:
        try:
            return Path(path).relative_to(root).as_posix()
        except ValueError:
            continue
    return Path(path).name


# ------------------------------------------------------------------------------------------------------------------------------ songs


def song(row: sqlite3.Row, roots: Sequence[str]) -> dict[str, Any]:
    path = row["file_path"]
    artist = row["artist"] or row["album_artist"] or UNKNOWN_ARTIST
    album_artist = row["album_artist"] or row["artist"] or UNKNOWN_ARTIST
    album = row["album"] or UNKNOWN_ALBUM
    album_id = ids.album_id(album_key(album_artist, album))
    suffix = Path(path).suffix.lower()[1:]  # (without the dot)
    created = iso(row["date_added"]) or EPOCH
    bitrate = row["bitrate"]
    return {
        "id": ids.song_id(row["id"]),
        "parent": album_id,
        "isDir": False,
        "title": row["title"] or Path(path).stem,
        "album": album,
        "artist": artist,
        "track": row["track_number"] or None,
        "year": row["year"] or None,
        "genre": row["genre"] or None,
        "coverArt": album_id,
        "size": row["file_size"],
        "contentType": MEDIA_TYPES.get("." + suffix, "application/octet-stream"),
        "suffix": suffix or None,
        "duration": int(round(row["duration_seconds"] or 0)),
        "bitRate": round(bitrate / 1000) if bitrate else None,
        "path": _relative(path, roots),
        "isVideo": False,
        "userRating": row["rating"] or None,
        "playCount": row["plays"] or 0,
        "discNumber": row["disc_number"] or None,
        "created": created,
        "starred": created if row["favorite"] else None,
        "albumId": album_id,
        "artistId": ids.artist_id(album_artist),
        "type": "music",
        "mediaType": "song",
        "played": iso(row["last_played"]),
        "displayArtist": artist,
        "displayAlbumArtist": album_artist,
        "musicBrainzId": row["musicbrainz_recording_id"] or None,
        "genres": [{"name": row["genre"]}] if row["genre"] else None,
    }


def songs(conn: sqlite3.Connection, sel: Selection, offset: int, limit: int, roots: Sequence[str]) -> list[dict[str, Any]]:
    """One page of a selection, in the selection's order."""
    extra = ", pt.position AS pos" if sel.playlist_position else ""
    rows = conn.execute(
        f"SELECT {SONG_COLUMNS}{extra} {sel.from_sql} ORDER BY {sel.order} LIMIT ? OFFSET ?", [*sel.params, limit, offset]
    ).fetchall()
    return [song(row, roots) for row in rows]


def one_song(conn: sqlite3.Connection, number: int, roots: Sequence[str]) -> dict[str, Any]:
    row = conn.execute(f"SELECT {SONG_COLUMNS} FROM tracks t {queries.HISTORY_JOIN} WHERE t.is_missing = 0 AND t.no_audio = 0 AND t.id = ?", (number,)).fetchone()
    if row is None:
        raise not_found("song")
    return song(row, roots)


def playable(conn: sqlite3.Connection, numbers: Sequence[int]) -> dict[int, float]:
    """The songs among these that the library has and that can be played, each with how long it is, in seconds (a client may name the same one twice)."""
    unique = list(dict.fromkeys(numbers))
    found: dict[int, float] = {}
    for start in range(0, len(unique), CHUNK):
        chunk = unique[start:start + CHUNK]
        rows = conn.execute(
            f"SELECT t.id, t.duration_seconds FROM tracks t WHERE t.is_missing = 0 AND t.no_audio = 0 AND t.id IN ({', '.join('?' * len(chunk))})", chunk
        ).fetchall()
        found.update((row["id"], row["duration_seconds"] or 0) for row in rows)
    return found


# ------------------------------------------------------------------------------------------------------------------------ albums and artists

# The names of the totals are not the names of columns of the tables they are made from: in a HAVING, SQLite reads such a name as the column (of one row of the group).
ALBUM_AGGREGATE = (
    f"SELECT {ALBUM_ARTIST_SQL} AS aa, {ALBUM_SQL} AS al, MIN(t.year) AS first_year, COUNT(*) AS n, SUM(t.duration_seconds) AS dur, "
    "MIN(t.date_added) AS added, MAX(t.date_added) AS newest, SUM(COALESCE(h.plays, 0)) AS play_total, MAX(h.last_played) AS latest_play, "
    "MIN(NULLIF(t.genre, '')) AS main_genre, MIN(t.favorite) AS all_starred, MIN(t.id) AS first_id "
)


def album(row: sqlite3.Row) -> dict[str, Any]:
    key = ids.album_id(album_key(row["aa"], row["al"]))
    created = iso(row["added"]) or EPOCH
    return {
        "id": key,
        "name": row["al"],
        "artist": row["aa"],
        "artistId": ids.artist_id(row["aa"]),
        "coverArt": key,
        "songCount": row["n"],
        "duration": int(round(row["dur"] or 0)),
        "playCount": row["play_total"] or 0,
        "created": created,
        "starred": created if row["all_starred"] else None,
        "year": row["first_year"] or None,
        "genre": row["main_genre"],
        "played": iso(row["latest_play"]),
        "displayArtist": row["aa"],
        "mediaType": "album",
    }


def albums(conn: sqlite3.Connection, sel: Selection, order: str, limit: int, offset: int, having: str = "", extra: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(
        f"{ALBUM_AGGREGATE}{sel.from_sql} GROUP BY aa, al {having} ORDER BY {order} LIMIT ? OFFSET ?", [*sel.params, *extra, limit, offset]
    ).fetchall()


def directory_of_album(row: sqlite3.Row) -> dict[str, Any]:
    """An album as the older calls (getAlbumList, getMusicDirectory) show it: a folder of songs, with the artist as its parent."""
    full = album(row)
    return {
        "id": full["id"], "parent": full["artistId"], "isDir": True, "title": full["name"], "album": full["name"], "artist": full["artist"],
        "year": full["year"], "genre": full["genre"], "coverArt": full["coverArt"], "playCount": full["playCount"], "created": full["created"],
        "starred": full["starred"], "artistId": full["artistId"],
    }


def artist_selection(name: str) -> Selection:
    sel = base()
    sel.where.append(f"{ALBUM_ARTIST_SQL} = ?")
    sel.params.append(name)
    return sel


def album_selection(key: str) -> Selection:
    """The songs of an album, by the key the library gave it (a key that is not one is an album that is not there)."""
    try:
        sel = only_playable(queries.build_selection(None, album=key))  # type: ignore[arg-type]
    except HTTPException:  # (a key that is not one)
        raise not_found("album") from None
    return sel


def artists(conn: sqlite3.Connection, sel: Selection | None = None, limit: int = -1, offset: int = 0) -> list[dict[str, Any]]:
    """The album artists of a selection, in order of name (all of them unless `limit` says how many to give, after skipping `offset`)."""
    sel = sel or base()
    rows = conn.execute(
        f"SELECT {ALBUM_ARTIST_SQL} AS name, COUNT(DISTINCT {ALBUM_SQL}) AS albums {sel.from_sql} GROUP BY name ORDER BY LOWER(name) LIMIT ? OFFSET ?",
        [*sel.params, limit, offset],
    ).fetchall()
    return [artist(row) for row in rows]


def artist(row: sqlite3.Row) -> dict[str, Any]:
    identifier = ids.artist_id(row["name"])
    return {"id": identifier, "name": row["name"], "albumCount": row["albums"], "coverArt": identifier}


def _sort_key(name: str) -> tuple[str, str]:
    """How the names of artists are put in order: lower case, and without a leading article (The Beatles under B)."""
    lowered = name.strip().lower()
    for article in IGNORED_ARTICLES.lower().split():
        if lowered.startswith(article + " "):  # (what is left after the article and its space: there is some, as the name was stripped)
            return lowered[len(article) + 1:], lowered
    return lowered, lowered


def index_letter(name: str) -> str:
    first = _sort_key(name)[0][:1].upper()
    return first if first.isalpha() else "#"


def indexed(entries: list[dict[str, Any]], member: str = "artist") -> list[dict[str, Any]]:
    """The artists as Subsonic lists them: a section for each first letter (# for those that start with something else), in order."""
    sections: dict[str, list[dict[str, Any]]] = {}
    for entry in sorted(entries, key=lambda e: _sort_key(e["name"])):
        sections.setdefault(index_letter(entry["name"]), []).append(entry)
    letters = sorted(sections, key=lambda letter: (letter == "#", letter))
    return [{"name": letter, member: sections[letter]} for letter in letters]


def album_of(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    """The aggregate row of one album, or the error that there is none."""
    sel = album_selection(key)
    found = albums(conn, sel, "aa", MAX_PAGE, 0)  # (one row: the selection is of one album)
    if not found:
        raise not_found("album")
    return found[0]


__all__ = ["SubsonicError", "base", "iso"]
