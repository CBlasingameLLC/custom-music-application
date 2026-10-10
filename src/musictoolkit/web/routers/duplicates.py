"""Duplicate songs: search, choose what to keep, park the rest in a review folder, restore or delete from there.

Nothing here deletes anything straight away. Removed copies are moved into `_duplicates_review` inside their
library folder and hidden; only "delete" from the review folder, which needs a typed confirmation, sends them to
the Recycle Bin."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from musictoolkit.ingest import dedupe, moves
from musictoolkit.web.context import AppContext, get_ctx
from musictoolkit.web.jobs import JobHandle

router = APIRouter(prefix="/api/tools/duplicates")

PAGE = 25
JOB_KINDS = ("dedupe", "dedupe-restore", "dedupe-purge")


class ScanBody(BaseModel):
    exact: bool = False  # also compare file contents


class Choice(BaseModel):
    key: str
    keeper: int
    remove: list[int] = Field(min_length=1, max_length=1000)


class QuarantineBody(BaseModel):
    job_id: str
    choices: list[Choice] = Field(min_length=1, max_length=20_000)


class IgnoreBody(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=20_000)


class SelectionBody(BaseModel):
    track_ids: list[int] | None = Field(default=None, max_length=100_000)
    everything: bool = False


class DeleteBody(SelectionBody):
    confirm: str = ""


class UndoBody(BaseModel):
    batch_id: str


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _copies(n: int) -> str:
    return f"{n:,} {'copy' if n == 1 else 'copies'}"


def _roots(ctx: AppContext) -> list[Path]:
    return [Path(r).resolve() for r in ctx.config.library.roots]


def _busy(ctx: AppContext) -> bool:
    return any(ctx.jobs.is_busy(kind) for kind in (*JOB_KINDS, "organize", "organize-undo"))


def _selected(ctx: AppContext, body: SelectionBody) -> list[int]:
    if body.everything:
        with ctx.db() as conn:
            ids = dedupe.quarantined_ids(conn)
        if not ids:
            raise HTTPException(status_code=422, detail="The review folder is empty")
        return ids
    if not body.track_ids:
        raise HTTPException(status_code=422, detail="Choose songs, or everything in the review folder")
    return list(dict.fromkeys(body.track_ids))


@router.get("/info")
def info(ctx: AppContext = Depends(get_ctx)) -> dict:
    held = ctx.previews.get("duplicates")
    with ctx.db() as conn:
        return {
            "has_folders": bool(ctx.config.library.roots),
            "review": dedupe.quarantined_summary(conn),
            "ignored": len(dedupe.load_ignored(conn)),
            "batches": moves.list_batches(conn, "quarantine"),
            # the last search, so leaving the page and coming back does not lose it
            "scan": {"job_id": held["job_id"], **held["result"]} if held else None,
        }


@router.post("/scan")
def scan(body: ScanBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    roots = _roots(ctx)
    if not roots:
        raise HTTPException(status_code=409, detail="Add a music folder first")

    def run(handle: JobHandle) -> dict[str, Any]:
        def progress(done: int, total: int) -> None:
            handle.update(done=done, total=total, message=f"Compared {done:,} of {_plural(total, 'file')}")
            handle.check()

        with ctx.db() as conn:
            ignored = dedupe.load_ignored(conn)
            groups = dedupe.find_duplicate_groups(conn, roots, body.exact, progress, ignored)
            described = dedupe.describe_groups(conn, groups)
        result = {
            "groups": len(described), "copies": sum(len(g["members"]) - 1 for g in described),
            "bytes": sum(g["wasted"] for g in described), "exact": body.exact, "ignored": len(ignored),
        }
        ctx.previews["duplicates"] = {"job_id": handle.id, "groups": described, "result": result}
        return result

    title = "Comparing file contents" if body.exact else "Looking for duplicate songs"
    return {"job": ctx.jobs.submit("dedupe-scan", title, run, lane="analyze").to_dict()}


def _held(ctx: AppContext, job_id: str) -> dict[str, Any]:
    held = ctx.previews.get("duplicates")
    if not held or held["job_id"] != job_id:
        raise HTTPException(status_code=409, detail="That list is out of date. Search again.")
    return held


@router.get("/groups/{job_id}")
def groups(job_id: str, offset: int = 0, limit: int = PAGE, ctx: AppContext = Depends(get_ctx)) -> dict:
    held = _held(ctx, job_id)
    offset, limit = max(0, offset), max(1, min(limit, 100))
    return {"total": len(held["groups"]), "items": held["groups"][offset:offset + limit]}


@router.post("/quarantine")
def quarantine(body: QuarantineBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    held = _held(ctx, body.job_id)
    by_key = {g["key"]: g for g in held["groups"]}
    choices: list[tuple[int, list[int]]] = []
    for choice in body.choices:
        group = by_key.get(choice.key)
        if group is None:
            raise HTTPException(status_code=422, detail="One of those groups is not in the list any more. Search again.")
        member_ids = {m["id"] for m in group["members"]}
        if choice.keeper not in member_ids or not set(choice.remove) <= member_ids or choice.keeper in choice.remove:
            raise HTTPException(status_code=422, detail="Pick one copy to keep and others from the same group to remove")
        choices.append((choice.keeper, list(dict.fromkeys(choice.remove))))
    if _busy(ctx):
        raise HTTPException(status_code=409, detail="Files are already being moved")
    count = sum(len(remove) for _, remove in choices)
    roots = _roots(ctx)

    def run(handle: JobHandle) -> dict[str, Any]:
        def progress(done: int, total: int) -> None:
            handle.update(done=done, total=total, message=f"Moved {done:,} of {_copies(total)}")
            handle.check()

        try:
            with ctx.db() as conn:
                result = dedupe.quarantine(conn, choices, roots, progress)
        finally:
            ctx.previews.pop("duplicates", None)  # the list no longer matches the library
        return {**result.to_dict(), "errors": result.errors[:100], "errors_total": len(result.errors)}

    title = f"Moving {_copies(count)} to the review folder"
    return {"job": ctx.jobs.submit("dedupe", title, run).to_dict()}


@router.post("/ignore")
def ignore(body: IgnoreBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        added = dedupe.ignore_groups(conn, body.keys)
        total = len(dedupe.load_ignored(conn))
    held = ctx.previews.get("duplicates")
    if held:
        gone = set(body.keys)
        held["groups"] = [g for g in held["groups"] if g["key"] not in gone]
    return {"ignored": added, "total_ignored": total}


@router.post("/ignore/reset")
def reset_ignore(ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        return {"cleared": dedupe.reset_ignored(conn)}


@router.get("/review")
def review(offset: int = 0, limit: int = 100, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        return {**dedupe.list_quarantined(conn, max(0, offset), max(1, min(limit, 500))), **dedupe.quarantined_summary(conn)}


@router.post("/restore")
def restore(body: SelectionBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    ids = _selected(ctx, body)
    if _busy(ctx):
        raise HTTPException(status_code=409, detail="Files are already being moved")

    def run(handle: JobHandle) -> dict[str, Any]:
        def progress(done: int, total: int) -> None:
            handle.update(done=done, total=total, message=f"Restored {done:,} of {_plural(total, 'song')}")
            handle.check()

        with ctx.db() as conn:
            result = dedupe.restore(conn, ids, _roots(ctx), progress)
        return {**result.to_dict(), "errors": result.errors[:100], "errors_total": len(result.errors)}

    return {"job": ctx.jobs.submit("dedupe-restore", f"Restoring {_plural(len(ids), 'song')}", run).to_dict()}


@router.post("/delete")
def delete(body: DeleteBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Send review-folder copies to the Recycle Bin. Needs the word DELETE as `confirm`."""
    if body.confirm != "DELETE":
        raise HTTPException(status_code=422, detail="Type DELETE to confirm")
    ids = _selected(ctx, body)
    if _busy(ctx):
        raise HTTPException(status_code=409, detail="Files are already being moved")

    def run(handle: JobHandle) -> dict[str, Any]:
        def progress(done: int, total: int) -> None:
            handle.update(done=done, total=total, message=f"Moved {done:,} of {_plural(total, 'song')} to the Recycle Bin")
            handle.check()

        with ctx.db() as conn:
            result = dedupe.purge(conn, ids, _roots(ctx), progress)
        return {**result.to_dict(), "errors": result.errors[:100], "errors_total": len(result.errors)}

    return {"job": ctx.jobs.submit("dedupe-purge", f"Moving {_plural(len(ids), 'song')} to the Recycle Bin", run).to_dict()}


@router.get("/batches")
def batches(ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        return {"batches": moves.list_batches(conn, "quarantine")}


@router.post("/undo")
def undo(body: UndoBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Bring back everything a "move to the review folder" batch moved (and what was carried over)."""
    with ctx.db() as conn:
        files = conn.execute(
            "SELECT COUNT(*) FROM file_moves WHERE batch_id = ? AND kind = 'quarantine' AND undone_at IS NULL", (body.batch_id,)
        ).fetchone()[0]
    if not files:
        raise HTTPException(status_code=404, detail="Nothing is left to undo in that batch")
    if _busy(ctx):
        raise HTTPException(status_code=409, detail="Files are already being moved")

    def run(handle: JobHandle) -> dict[str, Any]:
        def progress(done: int, total: int) -> None:
            handle.update(done=done, total=total, message=f"Restored {done:,} of {_plural(total, 'song')}")
            handle.check()

        with ctx.db() as conn:
            result = moves.undo_batch(conn, body.batch_id, "quarantine", _roots(ctx), progress)
        return {**result.to_dict(), "errors": result.errors[:100], "errors_total": len(result.errors)}

    return {"job": ctx.jobs.submit("dedupe-restore", f"Restoring {_plural(files, 'song')}", run).to_dict()}
