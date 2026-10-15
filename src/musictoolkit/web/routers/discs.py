"""CDs and DVDs: the drives, the disc in one, finding out which album it is, ripping it into the library (preview first), the history of rips,
and taking a rip back.

Nothing is written until a preview of the same request has been made, and the request names a drive only by its letter: the letter must be
one the helper itself lists, so the page can never make anything open a device of its choosing."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from musictoolkit.discs import coverart, encode, lookup, rip, store
from musictoolkit.discs import helper as disc_helper
from musictoolkit.discs.helper import LISTING_TIMEOUT, DiscError, DiscHelper, DiscUnavailable, valid_drive
from musictoolkit.discs.toc import BadToc, Toc
from musictoolkit.integrations import musicbrainz_client
from musictoolkit.web.context import AppContext, get_ctx
from musictoolkit.web.jobs import JobHandle
from musictoolkit.web.routers.organize import PRESETS

router = APIRouter(prefix="/api/discs")

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class TrackBody(BaseModel):
    number: int = Field(ge=1, le=99)
    title: str = Field("", max_length=300)
    artist: str = Field("", max_length=300)
    recording_id: str | None = Field(None, max_length=40)
    rip: bool = True


class RipBody(BaseModel):
    drive: str
    root: str
    scheme: str = Field(min_length=1, max_length=500)
    format: str
    quality: str
    accuracy: str
    album: str = Field("", max_length=300)
    album_artist: str = Field("", max_length=300)
    year: int | None = Field(None, ge=1, le=9999)
    date: str = Field("", max_length=10)
    genre: str = Field("", max_length=100)
    disc_number: int = Field(1, ge=1, le=99)
    disc_total: int = Field(1, ge=1, le=99)
    release_id: str | None = Field(None, max_length=40)
    release_group_id: str | None = Field(None, max_length=40)
    artist_ids: list[str] = Field(default_factory=list, max_length=20)
    barcode: str = Field("", max_length=20)
    label: str = Field("", max_length=300)
    tracks: list[TrackBody] = Field(max_length=99)
    cover: bool = True
    eject: bool = False


class DriveBody(BaseModel):
    drive: str


def _cache_dir(ctx: AppContext) -> Path:
    return ctx.data_dir / "cache" / "discs"


def _problem(error: Exception) -> HTTPException:
    """A refusal from the drive or the helper, as an answer the person can act on."""
    code = error.code if isinstance(error, DiscError) else None
    return HTTPException(status_code=404 if code == "not_found" else 409, detail=str(error))


def _toc_of(drive: str) -> Toc:
    """The table of contents of the disc in this drive. A helper is started for the moment. The drive must be one the helper lists itself."""
    drive = valid_drive(drive)
    with DiscHelper() as helper:
        listed = {str(d.get("id", "")).upper() for d in helper.call("drives", timeout=LISTING_TIMEOUT)["drives"]}
        if drive not in listed:
            raise DiscError("not_found")
        return Toc.from_reply(helper.call("toc", drive=drive))


def _read_toc(ctx: AppContext, drive: str) -> Toc:
    if ctx.jobs.is_busy("rip"):
        raise HTTPException(status_code=409, detail="A disc is being ripped right now. Wait for it to finish, or stop it.")
    try:
        toc = _toc_of(drive)
    except (DiscError, DiscUnavailable, BadToc) as error:
        raise _problem(error) from None
    if not toc.audio_tracks():
        raise HTTPException(status_code=409, detail="This disc has no music on it (it is a data disc).")
    return toc


def _mb_id(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not _UUID.match(value):
        raise HTTPException(status_code=422, detail="That is not a MusicBrainz id.")
    return value


def _request(body: RipBody) -> rip.RipRequest:
    try:
        drive = valid_drive(body.drive)
    except DiscError as error:
        raise _problem(error) from None
    return rip.RipRequest(
        drive=drive, root=body.root, scheme=body.scheme.strip(), format=body.format, quality=body.quality, accuracy=body.accuracy,
        album=body.album.strip(), album_artist=body.album_artist.strip(), year=body.year, date=body.date.strip(), genre=body.genre.strip(),
        disc_number=body.disc_number, disc_total=max(body.disc_total, body.disc_number), release_id=_mb_id(body.release_id),
        release_group_id=_mb_id(body.release_group_id), artist_ids=[i for i in (_mb_id(a) for a in body.artist_ids) if i], barcode=body.barcode.strip(),
        label=body.label.strip(), cover=body.cover, eject=body.eject,
        tracks=[rip.TrackInput(t.number, t.title, t.artist.strip(), _mb_id(t.recording_id), t.rip) for t in body.tracks],
    )


def _key(body: RipBody) -> str:
    return json.dumps(body.model_dump(), sort_keys=True)


def _plan(ctx: AppContext, toc: Toc, request: rip.RipRequest) -> rip.RipPlan:
    try:
        return rip.plan_rip(toc, request, list(ctx.config.library.roots), _cache_dir(ctx))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


# ---------------------------------------------------------------------------------------- what is there, and what the defaults are


@router.get("/options")
def options(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    cfg = ctx.config
    roots = list(cfg.library.roots)
    root = cfg.disc.rip_root if cfg.disc.rip_root in roots else (roots[0] if roots else "")
    fmt = cfg.disc.rip_format
    return {
        "contact_set": bool(cfg.musicbrainz.contact.strip()),
        "roots": [{"path": r, "available": Path(r).is_dir()} for r in roots],
        "scheme": cfg.disc.rip_scheme or cfg.library.canonical_scheme,
        "presets": PRESETS,
        "qualities": {name: list(values) for name, values in encode.QUALITIES.items()},
        "defaults": {
            "format": fmt, "quality": cfg.disc.rip_mp3_quality if fmt == "mp3" else encode.DEFAULT_QUALITY["flac"], "mp3_quality": cfg.disc.rip_mp3_quality,
            "accuracy": cfg.disc.rip_accuracy, "root": root, "cover": cfg.disc.rip_cover, "eject": cfg.disc.eject_after_rip,
        },
    }


@router.get("/drives")
def drives(fresh: bool = False, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """The CD and DVD drives and what is in them: {available, reason, error, drives, busy}. Never an error: `reason` says why there are none."""
    return {**disc_helper.list_drives(fresh=fresh), "busy": ctx.jobs.is_busy("rip")}


@router.get("/drives/{drive}/disc")
def disc(drive: str, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """The disc in a drive: its tracks and disc ID. Asks the drive only; nothing goes to the internet."""
    toc = _read_toc(ctx, drive)
    return {
        "drive": valid_drive(drive), "disc_id": toc.disc_id(), "attach_url": toc.attach_url(),
        "tracks": [{"number": t.number, "seconds": round(toc.seconds_of(t), 1), "start": t.start} for t in toc.audio_tracks()],
        "manual": lookup.manual_candidate(toc).to_dict(),
    }


@router.post("/lookup")
def find_album(body: DriveBody, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Ask MusicBrainz which album the disc is. A job, because MusicBrainz can be slow (and it only answers one question a second)."""
    cfg = ctx.config.musicbrainz
    if not cfg.contact.strip():
        raise HTTPException(
            status_code=409,
            detail="MusicBrainz asks every app to send a contact email with its requests. Add yours in Settings, Accounts, or type the album in by hand.",
        )
    drive = valid_drive_or_404(body.drive)
    if ctx.jobs.is_busy("rip"):
        raise HTTPException(status_code=409, detail="A disc is being ripped right now.")

    def run(handle: JobHandle) -> dict[str, Any]:
        handle.update(message="Reading the disc")
        toc = _toc_of(drive)
        musicbrainz_client.configure(cfg.app_name, cfg.app_version, cfg.contact.strip())
        handle.update(message="Asking MusicBrainz")
        found = lookup.find_releases(toc)
        return {"disc_id": toc.disc_id(), "attach_url": toc.attach_url(), "candidates": [c.to_dict() for c in found]}

    return {"job": ctx.jobs.submit("disc-lookup", "Looking the disc up on MusicBrainz", run, lane="network").to_dict()}


