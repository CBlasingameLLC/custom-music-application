"""The answer to a request for a song at a lower bitrate: the kept file when there is one, the song as it is made when there is not, and, when that cannot be
done, nothing: the caller then sends the original, so that the listener has the song whatever went wrong (see transcode.py).

What a listener meets, by what it asks for:

- a song that was made before is a plain file, which answers a Range request like any other;
- a song that is not made yet is handed out as it is made, and ffmpeg is several times quicker than the song plays, so it starts at once. The answer has no
  length (it is not known until the song is whole), unless the client asked for one to be guessed (`estimateContentLength`), and it says that it cannot be asked
  for a part (`Accept-Ranges: none`);
- a request for a part of it (Safari asks for the first byte or two, before it plays anything, and a seek asks for the middle) is held until the song is whole,
  which takes the seconds that ffmpeg needs, and then answered as a part of the kept file with its real length;
- a request for a song from a time (`timeOffset`) is made for that listener alone, from that time, and not kept;
- a request that only asks what the answer would be (HEAD) starts nothing.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from pathlib import Path

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, Response, StreamingResponse

from musictoolkit import transcode
from musictoolkit.transcode import MEDIA_TYPE, Plan, TranscodeError, Transcoder

KEPT = {"Cache-Control": "private, max-age=3600"}
LIVE = {"Accept-Ranges": "none", "Cache-Control": "no-store"}


def from_the_start(range_header: str) -> bool:
    """Is this a request for the whole of it, from the first byte (what Chrome and Firefox begin with)? It can be answered at once, with a 200."""
    return range_header.strip().lower().replace(" ", "") == "bytes=0-"


def _head() -> Response:
    """What a HEAD request is told about a song that is not made yet: nothing but what it will be (a stream that cannot be asked for a part)."""
    return StreamingResponse(iter(()), media_type=MEDIA_TYPE, headers=LIVE)


def _promised(body: Iterator[bytes], headers: dict[str, str], duration: float | None, kbps: int, estimate: bool) -> tuple[Iterator[bytes], dict[str, str]]:
    """The body and headers with the length that a client asked to be guessed (`estimateContentLength`), when it did and the length of the song is known."""
    if not estimate or not duration or duration <= 0:
        return body, headers
    size = transcode.estimated_bytes(duration, kbps)
    return transcode.exact_length(body, size), {**headers, "Content-Length": str(size)}


def _from_a_time(transcoder: Transcoder, path: Path, plan: Plan, duration: float | None, offset: float, estimate: bool) -> Response | None:
    pieces = transcoder.direct(path, plan.kbps, offset)
    try:
        first = next(pieces)  # (it is known that it began before the answer is begun: the one thing that cannot be said afterwards is "no")
    except (TranscodeError, StopIteration):  # (the song could not be started, or made nothing: the generator has ended, and let go of what it held)
        return None
    left = duration - offset if duration else None
    body, headers = _promised(itertools.chain([first], pieces), dict(LIVE), left, plan.kbps, estimate)
    return StreamingResponse(body, media_type=MEDIA_TYPE, headers=headers, background=BackgroundTask(pieces.close))


def converted(
    transcoder: Transcoder, request: Request, track_id: int, path: Path, plan: Plan, duration: float | None, *, offset: float, estimate: bool,
) -> Response | None:
    """The response for the song converted as `plan` says; None when it cannot be (the caller sends the file as it is)."""
    head = request.method == "HEAD"
    if offset > 0:
        return _head() if head else _from_a_time(transcoder, path, plan, duration, offset, estimate)
    key = transcoder.key(track_id, path, plan.kbps)
    kept = transcoder.cached(key)
    if kept is not None:
        return FileResponse(kept, media_type=MEDIA_TYPE, headers=KEPT)
    if transcoder.failed(key):  # (it did not work a moment ago, and a listener who was cut off asks again at once: the file)
        return None
    if head:
        return _head()
    job = transcoder.job(key, path, plan.kbps)
    wanted = request.headers.get("range")
    if wanted and not from_the_start(wanted):
        kept = transcoder.cached(key) if job.done.wait(transcode.COMPLETE_WAIT) else None  # (a song that could not be made has no file: the original is sent)
        return FileResponse(kept, media_type=MEDIA_TYPE, headers=KEPT) if kept is not None else None
    if not job.started.wait(transcode.FIRST_BYTES_WAIT) or not job.size:  # (no bytes in time, or the song ended without any)
        return None
    body, headers = _promised(transcoder.follow(job), dict(LIVE), duration, plan.kbps, estimate)
    return StreamingResponse(body, media_type=MEDIA_TYPE, headers=headers)
