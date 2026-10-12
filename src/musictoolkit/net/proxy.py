"""Passing an audio stream from another server on to the page, through this app.

The page may only fetch from its own origin, and the sound graph (equalizer, analyser, crossfade) needs audio that comes from it, so a
station or an episode is played through the app: the page asks for /api/radio/stations/3/stream, the app (which knows the real address, and
checks it) fetches that and hands the bytes on. It is never an open relay: the address always comes from the database, never from the page.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from typing import Any

import requests
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from musictoolkit.net import guard

AUDIO_TYPES_OK = {"application/ogg", "application/octet-stream", "binary/octet-stream", "video/ogg", ""}
READ_SIZE = 16384


class StreamError(Exception):
    """The address cannot be played; the message says why in words a person can read."""


def acceptable_type(content_type: str) -> bool:
    kind = content_type.split(";")[0].strip().lower()
    return kind.startswith("audio/") or kind in AUDIO_TYPES_OK


class Slots:
    """At most `size` streams at once, so that a page gone wrong cannot hold open every connection the app has."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._used = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self._used >= self.size:
                return False
            self._used += 1
            return True

    def give_back(self) -> None:
        with self._lock:
            self._used = max(0, self._used - 1)

    @property
    def used(self) -> int:
        return self._used


def _hang_up(upstream: requests.Response) -> None:
    """Close the connection to the server, waking a read that another thread is blocked in."""
    try:
        shutdown = getattr(upstream.raw, "shutdown", None)
        if shutdown is not None:
            shutdown()
    except (OSError, ValueError, RuntimeError, NotImplementedError):  # RuntimeError: the connection is already back in the pool (the body was all read)
        pass
    try:
        upstream.close()
    except (OSError, requests.RequestException):
        pass


def relay(
    upstream: requests.Response,
    *,
    status_code: int = 200,
    media_type: str = "audio/mpeg",
    headers: dict[str, str] | None = None,
    transform: Callable[[bytes], bytes] | None = None,
    skip: int = 0,
    done: Callable[[], None] | None = None,
) -> StreamingResponse:
    """Hand `upstream`'s body to the page as it arrives. `transform` changes each piece (the ICY demuxer removes the title blocks),
    `skip` drops that many bytes first (a server that ignored a Range request), and `done` runs when the stream ends for any reason."""
    pieces: Iterator[bytes] = guard.body_pieces(upstream, READ_SIZE)

    def next_piece() -> bytes | None:
        nonlocal skip
        while True:
            try:
                piece = next(pieces, None)
            except (requests.RequestException, guard.HTTPError, OSError, ValueError):
                return None
            if piece is None:
                return None
            if skip:
                dropped = min(skip, len(piece))
                skip -= dropped
                piece = piece[dropped:]
            if transform is not None:
                piece = transform(piece)
            if piece:
                return piece

    async def body():
        try:
            while True:
                piece = await run_in_threadpool(next_piece)
                if piece is None:
                    return
                yield piece
        finally:
            _hang_up(upstream)
            if done is not None:
                done()

    return StreamingResponse(body(), status_code=status_code, media_type=media_type, headers={"Cache-Control": "no-store", **(headers or {})})


def open_audio(url: str, *, headers: dict[str, str]) -> tuple[requests.Response, str]:
    """Open `url` for streaming; the server's refusal or silence is turned into a StreamError."""
    try:
        return guard.open_stream(url, headers=headers)
    except guard.FetchError as exc:
        raise StreamError(str(exc)) from exc
