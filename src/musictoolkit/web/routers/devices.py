from __future__ import annotations

from fastapi import APIRouter, Depends

from musictoolkit.sync import device_detect
from musictoolkit.web.context import AppContext, get_ctx

router = APIRouter(prefix="/api/devices")


@router.get("")
def list_devices(ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.db() as conn:
        rows = conn.execute(
            """
            SELECT d.id, d.label, d.last_seen_mount_path, d.created_at, COUNT(m.id) AS synced
            FROM devices d LEFT JOIN sync_manifest m ON m.device_id = d.id AND m.status = 'synced'
            GROUP BY d.id ORDER BY d.created_at DESC
            """
        ).fetchall()
    return {
        "items": [
            {"id": r["id"], "label": r["label"], "mount_path": r["last_seen_mount_path"],
             "created_at": r["created_at"], "synced": r["synced"]}
            for r in rows
        ]
    }


@router.get("/volumes")
def volumes() -> dict:
    """Every mounted volume; the removable-looking ones are flagged, never auto-selected."""
    return {
        "items": [
            {"mount_path": c.mountpoint, "device": c.device, "fs": c.fstype, "total": c.total_bytes,
             "free": c.free_bytes, "removable": c.likely_removable}
            for c in device_detect.list_candidate_devices()
        ]
    }
