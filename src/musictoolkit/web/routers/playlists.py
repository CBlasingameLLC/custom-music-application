from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from musictoolkit.sync import playlist_import
from musictoolkit.web.context import AppContext, get_ctx
from musictoolkit.web.queries import build_rule_sql, build_selection, parse_rules

router = APIRouter(prefix="/api/playlists")

MAX_TRACKS = 100_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _playlist_row(conn: sqlite3.Connection, playlist_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="playlist not found")
    return row


def _summary(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    rules = parse_rules(row["rules_json"]) if row["kind"] == "smart" else None
    sel = build_selection(conn, playlist_id=row["id"], sort="position")
    totals = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(t.duration_seconds), 0) AS dur {sel.from_sql}", sel.params
    ).fetchone()
    covers = conn.execute(
        f"SELECT t.id {sel.from_sql} ORDER BY {sel.order} LIMIT 4", sel.params
    ).fetchall()
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "source": row["source"],
        "rules": rules,
        "tracks": totals["n"],
        "duration": totals["dur"],
        "updated_at": row["updated_at"] or row["created_at"],
        "cover_track_ids": [r["id"] for r in covers],
    }


@router.get("")
def list_playlists(ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        rows = conn.execute("SELECT * FROM playlists ORDER BY LOWER(name), id").fetchall()
        return {"items": [_summary(conn, r) for r in rows]}


class PlaylistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["manual", "smart"] = "manual"
    rules: dict[str, Any] | None = None
    track_ids: list[int] = Field(default_factory=list, max_length=MAX_TRACKS)


def _write_order(conn: sqlite3.Connection, playlist_id: int, track_ids: list[int]) -> None:
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
    conn.executemany(
        "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
        [(playlist_id, track_id, position) for position, track_id in enumerate(track_ids)],
    )
    conn.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (_now(), playlist_id))


def _ordered_ids(conn: sqlite3.Connection, playlist_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT track_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position, rowid", (playlist_id,)
    ).fetchall()
    return [r["track_id"] for r in rows]


def _require_manual(row: sqlite3.Row) -> None:
    if row["kind"] != "manual":
        raise HTTPException(status_code=409, detail="a smart playlist fills itself from its rules")


@router.post("", status_code=201)
def create_playlist(body: PlaylistCreate, ctx: AppContext = Depends(get_ctx)) -> dict:
    if body.kind == "smart":
        build_rule_sql(body.rules)  # reject bad rules now, not when the playlist is opened
    with ctx.db() as conn:
        now = _now()
        cursor = conn.execute(
            "INSERT INTO playlists (name, source, kind, rules_json, created_at, updated_at) "
            "VALUES (?, 'manual', ?, ?, ?, ?)",
            (body.name.strip(), body.kind, _rules_json(body.rules) if body.kind == "smart" else None, now, now),
        )
        playlist_id = cursor.lastrowid
        if body.kind == "manual" and body.track_ids:
            _write_order(conn, playlist_id, body.track_ids)
        conn.commit()
        return _summary(conn, _playlist_row(conn, playlist_id))


def _rules_json(rules: dict[str, Any] | None) -> str | None:
    import json

    return json.dumps(rules) if rules else None


