"""Listening statistics for the Stats screen and the Year in Music view."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query

from musictoolkit.history import stats
from musictoolkit.web.context import AppContext, get_ctx

router = APIRouter(prefix="/api/stats")

CACHE_SECONDS = 600  # a worked-out period is reused this long, and until the history changes
CACHE_ENTRIES = 24


@router.get("/years")
def years(tz: int = Query(0, description="the viewer's offset from UTC in minutes, east positive"), ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        return {"items": stats.years(conn, tz)}


@router.get("/overview")
def overview(
    period: str = Query("all", alias="range", description="all, year:2025, month:2025-06 or days:30"),
    tz: int = 0,
    limit: int = 10,
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    key = (period, stats.clamp_offset(tz), stats._limit(limit))
    try:
        with ctx.db() as conn:
            row = conn.execute("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS last FROM play_history").fetchone()
            history = (row["n"], row["last"])
            kept = ctx.stats_cache.get(key)
            if kept is not None and kept[0] == history and time.monotonic() - kept[1] < CACHE_SECONDS:
                return kept[2]
            result = stats.overview(conn, period, tz, limit)
    except stats.BadRange as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if len(ctx.stats_cache) >= CACHE_ENTRIES:
        ctx.stats_cache.pop(next(iter(ctx.stats_cache)))  # the oldest
    ctx.stats_cache[key] = (history, time.monotonic(), result)
    return result
