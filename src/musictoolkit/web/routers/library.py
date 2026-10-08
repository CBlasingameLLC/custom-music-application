from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from musictoolkit.web import queries
from musictoolkit.web.context import AppContext, get_ctx
from musictoolkit.web.queries import (
    ALBUM_ARTIST_SQL,
    ALBUM_SQL,
    HISTORY_JOIN,
    album_key,
    build_selection,
    list_tracks,
    parse_rules,
    track_dict,
    tracks_by_ids,
)

router = APIRouter(prefix="/api")

MAX_PAGE = 1000
MAX_IDS = 100_000


# --- tracks ---------------------------------------------------------------


def _effective_sort(sort: str | None, playlist: int | None) -> str:
    """A playlist lists in its own order unless a column is chosen; everything else defaults to artist."""
    return sort or ("position" if playlist is not None else "artist")


@router.get("/tracks")
def get_tracks(
    q: str = "",
    sort: str | None = None,
    dir: str = "asc",
    offset: int = 0,
    limit: int = 200,
    rules: str | None = None,
    playlist: int | None = None,
    album: str | None = None,
    artist: str | None = None,
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    with ctx.db() as conn:
        sel = build_selection(
            conn, q=q, rules=parse_rules(rules), playlist_id=playlist, album=album, artist=artist,
            sort=_effective_sort(sort, playlist), direction=dir,
        )
        return list_tracks(conn, sel, max(offset, 0), max(1, min(limit, MAX_PAGE)))


@router.get("/tracks/ids")
def get_track_ids(
    q: str = "",
    sort: str | None = None,
    dir: str = "asc",
    rules: str | None = None,
    playlist: int | None = None,
    album: str | None = None,
    artist: str | None = None,
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    """Every id matching the filters, in display order: what 'Play all' and 'Shuffle' queue."""
    with ctx.db() as conn:
        sel = build_selection(
            conn, q=q, rules=parse_rules(rules), playlist_id=playlist, album=album, artist=artist,
            sort=_effective_sort(sort, playlist), direction=dir,
        )
        rows = conn.execute(
            f"SELECT t.id {sel.from_sql} ORDER BY {sel.order} LIMIT ?", [*sel.params, MAX_IDS]
        ).fetchall()
    return {"ids": [r["id"] for r in rows]}


class IdsBody(BaseModel):
    ids: list[int] = Field(max_length=MAX_IDS)


@router.post("/tracks/by-ids")
def post_tracks_by_ids(body: IdsBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        return {"items": tracks_by_ids(conn, body.ids)}


class BulkUpdate(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=MAX_IDS)
    rating: int | None = Field(default=None, ge=0, le=5)
    favorite: bool | None = None


@router.post("/tracks/bulk")
def bulk_update(body: BulkUpdate, ctx: AppContext = Depends(get_ctx)) -> dict:
    sets, params = [], []
    if body.rating is not None:
        sets.append("rating = ?")
        params.append(body.rating or None)  # 0 stars means "unrated"
    if body.favorite is not None:
        sets.append("favorite = ?")
        params.append(1 if body.favorite else 0)
    if not sets:
        raise HTTPException(status_code=422, detail="nothing to update")
    with ctx.db() as conn:
        for start in range(0, len(body.ids), 500):
            chunk = body.ids[start:start + 500]
            conn.execute(
                f"UPDATE tracks SET {', '.join(sets)} WHERE id IN ({', '.join('?' * len(chunk))})",
                [*params, *chunk],
            )
        conn.commit()
    return {"updated": len(body.ids)}


@router.get("/tracks/{track_id}")
def get_track(track_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        found = tracks_by_ids(conn, [track_id])
        if not found:
            raise HTTPException(status_code=404, detail="track not found")
        row = conn.execute(
            "SELECT file_path, file_size, musicbrainz_recording_id, tag_source FROM tracks WHERE id = ?", (track_id,)
        ).fetchone()
    path = Path(row["file_path"])
    return {
        **found[0],
        "path": str(path),
        "folder": str(path.parent),
        "file_size": row["file_size"],
        "musicbrainz_recording_id": row["musicbrainz_recording_id"],
        "tag_source": row["tag_source"],
    }


class TrackPatch(BaseModel):
    rating: int | None = Field(default=None, ge=0, le=5)
    favorite: bool | None = None


@router.patch("/tracks/{track_id}")
def patch_track(track_id: int, body: TrackPatch, ctx: AppContext = Depends(get_ctx)) -> dict:
    bulk_update(BulkUpdate(ids=[track_id], rating=body.rating, favorite=body.favorite), ctx)
    with ctx.db() as conn:
        found = tracks_by_ids(conn, [track_id])
    if not found:
        raise HTTPException(status_code=404, detail="track not found")
    return found[0]


# --- albums ---------------------------------------------------------------

ALBUM_ORDERS = {
    "name": "LOWER(al) {d}, LOWER(aa)",
    "artist": "LOWER(aa) {d}, year, LOWER(al)",
    "year": "(year IS NULL), year {d}, LOWER(aa)",
    "added": "added {d}",
    "tracks": "n {d}, LOWER(al)",
    "random": "RANDOM()",
}


def _album_dict(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "key": album_key(r["aa"], r["al"]),
        "album": r["al"],
        "artist": r["aa"],
        "year": r["year"],
        "tracks": r["n"],
        "duration": r["dur"] or 0,
        "added": r["added"],
        "cover_track_id": r["first_id"],
    }


@router.get("/albums")
def get_albums(
    q: str = "",
    sort: str = "name",
    dir: str = "asc",
    offset: int = 0,
    limit: int = 120,
    rules: str | None = None,
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    direction = "DESC" if dir == "desc" else "ASC"
    order = ALBUM_ORDERS.get(sort, ALBUM_ORDERS["name"]).format(d=direction)
    with ctx.db() as conn:
        sel = build_selection(conn, q=q, rules=parse_rules(rules))
        inner = (
            f"SELECT {ALBUM_ARTIST_SQL} AS aa, {ALBUM_SQL} AS al, t.year AS year, "
            f"t.duration_seconds AS dur, t.date_added AS added, t.id AS id {sel.from_sql}"
        )
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM (SELECT 1 FROM ({inner}) GROUP BY aa, al)", sel.params
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT aa, al, MIN(year) AS year, COUNT(*) AS n, SUM(dur) AS dur, MAX(added) AS added, "
            f"MIN(id) AS first_id FROM ({inner}) GROUP BY aa, al ORDER BY {order} LIMIT ? OFFSET ?",
            [*sel.params, max(1, min(limit, MAX_PAGE)), max(offset, 0)],
        ).fetchall()
    return {"total": total, "items": [_album_dict(r) for r in rows]}


@router.get("/albums/{key}")
def get_album(key: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    album_artist, album = queries.parse_album_key(key)
    with ctx.db() as conn:
        sel = build_selection(conn, album=key)
        listing = list_tracks(conn, sel, 0, MAX_PAGE)
    if not listing["items"]:
        raise HTTPException(status_code=404, detail="album not found")
    tracks = listing["items"]
    years = [t["year"] for t in tracks if t["year"]]
    return {
        "key": key,
        "album": album,
        "artist": album_artist,
        "year": min(years) if years else None,
        "genres": sorted({t["genre"] for t in tracks if t["genre"]}),
        "duration": sum(t["duration"] for t in tracks),
        "cover_track_id": tracks[0]["id"],
        "tracks": tracks,
    }


# --- artists --------------------------------------------------------------


@router.get("/artists")
def get_artists(
    q: str = "",
    sort: str = "name",
    offset: int = 0,
    limit: int = 200,
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    order = {"name": "LOWER(name)", "tracks": "n DESC, LOWER(name)", "albums": "albums DESC, LOWER(name)"}.get(
        sort, "LOWER(name)"
    )
    with ctx.db() as conn:
        sel = build_selection(conn, q=q)
        inner = f"SELECT {ALBUM_ARTIST_SQL} AS name, {ALBUM_SQL} AS al, t.id AS id, t.duration_seconds AS dur {sel.from_sql}"
        total = conn.execute(f"SELECT COUNT(*) AS c FROM (SELECT 1 FROM ({inner}) GROUP BY name)", sel.params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT name, COUNT(*) AS n, COUNT(DISTINCT al) AS albums, SUM(dur) AS dur, MIN(id) AS first_id "
            f"FROM ({inner}) GROUP BY name ORDER BY {order} LIMIT ? OFFSET ?",
            [*sel.params, max(1, min(limit, MAX_PAGE)), max(offset, 0)],
        ).fetchall()
    return {
        "total": total,
        "items": [
            {"name": r["name"], "tracks": r["n"], "albums": r["albums"], "duration": r["dur"] or 0,
             "cover_track_id": r["first_id"]}
            for r in rows
        ],
    }


@router.get("/artist")
def get_artist(name: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        sel = build_selection(conn, artist=name)
        count = conn.execute(f"SELECT COUNT(*) AS c {sel.from_sql}", sel.params).fetchone()["c"]
        if count == 0:
            raise HTTPException(status_code=404, detail="artist not found")
        inner = (
            f"SELECT {ALBUM_ARTIST_SQL} AS aa, {ALBUM_SQL} AS al, t.year AS year, t.duration_seconds AS dur, "
            f"t.date_added AS added, t.id AS id {sel.from_sql}"
        )
        albums = conn.execute(
            f"SELECT aa, al, MIN(year) AS year, COUNT(*) AS n, SUM(dur) AS dur, MAX(added) AS added, "
            f"MIN(id) AS first_id FROM ({inner}) GROUP BY aa, al ORDER BY (year IS NULL), year, LOWER(al)",
            sel.params,
        ).fetchall()
        top_sel = build_selection(conn, artist=name, sort="plays", direction="desc")
        top = list_tracks(conn, top_sel, 0, 10)
    return {
        "name": name,
        "tracks": count,
        "albums": [_album_dict(r) for r in albums],
        "top_tracks": top["items"],
    }


# --- facets, search, home -------------------------------------------------


@router.get("/facets")
def get_facets(ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        genres = conn.execute(
            "SELECT MIN(genre) AS name, COUNT(*) AS n FROM tracks "
            "WHERE is_missing = 0 AND genre IS NOT NULL AND genre <> '' GROUP BY LOWER(genre) ORDER BY n DESC, name"
        ).fetchall()
        years = conn.execute(
            "SELECT year, COUNT(*) AS n FROM tracks WHERE is_missing = 0 AND year IS NOT NULL "
            "GROUP BY year ORDER BY year DESC"
        ).fetchall()
        formats = conn.execute(
            "SELECT LOWER(format) AS name, COUNT(*) AS n FROM tracks WHERE is_missing = 0 AND format IS NOT NULL "
            "GROUP BY LOWER(format) ORDER BY n DESC"
        ).fetchall()
        totals = conn.execute(
            f"SELECT COUNT(*) AS tracks, COALESCE(SUM(t.duration_seconds), 0) AS duration, "
            f"COUNT(DISTINCT {ALBUM_ARTIST_SQL}) AS artists, COUNT(DISTINCT {ALBUM_ARTIST_SQL} || '|' || {ALBUM_SQL}) AS albums "
            f"FROM tracks t WHERE t.is_missing = 0"
        ).fetchone()
    return {
        "genres": [{"name": r["name"], "count": r["n"]} for r in genres],
        "years": [{"year": r["year"], "count": r["n"]} for r in years],
        "formats": [{"name": r["name"], "count": r["n"]} for r in formats],
        "totals": {k: totals[k] for k in ("tracks", "duration", "artists", "albums")},
    }


@router.get("/search")
def search(q: str, limit: int = 8, ctx: AppContext = Depends(get_ctx)) -> dict:
    limit = max(1, min(limit, 50))
    with ctx.db() as conn:
        sel = build_selection(conn, q=q, sort="plays", direction="desc")
        tracks = list_tracks(conn, sel, 0, limit)["items"]

        album_sel = build_selection(conn, q=q)
        inner = (
            f"SELECT {ALBUM_ARTIST_SQL} AS aa, {ALBUM_SQL} AS al, t.year AS year, t.duration_seconds AS dur, "
            f"t.date_added AS added, t.id AS id {album_sel.from_sql}"
        )
        albums = conn.execute(
            f"SELECT aa, al, MIN(year) AS year, COUNT(*) AS n, SUM(dur) AS dur, MAX(added) AS added, "
            f"MIN(id) AS first_id FROM ({inner}) GROUP BY aa, al ORDER BY n DESC LIMIT ?",
            [*album_sel.params, limit],
        ).fetchall()
        artists = conn.execute(
            f"SELECT name, COUNT(*) AS n, MIN(id) AS first_id FROM "
            f"(SELECT {ALBUM_ARTIST_SQL} AS name, t.id AS id {album_sel.from_sql}) GROUP BY name ORDER BY n DESC LIMIT ?",
            [*album_sel.params, limit],
        ).fetchall()
        like = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        playlists = conn.execute(
            "SELECT id, name, kind FROM playlists WHERE LOWER(name) LIKE ? ESCAPE '\\' ORDER BY name LIMIT ?",
            (like, limit),
        ).fetchall()
    return {
        "tracks": tracks,
        "albums": [_album_dict(r) for r in albums],
        "artists": [{"name": r["name"], "tracks": r["n"], "cover_track_id": r["first_id"]} for r in artists],
        "playlists": [{"id": r["id"], "name": r["name"], "kind": r["kind"]} for r in playlists],
    }


@router.get("/home")
def home(ctx: AppContext = Depends(get_ctx)) -> dict:
    played = {"rules": [{"field": "unplayed", "op": "is", "value": False}]}
    with ctx.db() as conn:
        def tracks(sort: str, rules: dict | None, limit: int = 12) -> list[dict]:
            sel = build_selection(conn, rules=rules, sort=sort, direction="desc")
            return list_tracks(conn, sel, 0, limit)["items"]

        def albums(order: str, limit: int = 12) -> list[dict]:
            inner = (
                f"SELECT {ALBUM_ARTIST_SQL} AS aa, {ALBUM_SQL} AS al, t.year AS year, t.duration_seconds AS dur, "
                f"t.date_added AS added, t.id AS id FROM tracks t WHERE t.is_missing = 0"
            )
            rows = conn.execute(
                f"SELECT aa, al, MIN(year) AS year, COUNT(*) AS n, SUM(dur) AS dur, MAX(added) AS added, "
                f"MIN(id) AS first_id FROM ({inner}) GROUP BY aa, al ORDER BY {order} LIMIT ?",
                (limit,),
            ).fetchall()
            return [_album_dict(r) for r in rows]

        totals = conn.execute(
            "SELECT COUNT(*) AS tracks, (SELECT COUNT(*) FROM play_history) AS plays FROM tracks WHERE is_missing = 0"
        ).fetchone()
        return {
            "totals": {"tracks": totals["tracks"], "plays": totals["plays"]},
            "recent": tracks("last_played", played),
            "most_played": tracks("plays", played, 10),
            "favorites": tracks("added", {"rules": [{"field": "favorite", "op": "is", "value": True}]}, 10),
            "recently_added": albums("added DESC"),
            "random_albums": albums("RANDOM()"),
        }