@router.get("/{playlist_id}")
def get_playlist(playlist_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        return _summary(conn, _playlist_row(conn, playlist_id))


class PlaylistPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    rules: dict[str, Any] | None = None


@router.patch("/{playlist_id}")
def patch_playlist(playlist_id: int, body: PlaylistPatch, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        row = _playlist_row(conn, playlist_id)
        if body.name is not None:
            conn.execute("UPDATE playlists SET name = ?, updated_at = ? WHERE id = ?", (body.name.strip(), _now(), playlist_id))
        if body.rules is not None:
            if row["kind"] != "smart":
                raise HTTPException(status_code=409, detail="only smart playlists have rules")
            build_rule_sql(body.rules)
            conn.execute(
                "UPDATE playlists SET rules_json = ?, updated_at = ? WHERE id = ?",
                (_rules_json(body.rules), _now(), playlist_id),
            )
        conn.commit()
        return _summary(conn, _playlist_row(conn, playlist_id))


@router.delete("/{playlist_id}")
def delete_playlist(playlist_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        _playlist_row(conn, playlist_id)
        conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
        conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        conn.commit()
    return {"ok": True}


class AddTracks(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=MAX_TRACKS)
    position: int | None = None  # insert index; None appends


@router.post("/{playlist_id}/tracks")
def add_tracks(playlist_id: int, body: AddTracks, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        _require_manual(_playlist_row(conn, playlist_id))
        placeholders = ", ".join("?" * len(set(body.ids)))
        known = {
            r["id"] for r in conn.execute(f"SELECT id FROM tracks WHERE id IN ({placeholders})", list(set(body.ids)))
        }
        incoming = [i for i in body.ids if i in known]
        current = _ordered_ids(conn, playlist_id)
        index = len(current) if body.position is None else max(0, min(body.position, len(current)))
        _write_order(conn, playlist_id, current[:index] + incoming + current[index:])
        conn.commit()
    return {"added": len(incoming), "skipped": len(body.ids) - len(incoming)}


class RemoveTracks(BaseModel):
    positions: list[int] = Field(min_length=1)


@router.post("/{playlist_id}/tracks/remove")
def remove_tracks(playlist_id: int, body: RemoveTracks, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Remove by position, since the same track may appear in a playlist more than once."""
    with ctx.db() as conn:
        _require_manual(_playlist_row(conn, playlist_id))
        current = _ordered_ids(conn, playlist_id)
        drop = set(body.positions)
        kept = [track_id for position, track_id in enumerate(current) if position not in drop]
        _write_order(conn, playlist_id, kept)
        conn.commit()
    return {"removed": len(current) - len(kept)}


class MoveTrack(BaseModel):
    from_position: int = Field(ge=0)
    to_position: int = Field(ge=0)


@router.post("/{playlist_id}/tracks/move")
def move_track(playlist_id: int, body: MoveTrack, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        _require_manual(_playlist_row(conn, playlist_id))
        current = _ordered_ids(conn, playlist_id)
        if body.from_position >= len(current):
            raise HTTPException(status_code=422, detail="position out of range")
        moved = current.pop(body.from_position)
        current.insert(min(body.to_position, len(current)), moved)
        _write_order(conn, playlist_id, current)
        conn.commit()
    return {"ok": True}


@router.get("/{playlist_id}/export.m3u8")
def export_m3u8(playlist_id: int, ctx: AppContext = Depends(get_ctx)) -> Response:
    with ctx.db() as conn:
        row = _playlist_row(conn, playlist_id)
        sel = build_selection(conn, playlist_id=playlist_id, sort="position")
        tracks = conn.execute(
            f"SELECT t.file_path, t.title, t.artist, t.duration_seconds {sel.from_sql} ORDER BY {sel.order}",
            sel.params,
        ).fetchall()
    lines = ["#EXTM3U"]
    for t in tracks:
        label = f"{t['artist']} - {t['title']}" if t["artist"] and t["title"] else Path(t["file_path"]).stem
        lines.append(f"#EXTINF:{int(t['duration_seconds'] or 0)},{label}")
        lines.append(t["file_path"])
    safe_name = re.sub(r'[\\/:*?"<>|]+', "_", row["name"]).strip() or "playlist"
    return Response(
        "\n".join(lines) + "\n",
        media_type="audio/x-mpegurl; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.m3u8"'},
    )


class ImportBody(BaseModel):
    path: str
    name: str | None = None


@router.post("/import", status_code=201)
def import_m3u(body: ImportBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    path = Path(body.path)
    if not path.is_file() or path.suffix.lower() not in (".m3u", ".m3u8"):
        raise HTTPException(status_code=422, detail="pick an .m3u or .m3u8 playlist file")
    entries = len(playlist_import.parse_m3u(path))
    with ctx.db() as conn:
        matched = playlist_import.import_playlist(conn, path, body.name)
        playlist_id = conn.execute("SELECT MAX(id) AS id FROM playlists").fetchone()["id"]
        conn.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (_now(), playlist_id))
        conn.commit()
        summary = _summary(conn, _playlist_row(conn, playlist_id))
    return {"playlist": summary, "entries": entries, "matched": matched, "unmatched": entries - matched}
