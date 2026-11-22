"""Convert: change songs from one format to another, into a folder outside the library.

Like every tool that changes files it shows a preview first (a job: looking at every song takes a while in a big library), the start is refused unless it
is for the very choices that were shown, and a conversion is a job that can be stopped and a history entry that can be taken back.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from musictoolkit.convert import formats, job, store
from musictoolkit.convert import plan as planning
from musictoolkit.convert.engine import Spec
from musictoolkit.discs.helper import availability
from musictoolkit.ingest import organizer
from musictoolkit.sync import selection
from musictoolkit.web.context import AppContext, get_ctx
from musictoolkit.web.jobs import JobHandle
from musictoolkit.web.routers.organize import FIELD_HELP, PRESETS

router = APIRouter(prefix="/api/tools/convert")

LAST_KEY = "convert.last"
MAX_SONGS = 100_000
CHUNK = 500


class ConvertBody(BaseModel):
    format: str
    quality: str | None = None
    destination: str = Field(default="", max_length=1000)
    scheme: str | None = Field(default=None, max_length=500)  # None: the library's own layout
    max_rate: int = 0
    depth: str = "16"
    existing: str = "copy"
    cover: bool = True
    sources: list[dict[str, Any]] = Field(default_factory=list, max_length=selection.MAX_SOURCES)
    song_ids: list[int] = Field(default_factory=list, max_length=MAX_SONGS)


class UndoBody(BaseModel):
    conversion_id: int


def _spec(body: ConvertBody) -> Spec:
    try:
        return Spec(body.format, body.quality, body.max_rate, body.depth, body.existing).checked()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _scheme(ctx: AppContext, body: ConvertBody) -> str:
    scheme = (body.scheme or ctx.config.library.canonical_scheme).strip()
    try:
        organizer.validate_scheme(scheme)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return scheme


def _checked(ctx: AppContext, body: ConvertBody) -> tuple[Spec, str]:
    spec, scheme = _spec(body), _scheme(ctx, body)
    if not body.sources and not body.song_ids:
        raise HTTPException(status_code=422, detail="Choose what to convert.")
    if body.sources:
        try:
            selection.validate_sources(body.sources)
        except selection.BadSource as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    return spec, scheme


def _key(body: ConvertBody) -> str:
    return hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()


def _rows(conn: sqlite3.Connection, body: ConvertBody) -> list[sqlite3.Row]:
    """The songs the choices add up to: the sources, then the songs picked one by one, without repeats and without those known to be missing."""
    found: dict[int, sqlite3.Row] = {}
    if body.sources:
        for row in selection.resolve_sources(conn, body.sources):
            found.setdefault(row["id"], row)
    picked = list(dict.fromkeys(body.song_ids))
    by_id: dict[int, sqlite3.Row] = {}
    for first in range(0, len(picked), CHUNK):
        chunk = picked[first : first + CHUNK]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT * FROM tracks WHERE is_missing = 0 AND id IN ({marks})", chunk):
            by_id[row["id"]] = row
    for song_id in picked:
        if song_id in by_id:
            found.setdefault(song_id, by_id[song_id])
    return list(found.values())


def _last(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (LAST_KEY,)).fetchone()
    try:
        saved = json.loads(row["value"]) if row else {}
    except ValueError:
        saved = {}
    return saved if isinstance(saved, dict) else {}


def _remember(conn: sqlite3.Connection, body: ConvertBody) -> None:
    choices = {"format": body.format, "quality": body.quality, "destination": body.destination, "scheme": body.scheme, "max_rate": body.max_rate,
               "depth": body.depth, "existing": body.existing, "cover": body.cover}
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (LAST_KEY, json.dumps(choices)))
    conn.commit()


def _busy(ctx: AppContext) -> str | None:
    if ctx.jobs.is_busy("convert") or ctx.jobs.is_busy("convert-undo"):
        return "Songs are already being converted."
    if ctx.jobs.is_busy("organize") or ctx.jobs.is_busy("organize-undo"):
        return "Files in the library are being moved: wait until that is done."
    return None


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


@router.get("/info")
def info(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db() as conn:
        last = _last(conn)
    ok, reason = availability()
    return {
        "formats": [
            {"id": f.id, "label": f.label, "extension": f.extension, "lossy": f.lossy, "blurb": f.blurb, "default": f.default, "depth": not f.lossy,
             "qualities": [{"id": q.id, "label": q.label} for q in f.qualities]}
            for f in formats.FORMATS.values()
        ],
        "max_rates": list(formats.MAX_RATES), "depths": list(formats.DEPTHS), "existing": list(formats.EXISTING),
        "presets": PRESETS, "fields": [{"name": name, "help": text} for name, text in FIELD_HELP], "default_scheme": ctx.config.library.canonical_scheme,
        "roots": list(ctx.config.library.roots), "last": last, "default_destination": str(folder_default()), "busy": _busy(ctx), "windows_decoding": {"available": ok, "reason": reason},
    }


@router.post("/preview")
def preview(body: ConvertBody, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    spec, scheme = _checked(ctx, body)
    key = _key(body)

    def run(handle: JobHandle) -> dict[str, Any]:
        def progress(done: int, total: int) -> None:
            handle.update(done=done, total=total, message=f"Looked at {done:,} of {_plural(total, 'song')}")

        handle.update(message="Finding the songs…")
        with ctx.db() as conn:
            rows = _rows(conn, body)
        plan = planning.build_plan(rows, spec, body.destination, scheme, ctx.config.library.roots, progress=progress, check=handle.check, cover=body.cover)
        ctx.previews["convert"] = {"job_id": handle.id, "key": key, "plan": plan}
        return plan.summary()

    return {"job": ctx.jobs.submit("convert-preview", "Looking at the songs to convert", run, lane="analyze").to_dict()}


@router.get("/preview/{job_id}")
def preview_page(job_id: str, action: str = "convert", offset: int = 0, limit: int = planning.PAGE, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """A page of the songs the preview sorted under one action (convert, copy, skip, exists, clash or cannot)."""
    held = ctx.previews.get("convert")
    if not held or held["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="That preview is out of date. Preview again.")
    try:
        return held["plan"].page(action, offset, limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/start")
def start(body: ConvertBody, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    spec, scheme = _checked(ctx, body)
    held = ctx.previews.get("convert")
    if not held or held["key"] != _key(body):
        raise HTTPException(status_code=409, detail="Preview the conversion first. Nothing is written until you have seen what will happen.")
    shown: planning.ConvertPlan = held["plan"]
    if not shown.ok:
        raise HTTPException(status_code=409, detail=(shown.problems or ["There is nothing to convert."])[0])
    busy = _busy(ctx)
    if busy:
        raise HTTPException(status_code=409, detail=busy)
    if ctx.previews.pop("convert", None) is not held:  # what was shown is used up; if another request took it in the same instant, this one stops
        raise HTTPException(status_code=409, detail="Preview the conversion first. Nothing is written until you have seen what will happen.")
    expected = len(shown.writing)
    with ctx.db() as conn:
        _remember(conn, body)

    def run(handle: JobHandle) -> dict[str, Any]:
        def make_plan(progress, check) -> planning.ConvertPlan:
            with ctx.db() as conn:
                rows = _rows(conn, body)
            return planning.build_plan(rows, spec, body.destination, scheme, ctx.config.library.roots, progress=progress, check=check, cover=body.cover)

        return job.run_conversion(
            db=ctx.db, make_plan=make_plan, request=job.Request(spec, body.destination, scheme, body.cover, previewed=expected), batch_id=uuid.uuid4().hex[:12],
            scratch=ctx.data_dir / "cache" / "convert", handle=handle,
        )

    label = formats.FORMATS[spec.format].label
    return {"job": ctx.jobs.submit("convert", f"Converting {_plural(expected, 'song')} to {label}", run, lane="convert").to_dict()}


@router.get("/batches")
def batches(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db() as conn:
        return {"batches": store.list_conversions(conn)}


@router.get("/batches/{conversion_id}")
def batch(conversion_id: int, offset: int = 0, limit: int = planning.PAGE, only: str | None = None, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """One conversion and a page of its songs; `only=not_made` narrows them to the ones that failed or were left out."""
    try:
        with ctx.db() as conn:
            found = store.get_conversion(conn, conversion_id, offset, limit, only)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if found is None:
        raise HTTPException(status_code=404, detail="That conversion is not in the history.")
    return found


@router.post("/undo")
def undo(body: UndoBody, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db() as conn:
        found = store.get_conversion(conn, body.conversion_id, limit=1)
        files = len(store.done_items(conn, body.conversion_id)) if found else 0
    if found is None:
        raise HTTPException(status_code=404, detail="That conversion is not in the history.")
    if found["undone_at"]:
        raise HTTPException(status_code=409, detail="That conversion has been taken back already.")
    if not files:
        raise HTTPException(status_code=409, detail="That conversion made no files to take back.")
    busy = _busy(ctx)
    if busy:
        raise HTTPException(status_code=409, detail=busy)

    def run(handle: JobHandle) -> dict[str, Any]:
        return job.undo_conversion(db=ctx.db, conversion_id=body.conversion_id, handle=handle)

    return {"job": ctx.jobs.submit("convert-undo", f"Taking back {_plural(files, 'converted song')}", run, lane="convert").to_dict()}


def folder_default() -> Path:
    """Where the converted songs go to begin with: a folder of their own beside the person's Music folder, not inside it (which is where a
    library usually is, and a converted copy inside the library would show up there as a second song)."""
    return Path.home() / "Converted music"
