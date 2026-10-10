from __future__ import annotations

import logging
import platform
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException

from musictoolkit import __version__
from musictoolkit.sync import device_detect, mtp
from musictoolkit.web.context import AppContext, get_ctx

logger = logging.getLogger("musictoolkit")

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


def _section(probe: Callable[[], Any]) -> Any:
    """One part of the report. A part that cannot be worked out says why instead of spoiling the rest."""
    try:
        return probe()
    except Exception as exc:  # a report that fails to appear helps nobody
        logger.warning("Diagnostics: a check failed", exc_info=exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


@router.get("/system/diagnostics")
def diagnostics(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Everything useful for working out why something is not working, in one place. Never any token, key or email."""

    def database() -> dict:
        with ctx.db() as conn:
            def count(sql: str) -> int:
                return conn.execute(sql).fetchone()[0]

            return {
                "size": ctx.db_path.stat().st_size if ctx.db_path.exists() else 0,
                "schema_version": count("PRAGMA user_version"),
                "songs": count("SELECT COUNT(*) FROM tracks WHERE is_missing = 0"),
                "missing": count("SELECT COUNT(*) FROM tracks WHERE is_missing = 1"),
                "in_review_folder": count("SELECT COUNT(*) FROM tracks WHERE is_missing = 2"),
                "plays": count("SELECT COUNT(*) FROM play_history"),
                "playlists": count("SELECT COUNT(*) FROM playlists"),
            }

    def library() -> list[dict]:
        out = []
        for root in ctx.config.library.roots:
            folder = Path(root)
            info: dict[str, Any] = {"path": root, "available": folder.is_dir(), "free": None, "total": None}
            if info["available"]:
                try:
                    usage = shutil.disk_usage(folder)
                    info["free"], info["total"] = usage.free, usage.total
                except OSError:
                    pass
            out.append(info)
        return out

    def drives() -> list[dict]:
        return [
            {"mount_path": c.mountpoint, "fs": c.fstype, "total": c.total_bytes, "free": c.free_bytes, "removable": c.likely_removable}
            for c in device_detect.list_candidate_devices()
        ]

    def devices() -> list[dict]:
        with ctx.db() as conn:
            rows = conn.execute(
                "SELECT d.label, d.kind, d.last_seen_mount_path AS path, d.mtp_serial, d.mtp_storage, d.mtp_storage_name, d.last_synced_at, "
                "(SELECT COUNT(*) FROM sync_manifest m WHERE m.device_id = d.id AND m.status = 'synced') AS synced "
                "FROM devices d ORDER BY d.id"
            ).fetchall()
        plugged_in = mtp.list_devices()["devices"] if any(r["kind"] == "mtp" for r in rows) else []
        out = []
        for r in rows:
            if r["kind"] == "mtp":
                connected = mtp.find(plugged_in, r["mtp_serial"], r["mtp_storage"], r["mtp_storage_name"])[1] is not None
            else:
                connected = Path(r["path"]).is_dir()
            out.append({
                "label": r["label"], "kind": r["kind"], "path": r["path"] if r["kind"] != "mtp" else None, "connected": connected,
                "synced": r["synced"], "last_synced_at": r["last_synced_at"],
            })
        return out

    def phones() -> dict:
        ready, reason = mtp.availability()
        return {"supported": sys.platform == "win32", "available": ready, "reason": reason}

    def services() -> dict:
        cfg = ctx.config
        return {
            "listenbrainz": {
                "enabled": cfg.listenbrainz.enabled, "has_token": bool(cfg.listenbrainz.user_token.strip()),
                "has_username": bool(cfg.listenbrainz.username.strip()), "scrobble": cfg.listenbrainz.scrobble,
                "scrobbler": ctx.scrobbler.status() if ctx.scrobbler else None,
            },
            "lastfm": {"enabled": cfg.lastfm.enabled, "has_key": bool(cfg.lastfm.api_key.strip())},
            "musicbrainz": {"has_contact": bool(cfg.musicbrainz.contact.strip())},
        }

    def jobs() -> dict:
        everything = ctx.jobs.list()
        failed = [j for j in everything if j.status == "error"][-5:]
        return {
            "running": sum(1 for j in everything if j.status in ("queued", "running")),
            "recent_failures": [{"title": j.title, "error": (j.error or "")[:300], "finished_at": j.finished_at} for j in reversed(failed)],
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "app": {
            "version": __version__, "python": sys.version.split()[0], "platform": platform.platform(),
            "installed": bool(getattr(sys, "frozen", False)),
        },
        "paths": {"data_dir": str(ctx.data_dir), "database": str(ctx.db_path), "config": str(ctx.config_path), "logs": str(ctx.log_dir), "home": str(Path.home())},
        "database": _section(database),
        "library": _section(library),
        "drives": _section(drives),
        "devices": _section(devices),
        "phones": _section(phones),
        "services": _section(services),
        "jobs": _section(jobs),
        "settings": {"auto_update": ctx.config.app.auto_update, "rescan_on_launch": ctx.config.app.rescan_on_launch},
    }
