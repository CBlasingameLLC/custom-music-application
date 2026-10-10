from __future__ import annotations

import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from musictoolkit.history import spotify_import
from musictoolkit.integrations.listenbrainz_client import ListenBrainzError
from musictoolkit.web.context import AppContext, get_ctx
from musictoolkit.web.jobs import JobHandle
from musictoolkit.web.scrobbler import describe_failure

router = APIRouter(prefix="/api/history")


@router.get("/top-artists")
def top_artists(limit: int = 15, days: int | None = None, ctx: AppContext = Depends(get_ctx)) -> dict:
    where = "WHERE raw_artist_name IS NOT NULL"
    params: list = []
    if days:
        where += " AND played_at_epoch >= CAST(strftime('%s', 'now') AS INTEGER) - ? * 86400"
        params.append(days)
    with ctx.db() as conn:
        rows = conn.execute(
            f"SELECT raw_artist_name AS name, COUNT(*) AS plays FROM play_history {where} "
            f"GROUP BY raw_artist_name ORDER BY plays DESC LIMIT ?",
            [*params, max(1, min(limit, 100))],
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS c FROM play_history").fetchone()["c"]
    return {"total_plays": total, "items": [{"name": r["name"], "plays": r["plays"]} for r in rows]}


@router.get("/recent")
def recent(limit: int = 50, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        rows = conn.execute(
            "SELECT id, track_id, source, played_at_epoch, ms_played, raw_artist_name, raw_track_name, raw_album_name "
            "FROM play_history ORDER BY played_at_epoch DESC, id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
    return {
        "items": [
            {
                "id": r["id"], "track_id": r["track_id"], "source": r["source"], "played_at": r["played_at_epoch"],
                "ms_played": r["ms_played"], "artist": r["raw_artist_name"], "title": r["raw_track_name"],
                "album": r["raw_album_name"],
            }
            for r in rows
        ]
    }


# ------------------------------------------------------------------------------------------- Spotify import


class ExportBody(BaseModel):
    path: str


class ApplyBody(BaseModel):
    job_id: str


def _export_source(raw: str) -> Path:
    path = Path(raw.strip().strip('"')).expanduser()
    if not path.exists():
        raise HTTPException(status_code=422, detail=f"Not found: {raw}")
    if path.is_dir() or (path.is_file() and path.suffix.lower() == ".zip"):
        return path
    raise HTTPException(status_code=422, detail="Choose the ZIP file Spotify sent you, or the folder you unzipped it into.")


def _stamp(path: Path) -> tuple:
    """Enough about an export to notice it changing between looking at it and importing it."""
    if path.is_file():
        info = path.stat()
        return (info.st_size, info.st_mtime_ns)
    files = list(path.rglob(spotify_import._AUDIO_HISTORY_GLOB))
    return (len(files), sum(f.stat().st_size for f in files), max((f.stat().st_mtime_ns for f in files), default=0))


@contextmanager
def _work_dir(ctx: AppContext) -> Iterator[Path]:
    """A scratch folder for unpacking a ZIP, inside the app's own data folder and removed afterwards."""
    parent = ctx.data_dir / "tmp"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spotify-", dir=parent) as work:
        yield Path(work)


@router.post("/import/preview")
def import_preview(body: ExportBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Read a Spotify export and report what is in it. Nothing is added to the history yet."""
    source = _export_source(body.path)

    def run(handle: JobHandle) -> dict:
        handle.update(message="Reading the export…")
        with _work_dir(ctx) as work:
            try:
                summary = spotify_import.preview(source, work)
            except ValueError as exc:
                raise RuntimeError(str(exc)) from None
        result = summary.to_dict()
        ctx.previews["spotify-import"] = {"job_id": handle.id, "path": str(source), "stamp": _stamp(source)}
        return result

    return {"job": ctx.jobs.submit("import-preview", "Reading your Spotify export", run, lane="analyze").to_dict()}


@router.post("/import/apply")
def import_apply(body: ApplyBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Add the plays from the export that was just read. Repeating an import, or importing an overlapping export, adds nothing twice."""
    held = ctx.previews.get("spotify-import")
    if not held or held["job_id"] != body.job_id:
        raise HTTPException(status_code=409, detail="Read the export first. Nothing is added to your history until you have seen what is in it.")
    source = Path(held["path"])
    if not source.exists() or _stamp(source) != held["stamp"]:
        raise HTTPException(status_code=409, detail="That export changed since you looked at it. Read it again.")
    if ctx.jobs.is_busy("import-spotify"):
        raise HTTPException(status_code=409, detail="An import is already running")

    def run(handle: JobHandle) -> dict:
        def progress(done: int, total: int) -> None:
            handle.update(done=done, total=total, message=f"Added {done:,} of {total:,} plays")
            handle.check()  # stopping here adds nothing: the plays are committed together at the end

        handle.update(message="Reading the export…")
        with _work_dir(ctx) as work, ctx.db() as conn:
            try:
                summary = spotify_import.import_history(conn, source, work, on_progress=progress)
            except ValueError as exc:
                raise RuntimeError(str(exc)) from None
        ctx.previews.pop("spotify-import", None)
        return summary.to_dict()

    return {"job": ctx.jobs.submit("import-spotify", "Importing your Spotify history", run).to_dict()}


def _imported_at(batch_id: str | None) -> str | None:
    try:
        return datetime.strptime(batch_id or "", "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


@router.get("/imports")
def imports(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Earlier imports, newest first, and how many imported plays ListenBrainz has not been given yet."""
    with ctx.db() as conn:
        rows = conn.execute(
            "SELECT import_batch_id AS batch_id, COUNT(*) AS plays, MIN(played_at_epoch) AS first_play, "
            "MAX(played_at_epoch) AS last_play, SUM(listenbrainz_submitted = 1) AS sent, SUM(track_id IS NOT NULL) AS matched "
            "FROM play_history WHERE source = 'spotify_import' GROUP BY import_batch_id ORDER BY MIN(id) DESC"
        ).fetchall()
        unsent = spotify_import.unsent_count(conn)
    cfg = ctx.config.listenbrainz
    return {
        "items": [
            {"batch_id": r["batch_id"], "imported_at": _imported_at(r["batch_id"]), "plays": r["plays"], "first_play": r["first_play"],
             "last_play": r["last_play"], "sent": r["sent"] or 0, "matched": r["matched"] or 0}
            for r in rows
        ],
        "unsent": unsent,
        "can_send": bool(cfg.enabled and cfg.user_token.strip()),
    }


@router.delete("/imports/{batch_id}")
def undo_import(batch_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Remove the plays one import added. Plays already sent to ListenBrainz stay there; this only clears them here."""
    with ctx.db() as conn:
        found = conn.execute(
            "SELECT COUNT(*) AS n, SUM(listenbrainz_submitted = 1) AS sent FROM play_history WHERE source = 'spotify_import' AND import_batch_id = ?",
            (batch_id,),
        ).fetchone()
        if not found["n"]:
            raise HTTPException(status_code=404, detail="That import is not in your history")
        conn.execute("DELETE FROM play_history WHERE source = 'spotify_import' AND import_batch_id = ?", (batch_id,))
        conn.commit()
    return {"removed": found["n"], "already_sent": found["sent"] or 0}


def _pause(handle: JobHandle, seconds: float) -> None:
    """ListenBrainz asked us to wait; stay responsive to Cancel meanwhile."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        handle.check()
        handle.update(message="ListenBrainz asked for a short pause…")
        time.sleep(min(1.0, max(0.0, end - time.monotonic())))


@router.post("/listenbrainz/backfill")
def backfill(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Send imported plays to the person's own ListenBrainz account. Resumable: what went through stays marked as sent."""
    cfg = ctx.config.listenbrainz
    token = cfg.user_token.strip()
    if not (cfg.enabled and token):
        raise HTTPException(status_code=409, detail="Add your ListenBrainz token in Settings first, and leave ListenBrainz switched on.")
    if ctx.jobs.is_busy("lb-backfill"):
        raise HTTPException(status_code=409, detail="Your history is already being sent")

    def run(handle: JobHandle) -> dict:
        refused: list = []

        def progress(done: int, total: int) -> None:
            handle.update(done=done, total=total, message=f"Sent {done:,} of {total:,} plays")
            handle.check()

        with ctx.db() as conn:
            handle.update(done=0, total=spotify_import.unsent_count(conn), message="Sending…")
            try:
                sent = spotify_import.backfill_to_listenbrainz(
                    conn, token, on_progress=progress, sleep=lambda seconds: _pause(handle, seconds), on_refused=refused.append
                )
            except ListenBrainzError as exc:
                raise RuntimeError(describe_failure(exc)) from None
            remaining = spotify_import.unsent_count(conn)
        return {"sent": sent, "refused": len(refused), "remaining": remaining}

    return {"job": ctx.jobs.submit("lb-backfill", "Sending your history to ListenBrainz", run, lane="network").to_dict()}
