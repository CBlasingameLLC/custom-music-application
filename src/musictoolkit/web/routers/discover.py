from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from musictoolkit.db.connection import connect
from musictoolkit.recommend import review_queue, service
from musictoolkit.web.jobs import JobHandle
from musictoolkit.web.context import AppContext, get_ctx

router = APIRouter(prefix="/api/recommendations")


def _links(row: Any) -> list[dict[str, str]]:
    """Where to listen or buy. Acquiring music stays a deliberate manual step."""
    query = quote_plus(f"{row['artist_name']} {row['track_name'] or ''}".strip())
    links = [
        {"label": "Bandcamp", "url": f"https://bandcamp.com/search?q={query}"},
        {"label": "YouTube", "url": f"https://www.youtube.com/results?search_query={query}"},
    ]
    if row["musicbrainz_artist_id"]:
        links.append({"label": "MusicBrainz", "url": f"https://musicbrainz.org/artist/{row['musicbrainz_artist_id']}"})
    return links


def _dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "artist": row["artist_name"],
        "track": row["track_name"],
        "source": row["source"],
        "score": row["score"],
        "reason": row["reason"],
        "status": row["status"],
        "suggested": row["date_suggested"],
        "reviewed": row["date_reviewed"],
        "links": _links(row),
    }


@router.get("")
def list_recommendations(
    status: str = "new", limit: int = 100, offset: int = 0, ctx: AppContext = Depends(get_ctx)
) -> dict:
    if status not in ("new", "owned", "dismissed", "accepted", "all"):
        raise HTTPException(status_code=422, detail="unknown status")
    where, params = ("", []) if status == "all" else ("WHERE status = ?", [status])
    with ctx.db() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM recommendations {where}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM recommendations {where} ORDER BY score DESC, id DESC LIMIT ? OFFSET ?",
            [*params, max(1, min(limit, 500)), max(offset, 0)],
        ).fetchall()
        counts = {
            r["status"]: r["n"]
            for r in conn.execute("SELECT status, COUNT(*) AS n FROM recommendations GROUP BY status")
        }
    return {"total": total, "items": [_dict(r) for r in rows], "counts": counts}


class StatusBody(BaseModel):
    status: Literal["owned", "dismissed", "accepted", "new"]


@router.post("/{recommendation_id}/status")
def set_status(recommendation_id: int, body: StatusBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        if conn.execute("SELECT 1 FROM recommendations WHERE id = ?", (recommendation_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="recommendation not found")
        if body.status == "new":  # undo a triage decision
            conn.execute("UPDATE recommendations SET status = 'new', date_reviewed = NULL WHERE id = ?", (recommendation_id,))
            conn.commit()
        else:
            review_queue.set_status(conn, recommendation_id, body.status)
    return {"ok": True}


class RefreshBody(BaseModel):
    source: Literal["listenbrainz", "lastfm", "both"] = "listenbrainz"
    limit: int = 20


@router.post("/refresh")
def refresh_recommendations(body: RefreshBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Fetch fresh recommendations in the background; the Tasks tray shows progress."""
    if ctx.jobs.is_busy("recommend"):
        raise HTTPException(status_code=409, detail="Recommendations are already being fetched")

    def run(handle: JobHandle) -> dict:
        conn = connect(ctx.db_path)
        try:
            result = service.refresh(
                conn, ctx.config, body.source, max(1, min(body.limit, 100)), progress=lambda m: handle.update(message=m)
            )
        finally:
            conn.close()
        if not result.attempted:
            raise RuntimeError(result.messages[0] if result.messages else "Nothing to fetch from.")
        for message in result.messages:
            handle.log(message)
        return {
            "fetched": result.fetched, "already_owned": result.already_owned, "new": result.new,
            "saved": result.saved, "messages": result.messages,
        }

    return {"job": ctx.jobs.submit("recommend", "Getting recommendations", run).to_dict()}
