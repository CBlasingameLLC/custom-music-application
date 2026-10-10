"""Devices: players, SD cards and drives to put music on. Choose what goes on one, preview exactly what a sync
would do, then run it. The preview is stored, and a sync only runs from a preview the person has seen."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from musictoolkit.sync import device_detect, mirror, mtp, selection
from musictoolkit.sync.targets import LocalFolder, Target, TargetError
from musictoolkit.web.context import AppContext, get_ctx, same_path
from musictoolkit.web.jobs import JobHandle

router = APIRouter(prefix="/api/devices")

PAGE = 100


class DeviceBody(BaseModel):
    path: str
    label: str | None = Field(default=None, max_length=80)


class MtpBody(BaseModel):
    serial: str = Field(max_length=200)
    storage: str = Field(max_length=200)
    folder: str | None = Field(default=None, max_length=200)  # where the music goes on that storage; "Music" if empty
    label: str | None = Field(default=None, max_length=80)


class DevicePatch(BaseModel):
    label: str | None = Field(default=None, max_length=80)
    path: str | None = None  # the device is somewhere else now (its drive letter changed)


class PrefsBody(BaseModel):
    sources: list[dict[str, Any]] = Field(default_factory=list, max_length=selection.MAX_SOURCES)
    scheme: str | None = Field(default=None, max_length=500)
    playlists: bool = True  # write the chosen playlists to the device as .m3u8 files
    covers: bool = True  # take folder pictures (cover.jpg) along


class SyncBody(BaseModel):
    job_id: str
    prune: bool = False
    confirm_prune: int | None = None  # must equal the number of removals the preview listed


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def _row(conn, device_id: int):
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="That device is not in your list")
    return row


def _prefs(row) -> dict[str, Any]:
    try:
        stored = json.loads(row["prefs_json"]) if row["prefs_json"] else {}
    except ValueError:
        stored = {}
    return {
        "sources": stored.get("sources", []), "scheme": stored.get("scheme"),
        "playlists": stored.get("playlists", True), "covers": stored.get("covers", True),
        "written_playlists": stored.get("written_playlists", []),
    }


def _save_prefs(conn, device_id: int, prefs: dict[str, Any]) -> None:
    conn.execute("UPDATE devices SET prefs_json = ? WHERE id = ?", (json.dumps(prefs), device_id))
    conn.commit()


def _library_overlap(ctx: AppContext, path: Path) -> str | None:
    resolved = path.resolve()
    for root in ctx.config.library.roots:
        library = Path(root).resolve()
        if resolved == library or resolved.is_relative_to(library) or library.is_relative_to(resolved):
            return root
    return None


def _usable_folder(ctx: AppContext, raw: str) -> Path:
    path = Path(raw.strip().strip('"')).expanduser()
    if not path.is_dir():
        raise HTTPException(status_code=422, detail=f"That folder is not available: {raw}. Is the device connected?")
    if _library_overlap(ctx, path):
        raise HTTPException(
            status_code=422,
            detail="That folder is part of your music library (or holds it). Choose the player or card to copy music to instead.",
        )
    return path.resolve()


def _label_for(path: Path, volume_label: str | None, given: str | None) -> str:
    """What to call a new device: the person's own name; else, for a whole drive, the drive's name ("WALKMAN");
    else the folder's name (a folder on a drive is better told apart by its own name than by the drive's)."""
    whole_drive = path == Path(path.anchor)
    return (given or "").strip() or (volume_label if whole_drive and volume_label else "") or path.name or str(path)


def _synced(conn, device_id: int):
    return conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes FROM sync_manifest WHERE device_id = ? AND status = 'synced'", (device_id,)
    ).fetchone()


def _mtp_trouble(listing: dict[str, Any], device: dict[str, Any] | None) -> str:
    """Why a phone or player cannot be used right now, in words for the person."""
    if not listing["available"]:
        return listing["reason"] or "Phones and players without a drive letter are not available here."
    if listing["error"]:
        return f"Could not look for phones and players: {listing['error']}"
    return mtp.NOT_CONNECTED if device is None else mtp.NO_STORAGE


def _describe_mtp(conn, row) -> dict[str, Any]:
    listing = mtp.list_devices()
    device, storage = mtp.find(listing["devices"], row["mtp_serial"], row["mtp_storage"], row["mtp_storage_name"])
    synced = _synced(conn, row["id"])
    prefs = _prefs(row)
    folder = row["mtp_base"] or mtp.DEFAULT_BASE
    return {
        "id": row["id"], "kind": "mtp", "label": row["label"], "path": f"{row['mtp_storage_name'] or 'Storage'} / {folder}",
        "volume_label": row["mtp_model"], "connected": storage is not None, "different_drive": False, "moved_to": None,
        "note": None if storage is not None else _mtp_trouble(listing, device),
        "free": int(storage["free"]) if storage and storage.get("free") is not None else None,
        "total": int(storage["capacity"]) if storage and storage.get("capacity") else None,
        "fs": None, "removable": True, "synced": synced["n"], "synced_bytes": synced["bytes"],
        "last_synced_at": row["last_synced_at"], "created_at": row["created_at"],
        "mtp": {"model": row["mtp_model"], "storage": row["mtp_storage_name"], "folder": folder, "serial": row["mtp_serial"]},
        "prefs": {k: prefs[k] for k in ("sources", "scheme", "playlists", "covers")},
    }


def _describe(conn, row) -> dict[str, Any]:
    if row["kind"] == "mtp":
        return _describe_mtp(conn, row)
    path = row["last_seen_mount_path"]
    exists = Path(path).is_dir()
    info = device_detect.volume_info(path) if exists else device_detect.VolumeInfo()
    different = bool(exists and row["volume_serial"] and info.serial and row["volume_serial"] != info.serial)
    moved_to = None
    if (not exists or different) and row["volume_serial"]:
        for candidate in device_detect.list_candidate_devices():
            if device_detect.volume_info(candidate.mountpoint).serial == row["volume_serial"]:
                moved_to = candidate.mountpoint
                break
    free = total = None
    removable = None
    fs = row["fs"]
    if exists:
        try:
            usage = shutil.disk_usage(path)
            free, total = usage.free, usage.total
        except OSError:
            pass
        volume = device_detect.volume_of(path)
        if volume:
            removable, fs = volume.likely_removable, fs or volume.fstype
    synced = _synced(conn, row["id"])
    prefs = _prefs(row)
    return {
        "id": row["id"], "kind": "folder", "label": row["label"], "path": path, "volume_label": row["volume_label"],
        "connected": exists and not different, "different_drive": different, "moved_to": moved_to,
        "free": free, "total": total, "fs": fs, "removable": removable,
        "synced": synced["n"], "synced_bytes": synced["bytes"], "last_synced_at": row["last_synced_at"],
        "created_at": row["created_at"],
        "prefs": {k: prefs[k] for k in ("sources", "scheme", "playlists", "covers")},
    }


def _out(ctx: AppContext, conn, row) -> dict[str, Any]:
    """A device as the screens want it: what it is now, plus the layout a device gets unless told otherwise."""
    return {**_describe(conn, row), "default_scheme": ctx.config.sync.device_scheme}


def _music_folder(mount: str) -> str | None:
    """The player's own Music folder at the top of a drive, if it has one. Walkmans and phones list only what is in there."""
    try:
        with os.scandir(mount) as entries:
            for entry in entries:
                if entry.name.lower() == "music" and entry.is_dir():
                    return entry.path
    except OSError:
        return None
    return None


