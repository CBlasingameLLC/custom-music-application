"""Internet radio: the stations you keep, the directory to find more, and the stream itself (played through this app)."""

from __future__ import annotations

import re
import threading
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from musictoolkit.net import artwork, guard
from musictoolkit.net.proxy import StreamError, relay
from musictoolkit.radio import directory, store, streams
from musictoolkit.radio.icy import IcyDemuxer, metaint_from
from musictoolkit.web.context import AppContext, get_ctx

router = APIRouter(prefix="/api/radio")

CODECS = {"audio/mpeg": "MP3", "audio/mp3": "MP3", "audio/aac": "AAC", "audio/aacp": "AAC+", "audio/ogg": "OGG", "application/ogg": "OGG", "audio/flac": "FLAC", "audio/x-flac": "FLAC"}


def _link(value: str | None) -> str | None:
    try:
        return guard.check_url(value) if value else None
    except guard.UnsafeURL:
        return None


def _station_row(ctx: AppContext, station_id: int):
    with ctx.db() as conn:
        row = store.station_row(conn, station_id)
    if row is None:
        raise HTTPException(status_code=404, detail="station not found")
    return row


# ---------------------------------------------------------------- kept stations

@router.get("/stations")
def list_stations(scope: Literal["all", "favorites", "recent"] = "all", ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        items = store.list_stations(conn, scope)
        counts = {
            "all": conn.execute("SELECT COUNT(*) FROM radio_stations").fetchone()[0],
            "favorites": conn.execute("SELECT COUNT(*) FROM radio_stations WHERE favorite = 1").fetchone()[0],
            "recent": conn.execute("SELECT COUNT(*) FROM radio_stations WHERE last_played_at IS NOT NULL").fetchone()[0],
        }
    return {"items": items, "counts": counts}


class AddByAddress(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    name: str | None = Field(default=None, max_length=200)


@router.post("/stations", status_code=201)
def add_by_address(body: AddByAddress, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Keep a station from its address (a stream, or the playlist file a station publishes). It is tried first: an address that does not
    play is not kept, and what the stream says about itself (name, kind of audio, bit rate, genre) fills in what was not typed."""
    try:
        url = guard.check_url(body.url)
    except guard.UnsafeURL as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        upstream, content_type = streams.open_station(url)
    except StreamError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    headers = {k.lower(): v for k, v in upstream.headers.items()}
    upstream.close()
    kind = content_type.split(";")[0].strip().lower()
    bitrate = headers.get("icy-br", "").split(",")[0].strip()
    name = (body.name or "").strip() or headers.get("icy-name", "").strip() or urlsplit(url).hostname or url
    genre = re.split(r"[,;/]| {2,}", headers.get("icy-genre", ""))
    with ctx.db() as conn:
        return store.add_station(
            conn, name=name, stream_url=url, homepage=_link(headers.get("icy-url")), tags=genre, codec=CODECS.get(kind), bitrate=int(bitrate) if bitrate.isdigit() else None,
            favorite=True,
        )


class SavedStation(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    stream_url: str = Field(min_length=1, max_length=2048)
    source: Literal["radio-browser", "manual"] = "radio-browser"
    source_id: str | None = Field(default=None, max_length=64)
    homepage: str | None = Field(default=None, max_length=2048)
    art_url: str | None = Field(default=None, max_length=2048)
    tags: list[str] = Field(default_factory=list, max_length=20)
    country: str | None = Field(default=None, max_length=80)
    country_code: str | None = Field(default=None, max_length=2)
    language: str | None = Field(default=None, max_length=80)
    codec: str | None = Field(default=None, max_length=20)
    bitrate: int | None = Field(default=None, ge=0, le=10000)
    favorite: bool = True


@router.post("/stations/save", status_code=201)
def save_station(body: SavedStation, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Keep a station found in the directory (marked as a favorite unless told otherwise)."""
    try:
        url = guard.check_url(body.stream_url)
    except guard.UnsafeURL as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with ctx.db() as conn:
        return store.add_station(
            conn, name=body.name, stream_url=url, source=body.source, source_id=body.source_id, homepage=_link(body.homepage), art_url=_link(body.art_url), tags=body.tags,
            country=body.country, country_code=body.country_code, language=body.language, codec=body.codec, bitrate=body.bitrate, favorite=body.favorite,
        )


class StationChange(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    favorite: bool | None = None


@router.patch("/stations/{station_id}")
def change_station(station_id: int, body: StationChange, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        station = store.update_station(conn, station_id, name=body.name, favorite=body.favorite)
    if station is None:
        raise HTTPException(status_code=404, detail="station not found")
    return station


@router.delete("/stations/{station_id}")
def delete_station(station_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        if not store.delete_station(conn, station_id):
            raise HTTPException(status_code=404, detail="station not found")
    ctx.radio.forget(station_id)
    return {"ok": True}


@router.post("/stations/{station_id}/played")
def played(station_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    """The person started this station: remember it among the recent ones, and let the directory count the listen (it ranks stations by that)."""
    row = _station_row(ctx, station_id)
    with ctx.db() as conn:
        store.record_play(conn, station_id)
    if row["source"] == "radio-browser":
        threading.Thread(target=directory.click, args=(row["source_id"],), daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------- listening

@router.get("/stations/{station_id}/stream")
def stream_station(station_id: int, ctx: AppContext = Depends(get_ctx)) -> StreamingResponse:
    """The station's audio, passed on. The title the station sends along with it is kept (see /now) and taken out of the audio."""
    row = _station_row(ctx, station_id)
    hub = ctx.radio
    if not hub.slots.take():
        raise HTTPException(status_code=429, detail="Too many streams are open at once")
    try:
        upstream, content_type = streams.open_station(row["stream_url"])
    except StreamError as exc:
        hub.slots.give_back()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except BaseException:
        hub.slots.give_back()
        raise
    interval = metaint_from(dict(upstream.headers))
    demuxer = IcyDemuxer(interval) if interval else None

    def transform(piece: bytes) -> bytes:
        audio = demuxer.feed(piece)  # type: ignore[union-attr]
        hub.heard(station_id, demuxer.title)  # type: ignore[union-attr]
        return audio

    def finished() -> None:
        hub.slots.give_back()
        hub.forget(station_id)

    kind = content_type.split(";")[0].strip().lower()
    return relay(
        upstream, media_type=kind if kind.startswith("audio/") or kind == "application/ogg" else "audio/mpeg", transform=transform if demuxer else None, done=finished,
    )


@router.get("/stations/{station_id}/now")
def now_playing(station_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    """What the station says is playing (if it says), for the player bar to show beside the station's name."""
    _station_row(ctx, station_id)
    return ctx.radio.now(station_id)


@router.post("/stations/{station_id}/check")
def check_station(station_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Try the station now and say whether it plays; the page asks after a failure, to tell the person why."""
    row = _station_row(ctx, station_id)
    try:
        upstream, content_type = streams.open_station(row["stream_url"])
    except StreamError as exc:
        return {"ok": False, "message": str(exc)}
    metadata = bool(metaint_from(dict(upstream.headers)))
    upstream.close()
    return {"ok": True, "type": content_type.split(";")[0].strip(), "metadata": metadata}


@router.get("/stations/{station_id}/art")
def station_art(station_id: int, ctx: AppContext = Depends(get_ctx)) -> Response:
    row = _station_row(ctx, station_id)
    data = artwork.cached_picture(row["art_url"], ctx.data_dir / "cache" / "radio")
    if data is None:
        raise HTTPException(status_code=404, detail="no picture")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------- the directory

def _marked(ctx: AppContext, found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The directory's stations, each saying whether it is already kept here (and under which id)."""
    with ctx.db() as conn:
        for station in found:
            row = conn.execute("SELECT id, favorite FROM radio_stations WHERE stream_url = ?", (station["stream_url"],)).fetchone()
            station["stored_id"] = row["id"] if row else None
            station["favorite"] = bool(row and row["favorite"])
    return found


@router.get("/directory")
def search_directory(
    q: str = "", tag: str = "", country: str = "", language: str = "", order: Literal["popular", "votes", "name", "bitrate"] = "popular", offset: int = 0,
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    try:
        found = directory.search(name=q, tag=tag, country_code=country, language=language, order=order, offset=max(0, offset))
    except directory.DirectoryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"items": _marked(ctx, found["items"]), "more": found["more"]}


@router.get("/directory/tags")
def directory_tags() -> dict:
    try:
        return {"items": directory.tags()}
    except directory.DirectoryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/directory/countries")
def directory_countries() -> dict:
    try:
        return {"items": directory.countries()}
    except directory.DirectoryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
