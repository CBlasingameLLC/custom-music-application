"""Podcasts: the ones you follow, finding more, the episodes and where you stopped, and the audio itself (played through this app)."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from musictoolkit.net import artwork
from musictoolkit.net.proxy import StreamError, acceptable_type, open_audio, relay
from musictoolkit.podcasts import directory, refresh, store
from musictoolkit.web.context import AppContext, get_ctx
from musictoolkit.web.podcast_job import submit_refresh

router = APIRouter(prefix="/api/podcasts")

MAX_EMULATED_SKIP = 400_000_000  # bytes a host that ignores Range may be made to skip over for a seek
RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _podcast_row(ctx: AppContext, podcast_id: int):
    with ctx.db() as conn:
        row = conn.execute("SELECT * FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="podcast not found")
    return row


def _episode_row(ctx: AppContext, episode_id: int):
    with ctx.db() as conn:
        row = store.episode_row(conn, episode_id)
    if row is None:
        raise HTTPException(status_code=404, detail="episode not found")
    return row


# ---------------------------------------------------------------- the podcasts followed (fixed paths first: /{podcast_id} would claim them)

@router.get("")
def list_podcasts(ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        items = store.list_podcasts(conn)
    return {"items": items, "refreshing": ctx.jobs.is_busy("podcasts"), "automatic": ctx.config.app.podcast_refresh}


class Follow(BaseModel):
    feed_url: str = Field(min_length=1, max_length=2048)
    itunes_id: str | None = Field(default=None, max_length=32)


@router.post("", status_code=201)
def follow(body: Follow, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Follow a podcast from the address of its feed. The feed is read first: an address that is not a podcast is not followed."""
    with ctx.db() as conn:
        try:
            return refresh.follow(conn, body.feed_url, itunes_id=body.itunes_id)
        except refresh.PodcastError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/refresh")
