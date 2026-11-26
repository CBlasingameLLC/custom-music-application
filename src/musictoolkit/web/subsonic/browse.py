"""The calls that look through the library: the artists, an artist's albums, an album's songs, the lists of albums and songs, the genres, and search."""

from __future__ import annotations

import time
from typing import Any

from musictoolkit.web import queries
from musictoolkit.web.queries import ALBUM_ARTIST_SQL, ALBUM_SQL
from musictoolkit.web.subsonic import catalog, ids
from musictoolkit.web.subsonic.catalog import MAX_PAGE
from musictoolkit.web.subsonic.dispatch import Call, endpoint
from musictoolkit.web.subsonic.errors import GENERIC, MISSING, SubsonicError, not_found

EVERYTHING = 5000  # the most songs one call gives (a playlist or an album is never near it)


def _roots(call: Call) -> list[str]:
    return list(call.ctx.config.library.roots)


# -------------------------------------------------------------------------------------------------------------------------- artists


@endpoint("getArtists")
def get_artists(call: Call) -> dict[str, Any]:
    with call.ctx.db() as conn:
        entries = catalog.artists(conn)
    return {"artists": {"ignoredArticles": catalog.IGNORED_ARTICLES, "index": catalog.indexed(entries)}}


@endpoint("getIndexes")
def get_indexes(call: Call) -> dict[str, Any]:
    """The same artists as getArtists, for the clients that browse by folder (each as a plain artist, not an ID3 one)."""
    with call.ctx.db() as conn:
        entries = [{"id": a["id"], "name": a["name"]} for a in catalog.artists(conn)]
    return {"indexes": {"ignoredArticles": catalog.IGNORED_ARTICLES, "lastModified": int(time.time() * 1000), "index": catalog.indexed(entries)}}


@endpoint("getArtist")
def get_artist(call: Call) -> dict[str, Any]:
    given = call.params.require("id")
    name = ids.to_artist(given)
    with call.ctx.db() as conn:
        rows = catalog.albums(conn, catalog.artist_selection(name), "first_year IS NULL, first_year, LOWER(al)", MAX_PAGE, 0)
    if not rows:
        raise not_found("artist")
    return {"artist": {"id": given, "name": name, "albumCount": len(rows), "coverArt": given, "album": [catalog.album(r) for r in rows]}}


# --------------------------------------------------------------------------------------------------------------------- albums and songs


@endpoint("getAlbum")
def get_album(call: Call) -> dict[str, Any]:
    key = ids.to_album(call.params.require("id"))
    with call.ctx.db() as conn:
        row = catalog.album_of(conn, key)
        listed = catalog.songs(conn, catalog.album_selection(key), 0, EVERYTHING, _roots(call))
    return {"album": {**catalog.album(row), "song": listed}}


@endpoint("getSong")
def get_song(call: Call) -> dict[str, Any]:
    number = ids.to_song(call.params.require("id"))
    with call.ctx.db() as conn:
        return {"song": catalog.one_song(conn, number, _roots(call))}


@endpoint("getMusicDirectory")
def get_music_directory(call: Call) -> dict[str, Any]:
    """An artist is a folder of albums, and an album a folder of songs (for the clients that browse that way)."""
    given = call.params.require("id")
    kind = ids.kind_of(given)
    with call.ctx.db() as conn:
        if kind == "artist":
            name = ids.to_artist(given)
            rows = catalog.albums(conn, catalog.artist_selection(name), "first_year IS NULL, first_year, LOWER(al)", MAX_PAGE, 0)
            if not rows:
                raise not_found("artist")
            return {"directory": {"id": given, "name": name, "child": [catalog.directory_of_album(r) for r in rows]}}
        if kind == "album":
            key = ids.to_album(given)
            row = catalog.album_of(conn, key)
            listed = catalog.songs(conn, catalog.album_selection(key), 0, EVERYTHING, _roots(call))
            return {"directory": {"id": given, "parent": ids.artist_id(row["aa"]), "name": row["al"], "child": listed}}
    raise not_found("directory")


