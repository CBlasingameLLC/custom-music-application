"""The calls that give a file or a picture rather than a list: the song itself, a cover, and the words of a song."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

from fastapi import HTTPException
from starlette.responses import Response

from musictoolkit.media import art
from musictoolkit.web import queries
from musictoolkit.web.routers import playback
from musictoolkit.web.subsonic import catalog, ids
from musictoolkit.web.subsonic.dispatch import Call, endpoint
from musictoolkit.web.subsonic.errors import missing, not_found

TRIES = 6  # how many of an artist's albums, or of a playlist's songs, are looked through for a cover before there is said to be none
NO_LANGUAGE = "xxx"  # (what OpenSubsonic says a lyric of unknown language is written in)


# ------------------------------------------------------------------------------------------------------------------------------- the file


@endpoint("stream", "download")
def stream(call: Call) -> Response:
    """The file as it is: nothing is converted, so `format`, `maxBitRate` and `timeOffset` are not used. The client seeks with a Range request, which is answered."""
    return playback.stream(ids.to_song(call.params.require("id")), call.ctx)


# ------------------------------------------------------------------------------------------------------------------------------ the cover


def _size(call: Call) -> int:
    """The smallest size the app keeps covers in that is as big as the client asked for (the biggest, when it asked for no size)."""
    asked = call.params.integer("size", low=1)
    if asked is None:
        return art.THUMBNAIL_SIZES[-1]
    return next((size for size in art.THUMBNAIL_SIZES if size >= asked), art.THUMBNAIL_SIZES[-1])


def _attempts(call: Call, given: str, size: int) -> list[Callable[[], Response]]:
    """Where the cover of `given` can come from, best first: a song or an album has one of its own; an artist has the cover of its first album that has
    one, and a playlist that of its first song that has one."""
    kind, ctx = ids.kind_of(given), call.ctx
    if kind == "song":
        return [partial(playback.track_art, ids.to_song(given), size, ctx)]
    if kind == "album":
        return [partial(playback.album_art, ids.to_album(given), size, ctx)]
    with ctx.db() as conn:
        if kind == "artist":
            rows = catalog.albums(conn, catalog.artist_selection(ids.to_artist(given)), "first_year IS NULL, first_year, LOWER(al)", TRIES, 0)
            return [partial(playback.album_art, queries.album_key(row["aa"], row["al"]), size, ctx) for row in rows]
        sel = catalog.only_playable(queries.build_selection(conn, playlist_id=ids.to_playlist(given), sort="position"))
        rows = conn.execute(f"SELECT t.id {sel.from_sql} ORDER BY {sel.order} LIMIT ?", [*sel.params, TRIES]).fetchall()
        return [partial(playback.track_art, row["id"], size, ctx) for row in rows]


@endpoint("getCoverArt")
def get_cover_art(call: Call) -> Response:
    for attempt in _attempts(call, call.params.require("id"), _size(call)):
        try:
            return attempt()
        except HTTPException as error:
            if error.status_code != 404:  # (the drive is not connected: not "there is no cover")
                raise
    raise not_found("cover art")


# ----------------------------------------------------------------------------------------------------------------------------- the lyrics


def _text_of(found: dict[str, Any]) -> str:
    if found["synced"]:
        return "\n".join(line["text"] for line in found["synced"])
    return found["plain"] or ""


@endpoint("getLyricsBySongId")
def get_lyrics_by_song_id(call: Call) -> dict[str, Any]:
    number = ids.to_song(call.params.require("id"))
    with call.ctx.db() as conn:
        song = catalog.one_song(conn, number, list(call.ctx.config.library.roots))
    found = playback.track_lyrics(number, call.ctx)
    shown = {"displayArtist": song["artist"], "displayTitle": song["title"], "lang": NO_LANGUAGE}
    entries: list[dict[str, Any]] = []
    if found["synced"]:
        entries.append({**shown, "offset": 0, "synced": True, "line": [{"start": round(line["t"] * 1000), "value": line["text"]} for line in found["synced"]]})
    elif found["plain"]:
        entries.append({**shown, "synced": False, "line": [{"value": text} for text in found["plain"].splitlines()]})
    return {"lyricsList": {"structuredLyrics": entries}}


@endpoint("getLyrics")
def get_lyrics(call: Call) -> dict[str, Any]:
    """The older call, by the artist and the title, which answers with the words alone (no times). An answer with nothing in it says there are none."""
    artist, title = call.params.get("artist") or "", call.params.get("title")
    if title is None:
        raise missing("title")
    with call.ctx.db() as conn:
        row = conn.execute(
            "SELECT t.id FROM tracks t WHERE t.is_missing = 0 AND t.no_audio = 0 AND LOWER(COALESCE(t.title, '')) = ? "
            "AND (? = '' OR LOWER(COALESCE(t.artist, '')) = ?) ORDER BY t.id LIMIT 1",
            (title.lower(), artist.lower(), artist.lower()),
        ).fetchone()
    value = _text_of(playback.track_lyrics(row["id"], call.ctx)) if row else ""
    return {"lyrics": {"artist": artist or None, "title": title, "value": value}}
