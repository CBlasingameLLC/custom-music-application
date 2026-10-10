"""New releases by artists you play: the list, a way to dismiss one, and a button to look again."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from musictoolkit.recommend import radar
from musictoolkit.web.context import AppContext, get_ctx
from musictoolkit.web.radar_job import submit_refresh

router = APIRouter(prefix="/api/releases")


def _item(row: Any, today: Any) -> dict[str, Any]:
    day = radar._day(row["release_date"])
    return {
        "id": row["id"], "artist": row["artist"], "title": row["title"], "kind": row["kind"], "date": row["release_date"],
        "upcoming": bool(day and day > today), "source": row["source"], "plays": row["plays"], "art": row["art_url"],
        "links": radar.links(row["artist"], row["title"], row["release_group_mbid"]),
    }


@router.get("")
def list_releases(status: Literal["new", "dismissed"] = "new", ctx: AppContext = Depends(get_ctx)) -> dict:
    today = datetime.now(timezone.utc).date()
    with ctx.db() as conn:
        rows = conn.execute("SELECT * FROM fresh_releases WHERE status = ? ORDER BY release_date DESC, plays DESC, id DESC", (status,)).fetchall()
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM fresh_releases GROUP BY status")}
        kept = radar.state(conn)
    cfg = ctx.config
    return {
        "items": [_item(r, today) for r in rows],
        "counts": {"new": counts.get("new", 0), "dismissed": counts.get("dismissed", 0)},
        "state": kept,
        "enabled": cfg.app.release_radar,
        "sources": {"listenbrainz": bool(cfg.listenbrainz.enabled and cfg.listenbrainz.username), "musicbrainz": bool(cfg.musicbrainz.contact.strip())},
        "running": ctx.jobs.is_busy("radar"),
    }


@router.post("/refresh")
def refresh_releases(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Look now, in the background (the Tasks tray shows progress), whether or not the automatic daily look is on."""
    job = submit_refresh(ctx)
    if job is None:
        raise HTTPException(status_code=409, detail="Already looking for new releases")
    return {"job": job.to_dict()}


class StatusBody(BaseModel):
    status: Literal["new", "dismissed"]


@router.post("/{release_id}/status")
def set_status(release_id: int, body: StatusBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        done = conn.execute("UPDATE fresh_releases SET status = ? WHERE id = ?", (body.status, release_id)).rowcount
        conn.commit()
    if not done:
        raise HTTPException(status_code=404, detail="release not found")
    return {"ok": True}