@endpoint("getGenres")
def get_genres(call: Call) -> dict[str, Any]:
    with call.ctx.db() as conn:
        rows = conn.execute(
            f"SELECT MIN(t.genre) AS name, COUNT(*) AS songs, COUNT(DISTINCT {ALBUM_ARTIST_SQL} || char(0) || {ALBUM_SQL}) AS albums "
            "FROM tracks t WHERE t.is_missing = 0 AND t.no_audio = 0 AND t.genre IS NOT NULL AND t.genre <> '' GROUP BY LOWER(t.genre) ORDER BY LOWER(name)"
        ).fetchall()
    return {"genres": {"genre": [{"value": r["name"], "songCount": r["songs"], "albumCount": r["albums"]} for r in rows]}}


# ------------------------------------------------------------------------------------------------------------------------------- lists


def _album_list(call: Call, as_directory: bool) -> list[dict[str, Any]]:
    kind = call.params.require("type")
    size = call.params.integer("size", 10, low=1, high=MAX_PAGE)
    offset = call.params.integer("offset", 0, low=0)
    sel = catalog.base()
    having, extra = "", []
    names = {
        "random": "RANDOM()",
        "newest": "newest DESC, LOWER(al)",
        "frequent": "play_total DESC, LOWER(al)",
        "recent": "latest_play DESC, LOWER(al)",
        "starred": "LOWER(al), LOWER(aa)",
        "alphabeticalByName": "LOWER(al), LOWER(aa)",
        "alphabeticalByArtist": "LOWER(aa), LOWER(al)",
        "byYear": "",
        "byGenre": "LOWER(al), LOWER(aa)",
    }
    if kind not in names:
        raise SubsonicError(GENERIC, f"{kind!r} is not a kind of list of albums")
    order = names[kind]
    if kind == "frequent":
        having = "HAVING play_total > 0"
    elif kind == "recent":
        having = "HAVING latest_play IS NOT NULL"
    elif kind == "starred":
        having = "HAVING all_starred = 1"
    elif kind == "byYear":
        first, last = call.params.integer("fromYear"), call.params.integer("toYear")
        if first is None or last is None:
            raise SubsonicError(MISSING, "Required parameter is missing: fromYear and toYear")
        sel.where.append("t.year BETWEEN ? AND ?")
        sel.params.extend([min(first, last), max(first, last)])
        order = "first_year, LOWER(al)" if first <= last else "first_year DESC, LOWER(al)"
    elif kind == "byGenre":
        sel.where.append("LOWER(t.genre) = ?")
        sel.params.append(call.params.require("genre").lower())
    with call.ctx.db() as conn:
        rows = catalog.albums(conn, sel, order, size, offset, having, extra)
    return [catalog.directory_of_album(r) if as_directory else catalog.album(r) for r in rows]


@endpoint("getAlbumList2")
def get_album_list2(call: Call) -> dict[str, Any]:
    return {"albumList2": {"album": _album_list(call, False)}}


@endpoint("getAlbumList")
def get_album_list(call: Call) -> dict[str, Any]:
    return {"albumList": {"album": _album_list(call, True)}}


def _by_year_and_genre(call: Call, sel: Any) -> None:
    genre = call.params.get("genre")
    if genre:
        sel.where.append("LOWER(t.genre) = ?")
        sel.params.append(genre.lower())
    first, last = call.params.integer("fromYear"), call.params.integer("toYear")
    if first is not None:
        sel.where.append("t.year >= ?")
        sel.params.append(first)
    if last is not None:
        sel.where.append("t.year <= ?")
        sel.params.append(last)


@endpoint("getRandomSongs")
def get_random_songs(call: Call) -> dict[str, Any]:
    size = call.params.integer("size", 10, low=1, high=MAX_PAGE)
    sel = catalog.base()
    _by_year_and_genre(call, sel)
    sel.order = "RANDOM()"
    with call.ctx.db() as conn:
        return {"randomSongs": {"song": catalog.songs(conn, sel, 0, size, _roots(call))}}


@endpoint("getSongsByGenre")
def get_songs_by_genre(call: Call) -> dict[str, Any]:
    genre = call.params.require("genre")
    count = call.params.integer("count", 10, low=1, high=MAX_PAGE)
    offset = call.params.integer("offset", 0, low=0)
    sel = catalog.base()
    sel.where.append("LOWER(t.genre) = ?")
    sel.params.append(genre.lower())
    sel.order = queries.ORDER_TAIL
    with call.ctx.db() as conn:
        return {"songsByGenre": {"song": catalog.songs(conn, sel, offset, count, _roots(call))}}


