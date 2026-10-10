"""Tag editing: read the current values, save edits as a background job, undo a batch."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from musictoolkit.ingest import tagedit, tagwrite
from musictoolkit.web.context import AppContext, get_ctx
from musictoolkit.web.jobs import JobHandle

router = APIRouter(prefix="/api/tags")

MAX_IDS = 20_000


class IdsBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=MAX_IDS)


class EditBody(IdsBody):
    changes: dict[str, Any]


class UndoBody(BaseModel):
    batch_id: str


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _progress(handle: JobHandle):
    def update(done: int, total: int) -> None:
        handle.update(done=done, total=total, message=f"{done} of {_plural(total, 'song')}")
        handle.check()

    return update


@router.post("/read")
def read_tags(body: IdsBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    """What the Edit tags dialog shows: per field the shared value, or "mixed" when the songs differ."""
    ids = list(dict.fromkeys(body.ids))
    with ctx.db() as conn:
        rows: list[Any] = []
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows += conn.execute(
                f"SELECT * FROM tracks WHERE is_missing = 0 AND id IN ({','.join('?' * len(chunk))})", chunk
            ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="those songs are not in the library")
    fields = {}
    for field in tagwrite.EDITABLE_FIELDS:
        values = {row[field] if row[field] not in ("", None) else None for row in rows}
        fields[field] = {"value": next(iter(values)) if len(values) == 1 else None, "mixed": len(values) > 1}
    return {
        "count": len(rows),
        "fields": fields,
        "path": rows[0]["file_path"] if len(rows) == 1 else None,
        "unwritable": [
            {"id": r["id"], "title": r["title"] or r["file_path"], "format": r["format"]}
            for r in rows
            if not tagwrite.can_write(r["file_path"])
        ],
    }


@router.post("/edit")
def edit_tags(body: EditBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    try:
        changes = tagwrite.clean_changes(body.changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to change")
    ids = list(dict.fromkeys(body.ids))

    def run(handle: JobHandle) -> dict[str, Any]:
        with ctx.db() as conn:
            return tagedit.edit_tracks(conn, ids, changes, on_progress=_progress(handle)).to_dict()

    return {"job": ctx.jobs.submit("tags", f"Saving tags for {_plural(len(ids), 'song')}", run).to_dict()}


@router.get("/batches")
def tag_batches(ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        return {"items": tagedit.list_batches(conn)}


@router.post("/undo")
def undo_tags(body: UndoBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        found = conn.execute("SELECT COUNT(*) AS n FROM tag_edits WHERE batch_id = ?", (body.batch_id,)).fetchone()["n"]
    if not found:
        raise HTTPException(status_code=404, detail="That edit can no longer be undone")

    def run(handle: JobHandle) -> dict[str, Any]:
        with ctx.db() as conn:
            return {**tagedit.undo_batch(conn, body.batch_id, on_progress=_progress(handle)).to_dict(), "undo": True}

    return {"job": ctx.jobs.submit("tags", f"Undoing a tag edit ({_plural(found, 'song')})", run).to_dict()}
