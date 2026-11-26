"""The ids a Subsonic client holds. It keeps them as they were given and gives them back, so they are ours to choose.

  a song          the number the library gives it:                    "1234"
  an album        "al-" and the key the library gives an album:       "al-WyJBdXJvcmEiLCJOb3J0aCJd"
  an artist       "ar-" and the name, in base 64 that is URL safe:    "ar-QXVyb3Jh"
  a playlist      "pl-" and its number:                               "pl-7"
"""

from __future__ import annotations

import base64

from musictoolkit.web.subsonic.errors import SubsonicError, not_found

ALBUM = "al-"
ARTIST = "ar-"
PLAYLIST = "pl-"


def song_id(track_id: int) -> str:
    return str(track_id)


def album_id(key: str) -> str:
    return ALBUM + key


def artist_id(name: str) -> str:
    return ARTIST + base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")


def playlist_id(number: int) -> str:
    return f"{PLAYLIST}{number}"


def kind_of(given: str) -> str:
    """'song', 'album', 'artist' or 'playlist' (what the id is the id of); NOT_FOUND for anything that is none of them."""
    if given.isascii() and given.isdigit():
        return "song"
    for prefix, kind in ((ALBUM, "album"), (ARTIST, "artist"), (PLAYLIST, "playlist")):
        if given.startswith(prefix) and len(given) > len(prefix):
            return kind
    raise not_found()


def to_song(given: str) -> int:
    if kind_of(given) != "song":
        raise not_found("song")
    return int(given)


def to_album(given: str) -> str:
    if kind_of(given) != "album":
        raise not_found("album")
    return given[len(ALBUM):]


def to_artist(given: str) -> str:
    if kind_of(given) != "artist":
        raise not_found("artist")
    body = given[len(ARTIST):].rstrip("=")  # (a client may give the padding back)
    try:
        return base64.b64decode((body + "=" * (-len(body) % 4)).encode("ascii"), altchars=b"-_", validate=True).decode("utf-8")
    except Exception:  # (not base 64, or not text)
        raise not_found("artist") from None


def to_playlist(given: str) -> int:
    if kind_of(given) != "playlist":
        raise not_found("playlist")
    digits = given[len(PLAYLIST):]
    if not (digits.isascii() and digits.isdigit()):
        raise not_found("playlist")
    return int(digits)


__all__ = [
    "SubsonicError", "album_id", "artist_id", "kind_of", "playlist_id", "song_id", "to_album", "to_artist", "to_playlist", "to_song",
]