def _starred(call: Call) -> tuple[list[dict[str, Any]], list[Any]]:
    sel = catalog.base()
    sel.where.append("t.favorite = 1")
    sel.order = "LOWER(COALESCE(NULLIF(t.title, ''), t.file_path)), t.id"
    with call.ctx.db() as conn:
        listed = catalog.songs(conn, sel, 0, EVERYTHING, _roots(call))
        rows = catalog.albums(conn, catalog.base(), "LOWER(al), LOWER(aa)", MAX_PAGE, 0, "HAVING all_starred = 1")
    return listed, rows


@endpoint("getStarred2")
def get_starred2(call: Call) -> dict[str, Any]:
    listed, rows = _starred(call)
    return {"starred2": {"album": [catalog.album(r) for r in rows], "song": listed}}


@endpoint("getStarred")
def get_starred(call: Call) -> dict[str, Any]:
    listed, rows = _starred(call)
    return {"starred": {"album": [catalog.directory_of_album(r) for r in rows], "song": listed}}


@endpoint("getNowPlaying")
def get_now_playing(call: Call) -> dict[str, Any]:
    """Nobody is shown as listening: the server does not follow what the apps play (they say it with scrobble, and it goes to the history)."""
    return {"nowPlaying": {"entry": []}}


@endpoint("getTopSongs")
def get_top_songs(call: Call) -> dict[str, Any]:
    name = call.params.require("artist")
    count = call.params.integer("count", 50, low=1, high=MAX_PAGE)
    with call.ctx.db() as conn:
        sel = catalog.only_playable(queries.build_selection(conn, artist=name, sort="plays", direction="desc"))
        return {"topSongs": {"song": catalog.songs(conn, sel, 0, count, _roots(call))}}


# What the library does not know (a biography, who sounds alike): the empty answer the protocol has for it, so that a client goes on without it.
@endpoint("getArtistInfo")
def get_artist_info(call: Call) -> dict[str, Any]:
    return {"artistInfo": {}}


@endpoint("getArtistInfo2")
def get_artist_info2(call: Call) -> dict[str, Any]:
    return {"artistInfo2": {}}


@endpoint("getAlbumInfo", "getAlbumInfo2")
def get_album_info(call: Call) -> dict[str, Any]:
    return {"albumInfo": {}}


@endpoint("getSimilarSongs")
def get_similar_songs(call: Call) -> dict[str, Any]:
    return {"similarSongs": {}}


@endpoint("getSimilarSongs2")
def get_similar_songs2(call: Call) -> dict[str, Any]:
    return {"similarSongs2": {}}


# ------------------------------------------------------------------------------------------------------------------------------ search


def _search(call: Call, as_directory: bool) -> dict[str, Any]:
    """The query is words, all of which a song has to have somewhere in its title, artist, album, genre or year (an empty query is every song, which is how
    some clients read the whole library). Artists and albums are those of the songs that match."""
    query = call.params.get("query") or ""
    counts = {name: call.params.integer(name, 20 if name.endswith("Count") else 0, low=0, high=MAX_PAGE) for name in (
        "artistCount", "artistOffset", "albumCount", "albumOffset", "songCount", "songOffset")}
    roots = _roots(call)
    result: dict[str, Any] = {}
    with call.ctx.db() as conn:
        if counts["artistCount"]:
            sel = catalog.only_playable(queries.build_selection(conn, q=query))
            found = catalog.artists(conn, sel, counts["artistCount"], counts["artistOffset"])
            result["artist"] = [{"id": a["id"], "name": a["name"]} for a in found] if as_directory else found
        if counts["albumCount"]:
            sel = catalog.only_playable(queries.build_selection(conn, q=query))
            rows = catalog.albums(conn, sel, "LOWER(al), LOWER(aa)", counts["albumCount"], counts["albumOffset"])
            result["album"] = [catalog.directory_of_album(r) if as_directory else catalog.album(r) for r in rows]
        if counts["songCount"]:
            sel = catalog.only_playable(queries.build_selection(conn, q=query, sort="plays", direction="desc"))
            result["song"] = catalog.songs(conn, sel, counts["songOffset"], counts["songCount"], roots)
    return result


@endpoint("search3")
def search3(call: Call) -> dict[str, Any]:
    return {"searchResult3": _search(call, False)}


@endpoint("search2")
def search2(call: Call) -> dict[str, Any]:
    """The older call: the same answer, with the albums as folders of songs and the artists as plain names."""
    return {"searchResult2": _search(call, True)}
