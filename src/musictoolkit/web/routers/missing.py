"""Missing files: songs the library lists but whose file cannot be found. Look at them, or forget them for good."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from musictoolkit.ingest import cleanup
from musictoolkit.web.context import AppContext, get_ctx

router = APIRouter(prefix="/api/tools/missing")


class ForgetBody(BaseModel):
    track_ids: list[int] | None = Field(default=None, max_length=200_000)
    root: str | None = None  # a library folder, or "*other*" for songs under no library folder
    everything: bool = False
    confirm: bool = False


@router.get("/summary")
def summary(ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        return cleanup.missing_summary(conn, list(ctx.config.library.roots))


@router.get("/items")
def items(offset: int = 0, limit: int = 100, root: str | None = None, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        return cleanup.list_missing(conn, list(ctx.config.library.roots), max(0, offset), max(1, min(limit, 500)), root)


@router.post("/forget")
def forget(body: ForgetBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Remove songs whose files are gone from the library, with their playlist entries. Needs `confirm`."""
    if not body.confirm:
        raise HTTPException(status_code=422, detail="Confirm that these songs should be forgotten")
    if body.track_ids is None and body.root is None and not body.everything:
        raise HTTPException(status_code=422, detail="Choose songs, a folder, or everything")
    with ctx.db() as conn:
        return cleanup.forget_missing(conn, list(ctx.config.library.roots), body.track_ids, body.root)
