"""The calls that write down what the person thinks of the music or did with it: stars, ratings, and what was listened to."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from musictoolkit.web.queries import Selection
from musictoolkit.web.routers import playback
from musictoolkit.web.subsonic import catalog, ids
from musictoolkit.web.subsonic.dispatch import Call, endpoint
from musictoolkit.web.subsonic.errors import GENERIC, MISSING, SubsonicError, missing, not_found


def _songs_of(conn: sqlite3.Connection, sel: Selection, what: str) -> list[int]:
    rows = conn.execute(f"SELECT t.id {sel.from_sql} ORDER BY t.id", sel.params).fetchall()
    if not rows:
        raise not_found(what)
    return [row["id"] for row in rows]


def _songs_named(call: Call, conn: sqlite3.Connection) -> list[int]:
    """Every song the call names: by `id` (a song; or an album or an artist, which is all of its songs: the clients that browse by folder star those), by
    `albumId`, by `artistId`. A name that is nobody's fails the whole call, before anything is changed."""
    numbers: list[int] = []
    for given in call.params.getlist("id"):
        kind = ids.kind_of(given)
        if kind == "song":
            number = ids.to_song(given)
            if number not in catalog.playable(conn, [number]):
                raise not_found("song")
            numbers.append(number)
        elif kind == "album":
            numbers.extend(_songs_of(conn, catalog.album_selection(ids.to_album(given)), "album"))
        elif kind == "artist":
            numbers.extend(_songs_of(conn, catalog.artist_selection(ids.to_artist(given)), "artist"))
        else:
            raise not_found()
    for given in call.params.getlist("albumId"):
        numbers.extend(_songs_of(conn, catalog.album_selection(ids.to_album(given)), "album"))
    for given in call.params.getlist("artistId"):
        numbers.extend(_songs_of(conn, catalog.artist_selection(ids.to_artist(given)), "artist"))
    if not numbers:
        raise SubsonicError(MISSING, "Required parameter is missing: id, albumId or artistId")
    return list(dict.fromkeys(numbers))


def _write(conn: sqlite3.Connection, column: str, value: Any, numbers: Sequence[int]) -> None:
    for start in range(0, len(numbers), catalog.CHUNK):
        chunk = numbers[start:start + catalog.CHUNK]
        conn.execute(f"UPDATE tracks SET {column} = ? WHERE id IN ({', '.join('?' * len(chunk))})", [value, *chunk])
    conn.commit()


@endpoint("star")
def star(call: Call) -> dict[str, Any]:
    with call.ctx.db() as conn:
        _write(conn, "favorite", 1, _songs_named(call, conn))
    return {}


@endpoint("unstar")
def unstar(call: Call) -> dict[str, Any]:
    with call.ctx.db() as conn:
        _write(conn, "favorite", 0, _songs_named(call, conn))
    return {}


@endpoint("setRating")
def set_rating(call: Call) -> dict[str, Any]:
    """Songs only: the library keeps a rating for each song, and rating an album would write over the ratings of its songs. 0 takes the rating away."""
    number = ids.to_song(call.params.require("id"))
    rating = call.params.integer("rating")
    if rating is None:
        raise missing("rating")
    if not 0 <= rating <= 5:
        raise SubsonicError(GENERIC, "rating is from 1 to 5, or 0 to take it away")
    with call.ctx.db() as conn:
        if number not in catalog.playable(conn, [number]):
            raise not_found("song")
        _write(conn, "rating", rating or None, [number])
    return {}


# --------------------------------------------------------------------------------------------------------------------------------- listens


def _times(call: Call, count: int) -> list[int | None]:
    """When each song was listened to, in the order of the songs, in milliseconds since 1970 as the protocol writes it (None: the client did not say)."""
    given = call.params.getlist("time")
    out: list[int | None] = []
    for position in range(count):
        try:
            out.append(int(given[position]) if position < len(given) else None)
        except ValueError:
            raise SubsonicError(GENERIC, "time is not a whole number of milliseconds") from None
    return out


def _written(conn: sqlite3.Connection, number: int, second: int) -> bool:
    """Is this listen in the history already? A client that did not hear the server's answer sends the listen again, with the same time on it: one listen, not two."""
    row = conn.execute("SELECT 1 FROM play_history WHERE track_id = ? AND source = 'future_scrobble' AND played_at_epoch = ?", (number, second)).fetchone()
    return row is not None


@endpoint("scrobble")
def scrobble(call: Call) -> dict[str, Any]:
    """`submission` true (the default): the song was listened to, and goes into the history (and on to ListenBrainz, if the person turned that on). False: it
    is playing now, which is only told to ListenBrainz. How long it was listened to is not in the protocol: a client sends this once it has counted the
    listen, so the whole song is taken."""
    numbers = [ids.to_song(given) for given in call.params.getlist("id")]
    if not numbers:
        raise missing("id")
    times = _times(call, len(numbers))
    submission = call.params.boolean("submission", True)
    with call.ctx.db() as conn:
        playable = catalog.playable(conn, numbers)
        if len(playable) != len(set(numbers)):  # (nothing is written unless every song is known)
            raise not_found("song")
        due = [(number, moment) for number, moment in zip(numbers, times) if not (submission and moment is not None and _written(conn, number, moment // 1000))]
    for number, moment in due:
        if submission:
            playback.record_play(call.ctx, number, round(playable[number] * 1000), None if moment is None else moment / 1000)
        else:
            playback.announce_now_playing(call.ctx, number)
    return {}
