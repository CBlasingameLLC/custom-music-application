"""Whether plays are reaching ListenBrainz, and a way to try again after a problem."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from musictoolkit.web.context import AppContext, get_ctx

router = APIRouter(prefix="/api/scrobbler")


@router.get("")
def status(ctx: AppContext = Depends(get_ctx)) -> dict:
    return ctx.scrobbler.status()


@router.post("/retry")
def retry(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Forget a rejection or a backoff and look at the queue now."""
    ctx.scrobbler.reset()
    return ctx.scrobbler.status()
