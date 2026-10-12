"""Opening a station for listening."""

from __future__ import annotations

import requests

from musictoolkit.net import guard
from musictoolkit.net.proxy import StreamError, acceptable_type, open_audio
from musictoolkit.radio import playlists

HEADERS = {"Icy-MetaData": "1", "Accept": "*/*", "Accept-Encoding": "identity"}


def _read_small(response: requests.Response, limit: int) -> bytes:
    pieces, size = [], 0
    try:
        for piece in guard.body_pieces(response, 8192):
            pieces.append(piece)
            size += len(piece)
            if size >= limit:
                break
    except (requests.RequestException, guard.HTTPError, OSError):
        pass
    finally:
        response.close()
    return b"".join(pieces)


def open_station(url: str) -> tuple[requests.Response, str]:
    """The open response of the station's stream (the caller closes it) and its content type.

    An address that turns out to be a playlist file (.pls, .m3u, .asx) is followed to the stream inside it, once. A stream that is not
    audio (a web page, an error) or that this player cannot play (HLS) is a StreamError with a sentence for the person."""
    response, final = open_audio(url, headers=HEADERS)
    content_type = response.headers.get("content-type", "")
    if response.status_code == 200 and playlists.looks_like_a_playlist(content_type, final):
        body = _read_small(response, playlists.MAX_PLAYLIST_BYTES)
        try:
            stream = playlists.first_stream(body, final)
        except playlists.UnplayableStream as exc:
            raise StreamError(str(exc)) from exc
        response, final = open_audio(stream, headers=HEADERS)
        content_type = response.headers.get("content-type", "")
        if response.status_code == 200 and playlists.looks_like_a_playlist(content_type, final):
            response.close()
            raise StreamError("The playlist points at another playlist, which this player does not follow.")
    if response.status_code != 200:
        status = response.status_code
        response.close()
        raise StreamError(f"The station answered with error {status}." if status != 404 else "The station is not at that address any more.")
    if not acceptable_type(content_type):
        response.close()
        raise StreamError("That address is not an audio stream.")
    return response, content_type