@router.get("")
def list_devices(ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        rows = conn.execute("SELECT * FROM devices ORDER BY created_at DESC, id DESC").fetchall()
        return {"items": [_out(ctx, conn, r) for r in rows], "default_scheme": ctx.config.sync.device_scheme}


@router.get("/volumes")
def volumes(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Every mounted volume; the removable-looking ones are flagged, never auto-selected."""
    with ctx.db() as conn:
        known = {r["last_seen_mount_path"]: r["id"] for r in conn.execute("SELECT id, last_seen_mount_path FROM devices")}
    items = []
    for c in device_detect.list_candidate_devices():
        info = device_detect.volume_info(c.mountpoint)
        items.append({
            "mount_path": c.mountpoint, "device": c.device, "fs": c.fstype, "total": c.total_bytes, "free": c.free_bytes,
            "removable": c.likely_removable, "label": info.label, "serial": info.serial, "music_folder": _music_folder(c.mountpoint),
            "device_id": next((i for p, i in known.items() if same_path(p, c.mountpoint)), None),
            "holds_library": _library_overlap(ctx, Path(c.mountpoint)) is not None,
        })
    return {"items": items}


def _folder_on_device(raw: str | None) -> str:
    """Where the music goes on a phone's storage: "Music" unless the person says otherwise."""
    parts = [part.strip() for part in (raw or "").replace("\\", "/").split("/") if part.strip()]
    if not parts:
        return mtp.DEFAULT_BASE
    if any(part in (".", "..") or re.search(r'[<>:"|?*\x00-\x1f]', part) for part in parts):
        raise HTTPException(status_code=422, detail="That folder name has characters a phone cannot use")
    return "/".join(parts)


@router.get("/mtp")
def mtp_devices(fresh: bool = False, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Phones and players without a drive letter that are plugged in now, with their storages; which are already added."""
    listing = mtp.list_devices(fresh=fresh)
    with ctx.db() as conn:
        known = [dict(r) for r in conn.execute("SELECT id, mtp_serial, mtp_storage, mtp_storage_name FROM devices WHERE kind = 'mtp'")]
    devices = []
    for device in listing["devices"]:
        mine = [k for k in known if k["mtp_serial"] in (device.get("serial"), device.get("id"))]
        storages = []
        for storage in device.get("storages", []):
            added = next((k["id"] for k in mine if storage["id"] == k["mtp_storage"] or storage.get("name") == k["mtp_storage_name"]), None)
            storages.append({**storage, "device_id": added})
        devices.append({**device, "storages": storages})
    return {**listing, "devices": devices}


@router.post("/mtp", status_code=201)
def add_mtp_device(body: MtpBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    folder = _folder_on_device(body.folder)
    listing = mtp.list_devices(fresh=True)
    device, storage = mtp.find(listing["devices"], body.serial, body.storage)
    if storage is None:
        raise HTTPException(status_code=422, detail=_mtp_trouble(listing, device))
    serial = device.get("serial") or device["id"]
    model = " ".join(part for part in (device.get("manufacturer"), device.get("model")) if part) or device.get("name")
    with ctx.db() as conn:
        existing = conn.execute(
            "SELECT id FROM devices WHERE kind = 'mtp' AND mtp_serial = ? AND (mtp_storage = ? OR mtp_storage_name = ?)",
            (serial, storage["id"], storage["name"]),
        ).fetchone()
        if existing is not None:  # the same phone again: keep its folder (what was copied is recorded relative to it)
            conn.execute(
                "UPDATE devices SET mtp_storage = ?, mtp_storage_name = ?, mtp_model = ? WHERE id = ?",
                (storage["id"], storage["name"], model, existing["id"]),
            )
            conn.commit()
            return _out(ctx, conn, _row(conn, existing["id"]))
        label = (body.label or "").strip() or device.get("name") or model or "Phone"
        device_id = mirror.get_or_create_device(conn, f"mtp:{serial}:{storage['id']}", label)
        conn.execute(
            "UPDATE devices SET kind = 'mtp', mtp_serial = ?, mtp_storage = ?, mtp_storage_name = ?, mtp_base = ?, mtp_model = ? WHERE id = ?",
            (serial, storage["id"], storage["name"], folder, model, device_id),
        )
        conn.commit()
        return _out(ctx, conn, _row(conn, device_id))


@router.post("", status_code=201)
def add_device(body: DeviceBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    path = _usable_folder(ctx, body.path)
    info = device_detect.volume_info(str(path))
    volume = device_detect.volume_of(str(path))
    with ctx.db() as conn:
        existing = conn.execute("SELECT id FROM devices WHERE last_seen_mount_path = ?", (str(path),)).fetchone()
        if existing is None and info.serial:
            existing = conn.execute("SELECT id FROM devices WHERE volume_serial = ?", (info.serial,)).fetchone()
        if existing is not None:
            conn.execute("UPDATE devices SET last_seen_mount_path = ? WHERE id = ?", (str(path), existing["id"]))
            conn.commit()
            return _out(ctx, conn, _row(conn, existing["id"]))
        label = _label_for(path, info.label, body.label)
        device_id = mirror.get_or_create_device(conn, str(path), label)
        conn.execute(
            "UPDATE devices SET volume_serial = ?, volume_label = ?, fs = ? WHERE id = ?",
            (info.serial, info.label, info.fs or (volume.fstype if volume else None), device_id),
        )
        conn.commit()
        return _out(ctx, conn, _row(conn, device_id))


@router.get("/{device_id}")
def get_device(device_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        return _out(ctx, conn, _row(conn, device_id))


@router.patch("/{device_id}")
def patch_device(device_id: int, body: DevicePatch, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        row = _row(conn, device_id)
        if body.label is not None:
            if not body.label.strip():
                raise HTTPException(status_code=422, detail="Give the device a name")
            conn.execute("UPDATE devices SET label = ? WHERE id = ?", (body.label.strip(), device_id))
        if body.path is not None:
            if row["kind"] == "mtp":
                raise HTTPException(status_code=422, detail="A phone or player without a drive letter is found by itself; there is no folder to point it at")
            path = _usable_folder(ctx, body.path)
            clash = conn.execute("SELECT id FROM devices WHERE last_seen_mount_path = ? AND id != ?", (str(path), device_id)).fetchone()
            if clash:
                raise HTTPException(status_code=409, detail="Another device already uses that folder")
            info = device_detect.volume_info(str(path))
            if row["volume_serial"] and info.serial and row["volume_serial"] != info.serial:
                raise HTTPException(status_code=409, detail="That is a different drive from the one this device was. Add it as a new device instead.")
            conn.execute("UPDATE devices SET last_seen_mount_path = ?, volume_serial = COALESCE(volume_serial, ?) WHERE id = ?", (str(path), info.serial, device_id))
        conn.commit()
        return _out(ctx, conn, _row(conn, device_id))


@router.delete("/{device_id}")
def forget_device(device_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Stop tracking a device. Nothing on the device itself is touched."""
    with ctx.db() as conn:
        _row(conn, device_id)
        conn.execute("DELETE FROM sync_manifest WHERE device_id = ?", (device_id,))
        conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        conn.commit()
    ctx.previews.pop(f"sync:{device_id}", None)
    return {"ok": True}


@router.put("/{device_id}/prefs")
def put_prefs(device_id: int, body: PrefsBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        row = _row(conn, device_id)
        _checked_sources(body.sources, allow_empty=True)
        prefs = _prefs(row)
        prefs.update(sources=body.sources, scheme=body.scheme, playlists=body.playlists, covers=body.covers)
        _save_prefs(conn, device_id, prefs)
        return _out(ctx, conn, _row(conn, device_id))


def _checked_sources(sources: list[dict[str, Any]], allow_empty: bool = False) -> None:
    if not sources and allow_empty:
        return
    try:
        selection.validate_sources(sources)
    except selection.BadSource as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@dataclass
class Where:
    """Where a device's files go right now, worked out when a preview or a sync is asked for."""

    key: str  # names the place: a preview only serves a sync to the very same place
    root: Path | None  # a folder on a drive; None for a phone or player reached through the helper
    free: int  # free bytes as just seen
    total: int | None = None


@contextmanager
def _open_target(row, where: Where) -> Iterator[Target]:
    """The device as the sync engine uses it. For a phone this starts the helper and keeps it for the whole job."""
    if where.root is not None:
        yield LocalFolder(where.root, row["label"])
        return
    helper = mtp.MtpHelper()
    try:
        helper.start()
        target = mtp.MtpTarget.connect(
            helper, serial=row["mtp_serial"], storage=row["mtp_storage"], storage_name=row["mtp_storage_name"],
            base=row["mtp_base"] or mtp.DEFAULT_BASE, label=row["label"],
        )
    except (mtp.MtpUnavailable, TargetError) as exc:
        helper.close()
        raise RuntimeError(exc.strerror if isinstance(exc, TargetError) and exc.strerror else str(exc)) from None
    try:
        yield target
    finally:
        helper.close()
        mtp.forget_listing()  # what the phone has free is different now


def _connected_device(ctx: AppContext, conn, device_id: int) -> tuple[Any, Where]:
    row = _row(conn, device_id)
    if row["kind"] == "mtp":
        listing = mtp.list_devices()
        device, storage = mtp.find(listing["devices"], row["mtp_serial"], row["mtp_storage"], row["mtp_storage_name"])
        if storage is None:
            raise HTTPException(status_code=409, detail=_mtp_trouble(listing, device))
        key = f"mtp:{row['mtp_serial']}:{storage['id']}:{row['mtp_base']}"
        return row, Where(key, None, int(storage.get("free") or 0), int(storage["capacity"]) if storage.get("capacity") else None)
    described = _describe(conn, row)
    if described["different_drive"]:
        raise HTTPException(status_code=409, detail="A different drive is using that drive letter now. Check that the right device is plugged in.")
    if not described["connected"]:
        hint = f" It looks like it is on {described['moved_to']} now." if described["moved_to"] else ""
        raise HTTPException(status_code=409, detail=f"The device is not connected.{hint}")
    root = Path(row["last_seen_mount_path"]).resolve()
    if _library_overlap(ctx, root):
        raise HTTPException(status_code=409, detail="That folder is part of your music library. Pick the device folder again.")
    try:
        usage = shutil.disk_usage(root)
    except OSError:
        raise HTTPException(status_code=409, detail="The device is not connected.") from None
    return row, Where(str(root), root, usage.free, usage.total)


@router.post("/{device_id}/preview")
def preview(device_id: int, body: PrefsBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    _checked_sources(body.sources)
    scheme = (body.scheme or "").strip() or ctx.config.sync.device_scheme
    if body.scheme and body.scheme.strip():
        from musictoolkit.ingest import organizer

        try:
            organizer.validate_scheme(scheme)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    with ctx.db() as conn:
        row, where = _connected_device(ctx, conn, device_id)
        prefs = _prefs(row)
        prefs.update(sources=body.sources, scheme=body.scheme, playlists=body.playlists, covers=body.covers)
        _save_prefs(conn, device_id, prefs)
        fs = row["fs"]

    def run(handle: JobHandle) -> dict[str, Any]:
        def progress(done: int, total: int) -> None:
            handle.update(done=done, total=total, message=f"Compared {done:,} of {_plural(total, 'song')}")
            handle.check()

        with ctx.db() as conn:
            try:
                rows = selection.resolve_sources(conn, body.sources)
            except selection.BadSource as exc:
                raise RuntimeError(str(exc)) from None
            with _open_target(row, where) as target:
                free, total = target.free_space(), target.capacity()
                plan = mirror.plan_sync(conn, device_id, target, rows, scheme, free, fs=fs, on_progress=progress)
        ctx.previews[f"sync:{device_id}"] = {
            "job_id": handle.id, "plan": plan, "prefs": body.model_dump(), "root": where.key, "scheme": scheme,
        }
        reasons: dict[str, int] = {}
        for item in plan.to_copy:
            reasons[item.reason] = reasons.get(item.reason, 0) + 1
        skipped: dict[str, int] = {}
        for item in plan.skipped:
            skipped[item.reason] = skipped.get(item.reason, 0) + 1
        return {
            "selected": plan.selected, "to_copy": len(plan.to_copy), "bytes": plan.total_bytes_to_copy,
            "unchanged": plan.unchanged, "to_prune": len(plan.to_prune), "prune_bytes": plan.prune_bytes,
            "skipped": len(plan.skipped), "reasons": reasons, "skipped_reasons": skipped,
            "free": free, "total": total,
            "enough_space": mirror.has_sufficient_space(plan), "enough_space_with_removals": mirror.has_sufficient_space(plan, prune=True),
        }

    return {"job": ctx.jobs.submit("sync-preview", "Working out what to copy", run, lane="analyze").to_dict()}


@router.get("/{device_id}/preview/{job_id}")
def preview_page(
    device_id: int, job_id: str, kind: str = "copy", offset: int = 0, limit: int = PAGE, ctx: AppContext = Depends(get_ctx)
) -> dict:
    held = ctx.previews.get(f"sync:{device_id}")
    if not held or held["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="That preview is out of date. Preview again.")
    plan: mirror.SyncPlan = held["plan"]
    offset, limit = max(0, offset), max(1, min(limit, 500))
    if kind == "copy":
        chosen = plan.to_copy[offset:offset + limit]
        items = [
            {"track_id": i.row["id"], "title": i.row["title"] or Path(i.row["file_path"]).stem, "artist": i.row["artist"],
             "album": i.row["album"], "size": i.row["file_size"], "to": i.relative,
             "reason": i.reason}
            for i in chosen
        ]
        return {"kind": kind, "total": len(plan.to_copy), "items": items}
    if kind == "prune":
        wanted = plan.to_prune[offset:offset + limit]
        details = {}
        with ctx.db() as conn:
            for manifest_id, relative in wanted:
                found = conn.execute(
                    "SELECT t.title, t.artist, m.size FROM sync_manifest m LEFT JOIN tracks t ON t.id = m.track_id WHERE m.id = ?", (manifest_id,)
                ).fetchone()
                details[manifest_id] = found
        items = [
            {"path": relative.replace("\\", "/"), "title": (details[m]["title"] if details[m] else None), "artist": (details[m]["artist"] if details[m] else None),
             "size": (details[m]["size"] if details[m] else None)}
            for m, relative in wanted
        ]
        return {"kind": kind, "total": len(plan.to_prune), "items": items}
    if kind == "skipped":
        items = [
            {"track_id": s.track_id, "source": s.source, "reason": s.reason, "detail": s.detail} for s in plan.skipped[offset:offset + limit]
        ]
        return {"kind": kind, "total": len(plan.skipped), "items": items}
    raise HTTPException(status_code=422, detail="kind must be copy, prune or skipped")


@router.post("/{device_id}/sync")
def start_sync(device_id: int, body: SyncBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    held = ctx.previews.get(f"sync:{device_id}")
    if not held or held["job_id"] != body.job_id:
        raise HTTPException(status_code=409, detail="Preview what will be copied first. Nothing is written to the device until you have seen it.")
    plan: mirror.SyncPlan = held["plan"]
    prune = body.prune and bool(plan.to_prune)
    if prune and body.confirm_prune != len(plan.to_prune):
        raise HTTPException(status_code=422, detail=f"Removing songs from the device needs confirmation of how many: {len(plan.to_prune)}")
    if ctx.jobs.is_busy("sync"):
        raise HTTPException(status_code=409, detail="A sync is already running")
    with ctx.db() as conn:
        row, where = _connected_device(ctx, conn, device_id)
    if where.key != held["root"]:
        raise HTTPException(status_code=409, detail="The device moved since the preview. Preview again.")
    plan.free_bytes_on_device = where.free
    if not mirror.has_sufficient_space(plan, prune=prune):
        raise HTTPException(
            status_code=409,
            detail=f"Not enough room on the device: {_size(plan.needed_bytes(prune))} needed, {_size(where.free)} free.",
        )
    prefs = held["prefs"]

    def run(handle: JobHandle) -> dict[str, Any]:
        def progress(done: int, total: int, copied: int) -> None:
            handle.update(done=done, total=total, message=f"Copied {done:,} of {_plural(total, 'song')} ({_size(copied)})")
            handle.check()

        with ctx.db() as conn, _open_target(row, where) as target:
            plan.free_bytes_on_device = target.free_space()  # a phone's room was last seen a moment ago; look again now
            if not mirror.has_sufficient_space(plan, prune=prune):
                raise RuntimeError(f"Not enough room on the device: {_size(plan.needed_bytes(prune))} needed, {_size(plan.free_bytes_on_device)} free.")
            result = mirror.run_sync(conn, device_id, target, plan, prune=prune, copy_covers=prefs["covers"], on_progress=progress)
            out = result.to_dict()
            out.update(playlists_written=0, playlists_removed=0)
            if result.aborted is None:
                stored = _prefs(_row(conn, device_id))
                wanted: list[tuple[str, list[int]]] = []
                if prefs["playlists"]:
                    for playlist_id in selection.playlist_sources(prefs["sources"]):
                        name = conn.execute("SELECT name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
                        if name is None:
                            continue
                        ids = [r["id"] for r in selection.resolve_sources(conn, [{"kind": "playlist", "id": playlist_id}])]
                        wanted.append((name["name"], ids))
                written, names = mirror.write_playlists(conn, device_id, target, wanted) if wanted else (0, [])
                stale = [n for n in stored["written_playlists"] if n not in names]
                out["playlists_written"] = written
                out["playlists_removed"] = mirror.remove_playlists(target, stale) if stale else 0
                stored["written_playlists"] = names
                _save_prefs(conn, device_id, stored)
        ctx.previews.pop(f"sync:{device_id}", None)
        out.update(label=row["label"], device_id=device_id)
        return out

    title = f"Copying {_plural(len(plan.to_copy), 'song')} to {row['label']}"
    return {"job": ctx.jobs.submit("sync", title, run, lane="device").to_dict()}
