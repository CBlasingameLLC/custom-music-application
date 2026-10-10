"""Organize files: preview a folder layout, apply it as one undoable batch, undo recent batches.

Nothing moves until a preview of the same folder and layout exists; applying then plans again against the
files as they are at that moment, so a stale preview can never move anything it did not show."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from musictoolkit.config import Config
from musictoolkit.ingest import moves, organizer
from musictoolkit.web.context import AppContext, get_ctx, same_path
from musictoolkit.web.jobs import JobHandle

router = APIRouter(prefix="/api/tools/organize")

PRESETS = [
    {"name": "Artist / Album / 01 - Title", "scheme": "{album_artist}/{album}/{track:02d} - {title}.{ext}"},
    {"name": "Artist / Year - Album / 01 - Title", "scheme": "{album_artist}/{year} - {album}/{track:02d} - {title}.{ext}"},
    {"name": "Artist / Album / 1-01 - Title (multi-disc albums)", "scheme": "{album_artist}/{album}/{disc}-{track:02d} - {title}.{ext}"},
    {"name": "Artist / Album / Title", "scheme": "{album_artist}/{album}/{title}.{ext}"},
    {"name": "Artist / Artist - Title (no album folders)", "scheme": "{album_artist}/{artist} - {title}.{ext}"},
]
FIELD_HELP = [
    ("album_artist", "The album's artist (falls back to the artist)"),
    ("artist", "The song's artist"),
    ("album", "Album title"),
    ("title", "Song title (falls back to the file name)"),
    ("track:02d", "Track number, two digits"),
    ("disc", "Disc number (1 when not tagged)"),
    ("year", "Release year (0 when not tagged)"),
    ("ext", "File type, such as mp3 (required)"),
]
SAMPLES = [
    {"album_artist": "Aurora Vale", "artist": "Aurora Vale", "album": "Northern Lights", "title": "Glacier",
     "track_number": 3, "disc_number": 1, "year": 2019, "file_path": "x/Glacier.flac"},
    {"album_artist": None, "artist": "Some: Band?", "album": None, "title": None,
     "track_number": None, "disc_number": 2, "year": None, "file_path": "x/raw-file.mp3"},
]
PREVIEW_PAGE = 100


class PlanBody(BaseModel):
    root: str
    scheme: str = Field(min_length=1, max_length=500)


class ApplyBody(PlanBody):
    remember: bool = True  # keep this layout as the library's layout in Settings


class UndoBody(BaseModel):
    batch_id: str


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _library_root(ctx: AppContext, requested: str) -> Path:
    match = next((r for r in ctx.config.library.roots if same_path(r, requested)), None)
    if match is None:
        raise HTTPException(status_code=404, detail="That folder is not in your library")
    root = Path(match)
    if not root.is_dir():
        raise HTTPException(status_code=409, detail="That folder is not available right now. Is its drive connected?")
    return root


def _checked(ctx: AppContext, body: PlanBody) -> tuple[Path, str]:
    root = _library_root(ctx, body.root)
    scheme = body.scheme.strip()
    try:
        organizer.validate_scheme(scheme)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return root, scheme


def _songs_in(conn, root: str) -> int:
    prefix = os.path.normpath(root).rstrip("\\/") + os.sep
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE is_missing = 0 AND file_path LIKE ? ESCAPE '\\'", (escaped + "%",)
    ).fetchone()[0]


@router.get("/info")
def info(ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        roots = [
            {"path": r, "songs": _songs_in(conn, r), "available": Path(r).is_dir()} for r in ctx.config.library.roots
        ]
        batches = moves.list_batches(conn, "organize")
    return {
        "roots": roots,
        "scheme": ctx.config.library.canonical_scheme,
        "default_scheme": Config().library.canonical_scheme,
        "presets": PRESETS,
        "fields": [{"name": name, "help": text} for name, text in FIELD_HELP],
        "batches": batches,
    }


@router.get("/example")
def example(scheme: str) -> dict:
    """What a couple of sample songs would be called under this layout, for the live example under the box."""
    scheme = scheme.strip()
    try:
        organizer.validate_scheme(scheme)
        base = Path("Music")
        examples = [organizer._target(row, base, scheme).relative_to(base).as_posix() for row in SAMPLES]
    except (ValueError, KeyError, IndexError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "examples": examples}


@router.post("/preview")
def preview(body: PlanBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    root, scheme = _checked(ctx, body)

    def run(handle: JobHandle) -> dict[str, Any]:
        def progress(done: int, total: int) -> None:
            handle.update(done=done, total=total, message=f"Checked {done:,} of {_plural(total, 'song')}")
            handle.check()

        with ctx.db() as conn:
            plan = organizer.propose_organization(conn, root, scheme, progress)
        ctx.previews["organize"] = {"job_id": handle.id, "root": str(root), "scheme": scheme, "plan": plan}
        return {
            "moves": len(plan.proposals), "unchanged": plan.unchanged, "collisions": len(plan.collisions),
            "too_long": len(plan.too_long), "unknown": plan.unknown,
            "root": str(root), "scheme": scheme,
        }

    return {"job": ctx.jobs.submit("organize-preview", f"Planning changes in {root.name or root}", run, lane="analyze").to_dict()}


@router.get("/preview/{job_id}")
def preview_page(job_id: str, kind: str = "moves", offset: int = 0, limit: int = PREVIEW_PAGE, ctx: AppContext = Depends(get_ctx)) -> dict:
    """A page of the planned changes (kind=moves), or of what was left out (kind=collisions or too_long)."""
    held = ctx.previews.get("organize")
    if not held or held["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="That preview is out of date. Preview again.")
    plan: organizer.OrganizeResult = held["plan"]
    root = Path(held["root"])
    if kind == "moves":
        pairs = [(p.old_path, p.new_path) for p in plan.proposals]
    elif kind == "collisions":
        pairs = plan.collisions
    elif kind == "too_long":
        pairs = plan.too_long
    else:
        raise HTTPException(status_code=422, detail="kind must be moves, collisions or too_long")
    offset, limit = max(0, offset), max(1, min(limit, 500))

    def shown(path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return str(path)

    return {
        "kind": kind, "total": len(pairs), "root": held["root"],
        "items": [{"from": shown(old), "to": shown(new)} for old, new in pairs[offset:offset + limit]],
    }


@router.post("/apply")
def apply(body: ApplyBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    root, scheme = _checked(ctx, body)
    held = ctx.previews.get("organize")
    if not held or not same_path(held["root"], str(root)) or held["scheme"] != scheme or not held["plan"].proposals:
        raise HTTPException(status_code=409, detail="Preview the changes first. Nothing moves until you have seen what will happen.")
    if ctx.jobs.is_busy("organize") or ctx.jobs.is_busy("organize-undo"):
        raise HTTPException(status_code=409, detail="Files are already being moved")
    if body.remember and ctx.config.library.canonical_scheme != scheme:
        ctx.config.library.canonical_scheme = scheme
        ctx.save_config()
    expected = len(held["plan"].proposals)

    def run(handle: JobHandle) -> dict[str, Any]:
        def progress(done: int, total: int) -> None:
            handle.update(done=done, total=total, message=f"Moved {done:,} of {_plural(total, 'song')}")
            handle.check()

        with ctx.db() as conn:
            handle.update(message="Checking the files one more time…")
            plan = organizer.propose_organization(conn, root, scheme)  # plan again: files may have changed since the preview
            batch = organizer.apply_with_log(conn, plan.proposals, root, progress)
        ctx.previews.pop("organize", None)  # what was shown has been used up
        return {
            **batch.to_dict(), "errors": batch.errors[:100], "errors_total": len(batch.errors),
            "planned": len(plan.proposals), "previewed": expected, "collisions": len(plan.collisions), "too_long": len(plan.too_long),
        }

    return {"job": ctx.jobs.submit("organize", f"Moving {_plural(expected, 'song')} into place", run).to_dict()}


@router.get("/batches")
def batches(ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        return {"batches": moves.list_batches(conn, "organize")}


@router.post("/undo")
def undo(body: UndoBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        files = conn.execute(
            "SELECT COUNT(*) FROM file_moves WHERE batch_id = ? AND kind = 'organize' AND undone_at IS NULL", (body.batch_id,)
        ).fetchone()[0]
    if not files:
        raise HTTPException(status_code=404, detail="Nothing is left to undo in that batch")
    if ctx.jobs.is_busy("organize") or ctx.jobs.is_busy("organize-undo"):
        raise HTTPException(status_code=409, detail="Files are already being moved")

    def run(handle: JobHandle) -> dict[str, Any]:
        def progress(done: int, total: int) -> None:
            handle.update(done=done, total=total, message=f"Restored {done:,} of {_plural(total, 'song')}")
            handle.check()

        roots = [Path(r).resolve() for r in ctx.config.library.roots]
        with ctx.db() as conn:
            result = moves.undo_batch(conn, body.batch_id, "organize", roots, progress)
        return {**result.to_dict(), "errors": result.errors[:100], "errors_total": len(result.errors)}

    return {"job": ctx.jobs.submit("organize-undo", f"Putting {_plural(files, 'song')} back", run).to_dict()}
