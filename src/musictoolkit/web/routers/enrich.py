"""Fix missing tags with MusicBrainz: look songs up in the background, review the matches, apply the good ones."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from musictoolkit.ingest import tagger
from musictoolkit.integrations import musicbrainz_client
from musictoolkit.web.context import AppContext, get_ctx
from musictoolkit.web.jobs import JobHandle

router = APIRouter(prefix="/api/tools/enrich")


class StartBody(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=100_000)


class ApplyBody(BaseModel):
    track_ids: list[int] | None = Field(default=None, max_length=50_000)
    min_confidence: float | None = Field(default=None, ge=0, le=1)
    overwrite: bool = False


class DismissBody(BaseModel):
    track_ids: list[int] | None = Field(default=None, max_length=50_000)
    max_confidence: float | None = Field(default=None, ge=0, le=1)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


@router.get("/summary")
def summary(ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        counts = tagger.summary(conn)
    return {**counts, "contact_set": bool(ctx.config.musicbrainz.contact.strip())}


@router.post("/start")
def start(body: StartBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    cfg = ctx.config.musicbrainz
    if not cfg.contact.strip():
        raise HTTPException(
            status_code=409,
            detail="MusicBrainz asks every app to send a contact email with its requests. Add yours in Settings first.",
        )
    if ctx.jobs.is_busy("enrich"):
        raise HTTPException(status_code=409, detail="A lookup is already running")

    def run(handle: JobHandle) -> dict[str, Any]:
        musicbrainz_client.configure(cfg.app_name, cfg.app_version, cfg.contact.strip())

        def progress(done: int, total: int) -> None:
            handle.update(done=done, total=total, message=f"{done} of {_plural(total, 'song')} looked up")
            handle.check()

        with ctx.db() as conn:
            result = tagger.propose_tags(conn, None, on_progress=progress, limit=body.limit, persist=True)
        handle.log(f"{len(result.proposals)} matches, {result.skipped_no_match} without a match, {result.errors} errors")
        return {"found": len(result.proposals), "no_match": result.skipped_no_match, "errors": result.errors}

    return {"job": ctx.jobs.submit("enrich", "Looking up songs on MusicBrainz", run, lane="network").to_dict()}


@router.get("/proposals")
def proposals(
    offset: int = 0,
    limit: int = 50,
    min_confidence: float = 0.0,
    overwrite: bool = False,
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    with ctx.db() as conn:
        return tagger.list_proposals(conn, max(0, offset), max(1, min(limit, 200)), min_confidence, overwrite)


@router.post("/apply")
def apply(body: ApplyBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    if body.track_ids is None and body.min_confidence is None:
        raise HTTPException(status_code=422, detail="Choose songs, or a minimum confidence")
    with ctx.db() as conn:
        count = len(tagger._selected(conn, body.track_ids, body.min_confidence))
    if not count:
        raise HTTPException(status_code=404, detail="No matching proposals are waiting")

    def run(handle: JobHandle) -> dict[str, Any]:
        def progress(done: int, total: int) -> None:
            handle.update(done=done, total=total, message=f"{done} of {_plural(total, 'song')}")
            handle.check()

        with ctx.db() as conn:
            return tagger.apply_proposals(conn, body.track_ids, body.min_confidence, body.overwrite, progress).to_dict()

    return {"job": ctx.jobs.submit("tags", f"Applying MusicBrainz tags to {_plural(count, 'song')}", run).to_dict()}


@router.post("/dismiss")
def dismiss(body: DismissBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        return {"dismissed": tagger.dismiss_proposals(conn, body.track_ids, body.max_confidence)}


@router.post("/retry")
def retry(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Let songs that had no match (or whose match was turned down) be looked up again."""
    with ctx.db() as conn:
        return {"reset": tagger.forget_dead_ends(conn)}
