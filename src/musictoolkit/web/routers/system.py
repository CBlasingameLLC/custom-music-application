from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from musictoolkit import __version__
from musictoolkit.web.context import AppContext, get_ctx

router = APIRouter(prefix="/api")


@router.get("/about")
def about(ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        tracks = conn.execute("SELECT COUNT(*) AS c FROM tracks WHERE is_missing = 0").fetchone()["c"]
    return {
        "version": __version__,
        "tracks": tracks,
        "data_dir": str(ctx.data_dir),
        "db_path": str(ctx.db_path),
        "config_path": str(ctx.config_path),
        "log_dir": str(ctx.log_dir),
        "db_size": ctx.db_path.stat().st_size if ctx.db_path.exists() else 0,
        "auth": ctx.token is not None,
    }


@router.get("/jobs")
def list_jobs(ctx: AppContext = Depends(get_ctx)) -> dict:
    return {"jobs": [job.to_dict() for job in reversed(ctx.jobs.list())]}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    job = ctx.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_dict(include_log=True)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    if not ctx.jobs.cancel(job_id):
        raise HTTPException(status_code=409, detail="job is not running")
    return {"ok": True}


@router.post("/system/backup")
def backup_database(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Consistent copy of the library database (play history, playlists, ratings
    live only here), via SQLite's online backup so WAL contents are included."""
    target_dir = ctx.data_dir / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"library-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    source = sqlite3.connect(ctx.db_path, timeout=30)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    return {"path": str(target), "size": target.stat().st_size}


@router.get("/system/logs")
def read_logs(tail: int = 300, ctx: AppContext = Depends(get_ctx)) -> dict:
    log_file = ctx.log_dir / "musictoolkit.log"
    if not log_file.exists():
        return {"path": str(log_file), "lines": []}
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"path": str(log_file), "lines": lines[-max(1, min(tail, 2000)):]}
