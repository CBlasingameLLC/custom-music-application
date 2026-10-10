"""Mixes and song radio: queues made from the library and the listening history."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from musictoolkit.web import mixes
from musictoolkit.web.context import AppContext, get_ctx

router = APIRouter(prefix="/api")


@router.get("/mixes")
def list_mixes(tz: int = 0, size: int = mixes.DEFAULT_SIZE, ctx: AppContext = Depends(get_ctx)) -> dict:
    """The mixes worth showing today. `tz` is the viewer's offset from UTC in minutes (east positive)."""
    with ctx.db() as conn:
        return {"items": mixes.available(conn, offset_minutes=max(-840, min(840, tz)), size=max(1, min(size, mixes.MAX_SIZE)))}


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
