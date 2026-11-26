"""The ids a Subsonic client holds. It keeps them as they were given and gives them back, so they are ours to choose.

  a song          the number the library gives it:                    "1234"
  an album        "al-" and the key the library gives an album:       "al-WyJBdXJvcmEiLCJOb3J0aCJd"
  an artist       "ar-" and the name, in base 64 that is URL safe:    "ar-QXVyb3Jh"
  a playlist      "pl-" and its number:                               "pl-7"
"""

from __future__ import annotations

import base64

from musictoolkit.web.subsonic.errors import not_found

ALBUM = "al-"
ARTIST = "ar-"
PLAYLIST = "pl-"


def song_id(track_id: int) -> str:
    return str(track_id)


def album_id(key: str) -> str:
    return ALBUM + key


def artist_id(name: str) -> str:
    return ARTIST + base64.urlsafe_b64encode(name.encode()).rstrip(b"=").decode()


def playlist_id(number: int) -> str:
    return f"{PLAYLIST}{number}"


def _number(text: str) -> bool:
    return text.isascii() and text.isdigit()  # (isdigit alone says yes to the digits of other scripts, which int() would take as well)


def kind_of(given: str) -> str | None:
    """'song', 'album', 'artist' or 'playlist' (what the id is the id of); None for anything that is none of them."""
    if _number(given):
        return "song"
    for prefix, kind in ((ALBUM, "album"), (ARTIST, "artist"), (PLAYLIST, "playlist")):
        if given.startswith(prefix) and len(given) > len(prefix):
            return kind
    return None


def to_song(given: str) -> int:
    if not _number(given):
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
        return base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True).decode()
    except ValueError:  # (not base 64, or not text: both are ValueErrors)
        raise not_found("artist") from None


def to_playlist(given: str) -> int:
    digits = given[len(PLAYLIST):] if given.startswith(PLAYLIST) else ""
    if not _number(digits):
        raise not_found("playlist")
    return int(digits)


__all__ = ["album_id", "artist_id", "kind_of", "playlist_id", "song_id", "to_album", "to_artist", "to_playlist", "to_song"]
