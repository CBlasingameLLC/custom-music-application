from __future__ import annotations

import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from musictoolkit.db.connection import connect
from musictoolkit.ingest import scanner
from musictoolkit.web.context import AppContext, get_ctx
from musictoolkit.web.jobs import Job, JobHandle

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