def valid_drive_or_404(raw: str) -> str:
    try:
        return valid_drive(raw)
    except DiscError as error:
        raise _problem(error) from None


# ---------------------------------------------------------------------------------------- ripping


@router.post("/rip/preview")
def preview(body: RipBody, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Where every song would go, and whether the rip can start. Touches nothing."""
    request = _request(body)
    toc = _read_toc(ctx, request.drive)
    plan = _plan(ctx, toc, request)
    ctx.previews["rip"] = {"key": _key(body), "toc": toc}
    return plan.to_dict()


@router.post("/rip/start")
def start(body: RipBody, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    held = ctx.previews.get("rip")
    if not held or held["key"] != _key(body):
        raise HTTPException(status_code=409, detail="Look at where the songs will go first. Nothing is written until you have seen it.")
    if ctx.jobs.is_busy("rip"):
        raise HTTPException(status_code=409, detail="A disc is being ripped already.")
    if ctx.previews.pop("rip", None) is not held:  # what was shown is used up; if another request took it in the same instant, this one stops
        raise HTTPException(status_code=409, detail="A disc is being ripped already.")
    request = _request(body)
    toc: Toc = held["toc"]
    plan = _plan(ctx, toc, request)  # plan again: files may have appeared since the preview
    if plan.problems:
        raise HTTPException(status_code=409, detail=plan.problems[0])
    batch_id = rip.new_batch_id()
    with ctx.db() as conn:
        rip_id = rip.begin(conn, plan, request, toc, batch_id)
    track_total = len(toc.audio_tracks())

    def run(handle: JobHandle) -> dict[str, Any]:
        cover = None
        if request.cover and request.release_id:
            handle.update(message="Getting the cover")
            cover = coverart.front_cover(request.release_id, request.release_group_id)
        return rip.run_rip(
            db=ctx.db, plan=plan, request=request, toc=toc, batch_id=batch_id, rip_id=rip_id, cache_dir=_cache_dir(ctx), cover=cover,
            handle=handle, track_total=track_total,
        )

    title = f"Ripping {request.album or 'the disc'}"
    return {"job": ctx.jobs.submit("rip", title, run, lane="disc").to_dict(), "rip_id": rip_id}


@router.get("/rips")
def rips(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db() as conn:
        return {"rips": store.list_rips(conn)}


@router.get("/rips/{rip_id}")
def rip_detail(rip_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db() as conn:
        found = store.get_rip(conn, rip_id)
    if found is None:
        raise HTTPException(status_code=404, detail="There is no such rip.")
    return found


@router.post("/rips/{rip_id}/undo")
def undo(rip_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Send the songs of a rip to the Recycle Bin and forget them, those that are still exactly as they were written."""
    with ctx.db() as conn:
        found = store.get_rip(conn, rip_id)
    if found is None:
        raise HTTPException(status_code=404, detail="There is no such rip.")
    if found["status"] == "running":
        raise HTTPException(status_code=409, detail="That rip is still going.")
    if found["undone_at"]:
        raise HTTPException(status_code=409, detail="That rip has already been taken back.")
    if ctx.jobs.is_busy("rip-undo"):
        raise HTTPException(status_code=409, detail="A rip is being taken back already.")
    songs = sum(1 for t in found["tracks"] if t["status"] in ("done", "problem"))

    def run(handle: JobHandle) -> dict[str, Any]:
        def progress(done: int, total: int) -> None:
            handle.update(done=done, total=total, message=f"Put back {done} of {total}")
            handle.check()

        roots = [Path(r) for r in ctx.config.library.roots]
        with ctx.db() as conn:
            return rip.undo_rip(conn, rip_id, roots, progress)

    return {"job": ctx.jobs.submit("rip-undo", f"Taking back {found['album']}", run).to_dict(), "songs": songs}


@router.post("/eject")
def eject(body: DriveBody, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    drive = valid_drive_or_404(body.drive)
    if ctx.jobs.is_busy("rip"):
        raise HTTPException(status_code=409, detail="A disc is being ripped right now.")
    try:
        with DiscHelper() as helper:
            if drive not in {str(d.get("id", "")).upper() for d in helper.call("drives", timeout=LISTING_TIMEOUT)["drives"]}:
                raise DiscError("not_found")
            helper.call("eject", drive=drive)
    except (DiscError, DiscUnavailable) as error:
        raise _problem(error) from None
    disc_helper.forget_listing()
    return {"ok": True}
