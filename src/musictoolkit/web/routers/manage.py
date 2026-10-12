from __future__ import annotations

import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from musictoolkit.db.connection import connect
from musictoolkit.ingest import scanner
from musictoolkit.web.context import AppContext, get_ctx
from musictoolkit.web.jobs import Job, JobHandle
from musictoolkit.web.queries import tracks_by_ids

logger = logging.getLogger("musictoolkit")

router = APIRouter(prefix="/api")


def submit_scan(ctx: AppContext, roots: list[str], title: str = "Scanning library") -> Job:
    """Queue a scan of the given folders. Folders that are not there (an unplugged
    drive) are skipped, not treated as 'everything was deleted'."""
    for job in ctx.jobs.active():
        if job.kind == "scan":
            return job  # one scan at a time is plenty; hand back the running one

    def run(handle: JobHandle) -> dict[str, Any]:
        totals = scanner.ScanResult()
        offline: list[str] = []
        conn = connect(ctx.db_path)
        try:
            for root in roots:
                handle.check()
                if not os.path.isdir(root):
                    offline.append(root)
                    handle.log(f"Skipped {root}: folder not found (is the drive connected?)")
                    continue

                def progress(done: int, total: int, root: str = root) -> None:
                    handle.update(done=done, total=total, message=f"{Path(root).name or root}: {done} of {total} files")
                    handle.check()

                result = scanner.scan_library(conn, Path(root), on_progress=progress)
                for key, value in asdict(result).items():
                    setattr(totals, key, getattr(totals, key) + value)
                handle.log(f"{root}: {result}")
        finally:
            conn.close()
        return {**asdict(totals), "offline_roots": offline}

    return ctx.jobs.submit("scan", title, run)


@router.post("/library/scan")
def start_scan(ctx: AppContext = Depends(get_ctx)) -> dict:
    if not ctx.config.library.roots:
        raise HTTPException(status_code=409, detail="Add a music folder first")
    return {"job": submit_scan(ctx, list(ctx.config.library.roots)).to_dict()}


class OpenFiles(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=500)


SKIPPED_BECAUSE = {
    "not_audio": "Not an audio file",
    "missing": "File not found",
    "unreadable": "Could not be read",
}


def _reason(outcome: str) -> str:
    if outcome == "too_many":
        return f"Only the first {scanner.MAX_OPENED:,} songs in it were added"
    return SKIPPED_BECAUSE.get(outcome, outcome)


@router.post("/library/open-files")
def open_files(body: OpenFiles, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Songs the person opened from outside the app (Open with in Explorer, files dropped on the window): put them in
    the library if they are not there yet and answer them as tracks, in the order given, ready to play. A folder stands
    for the songs inside it."""
    with ctx.db() as conn:
        outcomes = scanner.add_files(conn, body.paths)
        ids = list(dict.fromkeys(track_id for _, track_id, _ in outcomes if track_id is not None))
        tracks = tracks_by_ids(conn, ids)
    skipped = [{"path": path, "reason": _reason(outcome)} for path, track_id, outcome in outcomes if track_id is None]
    return {"tracks": tracks, "skipped": skipped, "added": sum(1 for _, _, outcome in outcomes if outcome == "added")}
