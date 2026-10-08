from __future__ import annotations

from fastapi import APIRouter, Depends

from musictoolkit.web.context import AppContext, get_ctx

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
