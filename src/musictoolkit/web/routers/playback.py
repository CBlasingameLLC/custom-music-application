from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from musictoolkit.media import art, info, lrclib, lyrics
from musictoolkit.web.context import AppContext, get_ctx
from musictoolkit.web.queries import (
    ALBUM_ARTIST_FIND,
    ALBUM_FIND,
    parse_album_key,
    tracks_by_ids,
)

router = APIRouter(prefix="/api")

MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".m4b": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".aiff": "audio/aiff",
    ".wma": "audio/x-ms-wma",
    ".ape": "audio/x-ape",
}

# Formats Chromium (and so the desktop app) cannot decode; the UI says so instead of failing silently.
UNPLAYABLE_FORMATS = {"wma", "ape"}


def _track_file(ctx: AppContext, track_id: int) -> Path:
    with ctx.db() as conn:
        row = conn.execute("SELECT file_path FROM tracks WHERE id = ? AND is_missing = 0", (track_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="track not found")
        path = Path(row["file_path"])
        if not path.is_file():
            conn.execute("UPDATE tracks SET is_missing = 1 WHERE id = ?", (track_id,))
            conn.commit()
            raise HTTPException(status_code=404, detail="the file is missing from disk")
    return path


@router.get("/tracks/{track_id}/stream")
def stream(track_id: int, ctx: AppContext = Depends(get_ctx)) -> FileResponse:
    """The audio file itself; FileResponse answers Range requests, which is what seeking needs."""
    path = _track_file(ctx, track_id)
    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/tracks/{track_id}/info")
def track_info(track_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    path = _track_file(ctx, track_id)
    details = info.file_info(path)
    gain = info.replaygain(path)
    return {
        **details,
        "replaygain_track_db": gain["track_db"],
        "replaygain_album_db": gain["album_db"],
        "playable": path.suffix.lower().lstrip(".") not in UNPLAYABLE_FORMATS,
        "has_art": art.cached_art(path, 64, ctx.art_cache_dir) is not None,
    }


def _online_lyrics(ctx: AppContext, track_id: int, *, fetch: bool, force: bool) -> dict:
    """The answer for a song whose file has no lyrics: what lrclib.net is known to have (kept in the app's database),
    asking it when `fetch` says that is allowed. `online` says how that went: found, none, off or unreachable."""
    with ctx.db() as conn:
        track = conn.execute("SELECT id, artist, title, album, duration_seconds AS duration FROM tracks WHERE id = ?", (track_id,)).fetchone()
        answer = lrclib.resolve(conn, track, fetch=fetch, force=force)
    result: dict = {"source": None, "synced": None, "plain": None, "instrumental": False, "online": answer["state"]}
    if answer["state"] == "found":
        result.update(source="lrclib", synced=answer["synced"], plain=answer["plain"], instrumental=answer["instrumental"])
    if answer.get("message"):
        result["message"] = answer["message"]
    return result


@router.get("/tracks/{track_id}/lyrics")
def track_lyrics(track_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Lyrics from the file (embedded, or a .lrc beside it); failing that, from lrclib.net if the person turned that on."""
    own = lyrics.lyrics_for(_track_file(ctx, track_id))
    if own["source"] is not None:
        return {**own, "instrumental": False, "online": None}
    return _online_lyrics(ctx, track_id, fetch=ctx.config.app.lyrics_lrclib, force=False)


@router.post("/tracks/{track_id}/lyrics/lookup")
def lookup_lyrics(track_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Look this one song up on lrclib.net now, whether or not automatic lookups are on (the person pressed Look up)."""
    own = lyrics.lyrics_for(_track_file(ctx, track_id))
    if own["source"] is not None:
        return {**own, "instrumental": False, "online": None}
    return _online_lyrics(ctx, track_id, fetch=True, force=True)


def _art_response(path: Path, size: int, ctx: AppContext) -> Response:
    data = art.cached_art(path, size, ctx.art_cache_dir)
    if data is None:
        raise HTTPException(status_code=404, detail="no cover art")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/art/track/{track_id}")
def track_art(track_id: int, size: int = 256, ctx: AppContext = Depends(get_ctx)) -> Response:
    return _art_response(_track_file(ctx, track_id), size, ctx)


@router.get("/art/album/{key}")
def album_art(key: str, size: int = 256, ctx: AppContext = Depends(get_ctx)) -> Response:
    """Cover for an album: the first of its tracks that has any art."""
    album_artist, album = parse_album_key(key)
    with ctx.db() as conn:
        rows = conn.execute(
            f"SELECT t.file_path FROM tracks t WHERE t.is_missing = 0 AND {ALBUM_ARTIST_FIND} = ? AND {ALBUM_FIND} = ? "
            f"ORDER BY COALESCE(t.disc_number, 0), COALESCE(t.track_number, 0), t.id LIMIT 6",
            (album_artist, album),
        ).fetchall()
    for row in rows:
        data = art.cached_art(Path(row["file_path"]), size, ctx.art_cache_dir)
        if data is not None:
            return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
    raise HTTPException(status_code=404, detail="no cover art")


class PlayLog(BaseModel):
    track_id: int
    ms_played: int = Field(ge=0)
    started_at: float | None = None  # unix seconds; defaults to now - ms_played


@router.post("/plays")
def log_play(body: PlayLog, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Record a listen from the in-app player. Uses the 'future_scrobble' source the
    schema reserved for native plays, with the names copied in so the history
    survives the file being removed and can be submitted to ListenBrainz."""
    started = int(body.started_at if body.started_at else time.time() - body.ms_played / 1000)
    with ctx.db() as conn:
        found = tracks_by_ids(conn, [body.track_id])
        if not found:
            raise HTTPException(status_code=404, detail="track not found")
        track = found[0]
        cursor = conn.execute(
            "INSERT INTO play_history (track_id, source, played_at_epoch, ms_played, raw_artist_name, "
            "raw_track_name, raw_album_name) VALUES (?, 'future_scrobble', ?, ?, ?, ?, ?)",
            (track["id"], started, body.ms_played, track["artist"], track["title"], track["album"]),
        )
        conn.commit()
        if ctx.scrobbler:
            ctx.scrobbler.wake()  # send it to ListenBrainz now rather than at the next poll
        return {"id": cursor.lastrowid, "plays": track["plays"] + 1}


class NowPlaying(BaseModel):
    track_id: int


@router.post("/now-playing")
def now_playing(body: NowPlaying, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Tell ListenBrainz what is playing, if the person turned that on. Never an error: it is only a courtesy."""
    with ctx.db() as conn:
        found = tracks_by_ids(conn, [body.track_id])
    if not found or not ctx.scrobbler:
        return {"sent": False}
    track = found[0]
    return {"sent": ctx.scrobbler.now_playing({"id": track["id"], "artist": track["artist"], "title": track["title"], "album": track["album"]})}
