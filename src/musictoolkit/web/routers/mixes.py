"""Mixes and song radio: queues made from the library and the listening history."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from musictoolkit.web import mixes
from musictoolkit.web.context import AppContext, get_ctx

router = APIRouter(prefix="/api")

CACHE_SECONDS = 300  # the shelf is reused this long, and until the library or the history changes
SIGNATURE = (
    "SELECT (SELECT COUNT(*) FROM play_history) AS plays, (SELECT COALESCE(MAX(id), 0) FROM play_history) AS last_play, "
    "(SELECT COUNT(*) FROM tracks) AS songs, (SELECT COALESCE(MAX(id), 0) FROM tracks) AS last_song, "
    "(SELECT COUNT(*) FROM tracks WHERE favorite = 1) AS favorites, (SELECT COUNT(*) FROM tracks WHERE is_missing = 1) AS missing"
)


@router.get("/mixes")
def list_mixes(tz: int = 0, size: int = mixes.DEFAULT_SIZE, ctx: AppContext = Depends(get_ctx)) -> dict:
    """The mixes worth showing today. `tz` is the viewer's offset from UTC in minutes (east positive)."""
    offset, size = max(-840, min(840, tz)), max(1, min(size, mixes.MAX_SIZE))
    with ctx.db() as conn:
        # What the shelf holds depends on the history, the library, the favorites and the day: while none of those changed
        # the last answer stands, so coming back to Home does not ask the whole library again.
        state = (tuple(conn.execute(SIGNATURE).fetchone()), mixes.daily_seed(datetime.now(timezone.utc)))
        key = (offset, size)
        kept = ctx.mixes_cache.get(key)
        if kept is not None and kept[0] == state and time.monotonic() - kept[1] < CACHE_SECONDS:
            return kept[2]
        answer = {"items": mixes.available(conn, offset_minutes=offset, size=size)}
    ctx.mixes_cache[key] = (state, time.monotonic(), answer)
    return answer


@router.get("/mixes/{mix_id}")
def get_mix(mix_id: str, tz: int = 0, size: int = mixes.DEFAULT_SIZE, seed: int | None = None, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        found = mixes.build(conn, mix_id, offset_minutes=max(-840, min(840, tz)), size=size, seed=seed)
    if found is None:
        raise HTTPException(status_code=404, detail="That mix is empty today (or there is no such mix)")
    return found


@router.get("/radio/track/{track_id}")
def song_radio(track_id: int, size: int = mixes.DEFAULT_SIZE, seed: int | None = None, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Songs that go with one song, starting with that song."""
    with ctx.db() as conn:
        tracks = mixes.radio(conn, track_id, size=size, seed=seed)
    if not tracks:
        raise HTTPException(status_code=404, detail="That song is not in the library")
    return {"seed_track_id": track_id, "tracks": tracks}
