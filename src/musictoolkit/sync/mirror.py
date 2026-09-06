from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("musictoolkit")

_FAT_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_COMPONENT_LENGTH = 255


def sanitize_for_fat(value: str) -> str:
    """Generic DAP microSD cards are typically FAT32/exFAT — strip characters
    invalid on those filesystems and respect path-length limits."""
    cleaned = _FAT_INVALID_CHARS.sub("_", value).strip().rstrip(".")
    cleaned = cleaned[:_MAX_COMPONENT_LENGTH]
    return cleaned or "Unknown"


def _compute_dest_path(row: sqlite3.Row, device_root: Path, scheme: str) -> Path:
    fields = {
        "album_artist": sanitize_for_fat(row["album_artist"] or row["artist"] or "Unknown Artist"),
        "artist": sanitize_for_fat(row["artist"] or "Unknown Artist"),
        "album": sanitize_for_fat(row["album"] or "Unknown Album"),
        "title": sanitize_for_fat(row["title"] or Path(row["file_path"]).stem),
        "track": row["track_number"] or 0,
        "year": row["year"] or 0,
        "ext": Path(row["file_path"]).suffix.lstrip(".").lower(),
    }
    relative = scheme.format(**fields)
    return device_root / relative


@dataclass
class CopyItem:
    row: sqlite3.Row
    dest: Path
    old_relative_path: str | None = None  # set only if this track was previously synced to a different path


@dataclass
class SyncPlan:
    to_copy: list[CopyItem] = field(default_factory=list)
    to_prune: list[tuple[int, str]] = field(default_factory=list)  # (manifest_id, dest_relative_path)
    unchanged: int = 0
    total_bytes_to_copy: int = 0
    free_bytes_on_device: int = 0


def has_sufficient_space(plan: SyncPlan) -> bool:
    return plan.total_bytes_to_copy <= plan.free_bytes_on_device


def get_or_create_device(conn: sqlite3.Connection, mount_path: str, label: str | None = None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute("SELECT id FROM devices WHERE last_seen_mount_path = ?", (mount_path,)).fetchone()
    if row:
        return row["id"]
    conn.execute(
        "INSERT INTO devices (label, last_seen_mount_path, created_at) VALUES (?, ?, ?)",
        (label or mount_path, mount_path, now),
    )
    conn.commit()
    return conn.execute("SELECT id FROM devices WHERE last_seen_mount_path = ?", (mount_path,)).fetchone()["id"]


def plan_sync(
    conn: sqlite3.Connection,
    device_id: int,
    device_root: Path,
    selected_rows: list[sqlite3.Row],
    scheme: str,
    free_bytes_on_device: int,
) -> SyncPlan:
    plan = SyncPlan(free_bytes_on_device=free_bytes_on_device)
    selected_track_ids = {row["id"] for row in selected_rows}

    for row in selected_rows:
        dest = _compute_dest_path(row, device_root, scheme)
        dest_relative = str(dest.relative_to(device_root))

        manifest_row = conn.execute(
            "SELECT * FROM sync_manifest WHERE device_id = ? AND track_id = ?", (device_id, row["id"])
        ).fetchone()

        source_path = Path(row["file_path"])
        try:
            source_mtime = source_path.stat().st_mtime
        except OSError:
            logger.warning("Source file missing, skipping: %s", source_path)
            continue

        if (
            manifest_row is not None
            and manifest_row["source_file_mtime"] == source_mtime
            and manifest_row["dest_relative_path"] == dest_relative
        ):
            plan.unchanged += 1
            continue

        old_relative = None
        if manifest_row is not None and manifest_row["dest_relative_path"] != dest_relative:
            old_relative = manifest_row["dest_relative_path"]

        plan.to_copy.append(CopyItem(row=row, dest=dest, old_relative_path=old_relative))
        plan.total_bytes_to_copy += row["file_size"] or 0

    for m_row in conn.execute("SELECT * FROM sync_manifest WHERE device_id = ?", (device_id,)).fetchall():
        if m_row["track_id"] not in selected_track_ids:
            plan.to_prune.append((m_row["id"], m_row["dest_relative_path"]))

    return plan


def apply_sync(
    conn: sqlite3.Connection, device_id: int, device_root: Path, plan: SyncPlan, prune: bool
) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    copied = 0
    pruned = 0

    for item in plan.to_copy:
        if item.old_relative_path:
            old_dest = device_root / item.old_relative_path
            if old_dest.exists() and old_dest != item.dest:
                old_dest.unlink()
                logger.info("Removed stale on-device copy %s (superseded by %s)", old_dest, item.dest)

        item.dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.row["file_path"], item.dest)
        dest_relative = str(item.dest.relative_to(device_root))
        source_mtime = Path(item.row["file_path"]).stat().st_mtime

        conn.execute(
            """
            INSERT INTO sync_manifest (device_id, track_id, dest_relative_path, source_file_hash, source_file_mtime, synced_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'synced')
            ON CONFLICT(device_id, track_id) DO UPDATE SET
                dest_relative_path = excluded.dest_relative_path,
                source_file_mtime = excluded.source_file_mtime,
                synced_at = excluded.synced_at,
                status = 'synced'
            """,
            (device_id, item.row["id"], dest_relative, item.row["file_hash"], source_mtime, now),
        )
        logger.info("Synced %s -> %s", item.row["file_path"], item.dest)
        copied += 1

    if prune:
        for manifest_id, relative_path in plan.to_prune:
            dest = device_root / relative_path
            if dest.exists():
                dest.unlink()
                logger.info("Pruned %s", dest)
            conn.execute("DELETE FROM sync_manifest WHERE id = ?", (manifest_id,))
            pruned += 1

    conn.commit()
    return copied, pruned