def refresh_all(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Look now, in the background (the Tasks tray shows progress), whether or not the automatic look is on."""
    job = submit_refresh(ctx)
    if job is None:
        raise HTTPException(status_code=409, detail="Already checking the podcasts")
    return {"job": job.to_dict()}


@router.get("/search")
def search(q: str = "", country: str = "US", ctx: AppContext = Depends(get_ctx)) -> dict:
    """Find podcasts in Apple's directory; each says whether it is followed here already (and under which id)."""
    try:
        found = directory.search(q, country=country)
    except directory.DirectoryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    with ctx.db() as conn:
        for show in found:
            row = conn.execute("SELECT id FROM podcasts WHERE feed_url = ?", (show["feed_url"],)).fetchone()
            show["followed_id"] = row["id"] if row else None
    return {"items": found}


@router.get("/new")
def new_episodes(limit: int = 50, offset: int = 0, ctx: AppContext = Depends(get_ctx)) -> dict:
    """The newest episodes not yet heard, from every podcast followed."""
    with ctx.db() as conn:
        return store.new_episodes(conn, limit=max(1, min(limit, 200)), offset=max(0, offset))


# ---------------------------------------------------------------- episodes

@router.get("/episodes/{episode_id}")
def get_episode(episode_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        episode = store.get_episode(conn, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    return episode


class Progress(BaseModel):
    position: float = Field(ge=0, le=10 * 86400, allow_inf_nan=False)
    duration: float | None = Field(default=None, ge=0, le=10 * 86400, allow_inf_nan=False)


@router.post("/episodes/{episode_id}/progress")
def episode_progress(episode_id: int, body: Progress, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Where listening has got to (the page reports it every few seconds and when playback pauses)."""
    with ctx.db() as conn:
        episode = store.set_progress(conn, episode_id, body.position, body.duration)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    return episode


class Played(BaseModel):
    played: bool = True


@router.post("/episodes/{episode_id}/played")
def episode_played(episode_id: int, body: Played, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        episode = store.set_played(conn, episode_id, body.played)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    return episode


def _picture(ctx: AppContext, url: str | None) -> Response:
    data = artwork.cached_picture(url, ctx.data_dir / "cache" / "podcasts")
    if data is None:
        raise HTTPException(status_code=404, detail="no picture")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/episodes/{episode_id}/art")
def episode_art(episode_id: int, ctx: AppContext = Depends(get_ctx)) -> Response:
    row = _episode_row(ctx, episode_id)
    return _picture(ctx, row["art_url"] or row["podcast_art"])


@router.get("/episodes/{episode_id}/stream", response_model=None)
def stream_episode(episode_id: int, request: Request, ctx: AppContext = Depends(get_ctx)) -> StreamingResponse | Response:
    """The episode's audio, passed on. Seeking works: a Range request is passed to the host, and when the host ignores it the app skips ahead itself."""
    row = _episode_row(ctx, episode_id)
    slots = ctx.podcast_slots
    if not slots.take():
        raise HTTPException(status_code=429, detail="Too many streams are open at once")
    asked = RANGE.match(request.headers.get("range", "").strip())
    start = int(asked.group(1)) if asked and asked.group(1) else None
    headers = {"Accept": "audio/*,*/*;q=0.5", "Accept-Encoding": "identity"}
    if asked and (asked.group(1) or asked.group(2)):
        headers["Range"] = request.headers["range"].strip()
    try:
        upstream, _final = open_audio(row["audio_url"], headers=headers)
    except StreamError as exc:
        slots.give_back()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except BaseException:
        slots.give_back()
        raise
    status = upstream.status_code
    if status == 416:
        upstream.close()
        slots.give_back()
        return Response(status_code=416, headers={"Content-Range": upstream.headers.get("Content-Range", "bytes */*")})
    content_type = upstream.headers.get("content-type", "")
    if status not in (200, 206) or not acceptable_type(content_type):
        upstream.close()
        slots.give_back()
        problem = "That address is not audio." if status in (200, 206) else f"The podcast's host answered with error {status}."
        raise HTTPException(status_code=502, detail=problem)
    kind = content_type.split(";")[0].strip().lower()
    media = kind if kind.startswith("audio/") or kind == "application/ogg" else (row["audio_type"] or "audio/mpeg")
    packed = bool(upstream.headers.get("content-encoding", "").strip().lower() not in ("", "identity"))
    length = upstream.headers.get("content-length", "")
    total = int(length) if length.isdigit() and not packed else None
    out: dict[str, str] = {}
    skip = 0
    code = status
    if status == 206 and not packed:
        if upstream.headers.get("content-range"):
            out["Content-Range"] = upstream.headers["content-range"]
        if total is not None:
            out["Content-Length"] = str(total)
        out["Accept-Ranges"] = "bytes"
    elif status == 200 and start and total is not None and start < total and start <= MAX_EMULATED_SKIP and not packed:
        code, skip = 206, start  # the host ignored the Range request: the app skips the bytes itself
        out.update({"Content-Range": f"bytes {start}-{total - 1}/{total}", "Content-Length": str(total - start), "Accept-Ranges": "bytes"})
    elif status == 200 and total is not None:
        out.update({"Content-Length": str(total), "Accept-Ranges": "bytes"})
    return relay(upstream, status_code=code, media_type=media, headers=out, skip=skip, done=slots.give_back)


# ---------------------------------------------------------------- one podcast

@router.get("/{podcast_id}")
def get_podcast(podcast_id: int, limit: int = 50, offset: int = 0, unplayed: bool = False, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db() as conn:
        podcast = store.get_podcast(conn, podcast_id)
        if podcast is None:
            raise HTTPException(status_code=404, detail="podcast not found")
        episodes = store.list_episodes(conn, podcast_id, unplayed_only=unplayed, limit=max(1, min(limit, 200)), offset=max(0, offset))
    return {**podcast, **episodes}


@router.delete("/{podcast_id}")
def unfollow(podcast_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        if not store.unsubscribe(conn, podcast_id):
            raise HTTPException(status_code=404, detail="podcast not found")
    return {"ok": True}


@router.post("/{podcast_id}/refresh")
def refresh_one(podcast_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    _podcast_row(ctx, podcast_id)
    job = submit_refresh(ctx, only=podcast_id)
    if job is None:
        raise HTTPException(status_code=409, detail="Already checking the podcasts")
    return {"job": job.to_dict()}


@router.post("/{podcast_id}/played")
def mark_all_played(podcast_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    _podcast_row(ctx, podcast_id)
    with ctx.db() as conn:
        return {"marked": store.mark_all_played(conn, podcast_id)}


@router.get("/{podcast_id}/art")
def podcast_art(podcast_id: int, ctx: AppContext = Depends(get_ctx)) -> Response:
    return _picture(ctx, _podcast_row(ctx, podcast_id)["art_url"])
