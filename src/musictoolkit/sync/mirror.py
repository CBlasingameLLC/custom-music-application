"""Copying a chosen part of the library onto a device (an SD card, a player in drive mode, a USB stick).

What happens is always previewed first (plan_sync) and then done from that plan (run_sync):
- files are copied under a folder layout built from the tags, with names made safe for FAT/exFAT;
- a copy goes to a temporary name and is renamed once complete, so a card pulled out mid-copy never holds a
  half-written song that looks finished;
- the manifest remembers what was copied where, so the next sync only copies what is new or changed, notices
  a file someone deleted from the device, and can remove what is no longer selected;
- only files this app copied are ever removed (they are in the manifest); everything else on the device is
  left alone;
- playlists can be written next to the music as .m3u8 files with relative paths.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from musictoolkit.ingest import moves, organizer

logger = logging.getLogger("musictoolkit")

_FAT_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_COMPONENT_LENGTH = 255
FAT_FILE_LIMIT = 4 * 1024**3 - 1  # a FAT32 file can't be 4 GiB or larger
FAT_NAMES = {"fat", "fat32", "vfat", "msdos"}
MAX_PATH = 259
PARTIAL_SUFFIX = ".mtk-partial"
PLAYLIST_DIR = "Playlists"
COMMIT_EVERY = 25
ABORT_AFTER_ERRORS = 5  # consecutive failures with the device gone

Progress = Callable[[int, int, int], None]  # (files done, files total, bytes copied so far)


def sanitize_for_fat(value: str) -> str:
    """Generic DAP microSD cards are typically FAT32/exFAT — strip characters
    invalid on those filesystems and respect path-length limits."""
    cleaned = _FAT_INVALID_CHARS.sub("_", value).strip().rstrip(".")
    cleaned = cleaned[:_MAX_COMPONENT_LENGTH]
    return cleaned or "Unknown"


def _compute_dest_path(row: sqlite3.Row, device_root: Path, scheme: str) -> Path:
    return device_root / organizer.render_relative(row, scheme, sanitize_for_fat)


@dataclass
class CopyItem:
    row: sqlite3.Row
    dest: Path
    old_relative_path: str | None = None  # set only if this track was previously synced to a different path
    reason: str = "new"  # new | changed | moved | missing (removed from the device) | damaged (wrong size)


@dataclass
class SkippedItem:
    track_id: int
    source: str
    reason: str  # collision | too_big | path_too_long | source_missing | layout
    detail: str = ""


@dataclass
class SyncPlan:
    to_copy: list[CopyItem] = field(default_factory=list)
    to_prune: list[tuple[int, str]] = field(default_factory=list)  # (manifest_id, dest_relative_path)
    unchanged: int = 0
    total_bytes_to_copy: int = 0
    free_bytes_on_device: int = 0
    prune_bytes: int = 0  # what removing the to_prune files would give back
    skipped: list[SkippedItem] = field(default_factory=list)
    selected: int = 0

    def needed_bytes(self, prune: bool) -> int:
        return max(0, self.total_bytes_to_copy - (self.prune_bytes if prune else 0))


def has_sufficient_space(plan: SyncPlan, prune: bool = False) -> bool:
    return plan.needed_bytes(prune) <= plan.free_bytes_on_device


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
    fs: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> SyncPlan:
    """What a sync would do, from the library, the manifest and what is actually on the device. Touches nothing."""
    plan = SyncPlan(free_bytes_on_device=free_bytes_on_device, selected=len(selected_rows))
    fat = (fs or "").lower() in FAT_NAMES
    selected_track_ids = {row["id"] for row in selected_rows}
    claimed: dict[str, int] = {}  # normcase'd destination -> the song that has it

    manifest = {m["track_id"]: m for m in conn.execute("SELECT * FROM sync_manifest WHERE device_id = ?", (device_id,))}

    for done, row in enumerate(selected_rows):
        if on_progress and done % 100 == 0:
            on_progress(done, len(selected_rows))
        source_path = Path(row["file_path"])
        try:
            source_stat = source_path.stat()
        except OSError:
            logger.warning("Source file missing, skipping: %s", source_path)
            plan.skipped.append(SkippedItem(row["id"], str(source_path), "source_missing", "The file is not where the library says"))
            continue
        try:
            dest = _compute_dest_path(row, device_root, scheme)
        except (KeyError, ValueError, IndexError) as exc:
            plan.skipped.append(SkippedItem(row["id"], str(source_path), "layout", f"The folder layout cannot be applied ({exc})"))
            continue
        dest_relative = str(dest.relative_to(device_root))

        if fat and source_stat.st_size > FAT_FILE_LIMIT:
            plan.skipped.append(SkippedItem(row["id"], str(source_path), "too_big", "A FAT32 card cannot hold a file of 4 GB or more"))
            continue
        if len(str(dest)) > MAX_PATH:
            plan.skipped.append(SkippedItem(row["id"], str(source_path), "path_too_long", f"The path on the device would be {len(str(dest))} characters"))
            continue
        key = os.path.normcase(str(dest))
        if key in claimed:
            plan.skipped.append(SkippedItem(row["id"], str(source_path), "collision", f"Another song already takes {dest_relative}"))
            continue
        claimed[key] = row["id"]

        entry = manifest.get(row["id"])
        reason = "new"
        old_relative = None
        if entry is not None:
            on_device = dest if entry["dest_relative_path"] == dest_relative else device_root / entry["dest_relative_path"]
            expected = entry["size"] if entry["size"] is not None else source_stat.st_size
            try:
                exists = on_device.is_file()
                size_ok = exists and on_device.stat().st_size == expected
            except OSError:
                exists = size_ok = False
            if entry["source_file_mtime"] == source_stat.st_mtime and entry["dest_relative_path"] == dest_relative and size_ok:
                plan.unchanged += 1
                continue
            if entry["dest_relative_path"] != dest_relative:
                old_relative, reason = entry["dest_relative_path"], "moved"
            elif not exists:
                reason = "missing"  # someone removed it from the device
            elif not size_ok:
                reason = "damaged"  # a different size than what was copied: cut short, or replaced
            else:
                reason = "changed"  # the song in the library changed since it was copied

        plan.to_copy.append(CopyItem(row=row, dest=dest, old_relative_path=old_relative, reason=reason))
        plan.total_bytes_to_copy += source_stat.st_size

    for m_row in manifest.values():
        if m_row["track_id"] not in selected_track_ids:
            plan.to_prune.append((m_row["id"], m_row["dest_relative_path"]))
            plan.prune_bytes += m_row["size"] or 0
    if on_progress:
        on_progress(len(selected_rows), len(selected_rows))
    return plan


# ------------------------------------------------------------------------------------------------- doing it


@dataclass
class SyncResult:
    copied: int = 0
    pruned: int = 0
    bytes_copied: int = 0
    covers: int = 0
    playlists: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    aborted: str | None = None  # why the run stopped early (device full or removed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "copied": self.copied, "pruned": self.pruned, "bytes_copied": self.bytes_copied, "covers": self.covers,
            "playlists": self.playlists, "errors": self.errors[:100], "errors_total": len(self.errors), "aborted": self.aborted,
        }


def _copy_file(source: str | Path, dest: Path) -> int:
    """Copy through a temporary name and rename when complete. Returns the size of the finished copy."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + PARTIAL_SUFFIX)
    try:
        shutil.copyfile(source, partial)
        try:
            shutil.copystat(source, partial)
        except OSError:
            pass  # some file systems refuse to take the date; the copy is still good
        os.replace(partial, dest)
    except BaseException:
        try:
            partial.unlink()
        except OSError:
            pass
        raise
    return dest.stat().st_size


def _copy_cover(source_dir: Path, dest_dir: Path) -> bool:
    """Take the folder picture (cover.jpg and friends) along when the device folder has none."""
    try:
        if any(moves.is_folder_art(e.name) for e in dest_dir.iterdir()):
            return False
        art = next((e for e in source_dir.iterdir() if e.is_file() and moves.is_folder_art(e.name)), None)
        if art is None:
            return False
        shutil.copyfile(art, dest_dir / art.name)
        return True
    except OSError:
        return False


def _remove_stale(device_root: Path, relative: str) -> None:
    """Remove a file this app copied earlier, and the folders that leaves empty. Never leaves the device folder."""
    target = (device_root / relative).resolve()
    if not target.is_relative_to(device_root.resolve()):
        logger.warning("Refusing to remove %s: it is outside the device folder", target)
        return
    if target.is_file():
        target.unlink()
        moves.prune_empty_folders(target.parent, device_root.resolve(), allow_art=True)


def run_sync(
    conn: sqlite3.Connection,
    device_id: int,
    device_root: Path,
    plan: SyncPlan,
    prune: bool = False,
    copy_covers: bool = True,
    on_progress: Progress | None = None,
) -> SyncResult:
    """Do what the plan says: removals first (they free space), then copies. If `on_progress` raises (a cancelled
    job) the manifest keeps everything copied so far. Returns what happened."""
    result = SyncResult()
    now = datetime.now(timezone.utc).isoformat()
    total = len(plan.to_copy)
    covered: set[Path] = set()
    failures_in_a_row = 0
    pending_commit = 0

    try:
        if prune:
            for manifest_id, relative in plan.to_prune:
                try:
                    _remove_stale(device_root, relative)
                except OSError as exc:
                    result.errors.append({"path": relative, "error": f"Could not remove it from the device ({exc.strerror or exc})"})
                    continue
                conn.execute("DELETE FROM sync_manifest WHERE id = ?", (manifest_id,))
                result.pruned += 1
            conn.commit()

        for done, item in enumerate(plan.to_copy):
            if on_progress:
                on_progress(done, total, result.bytes_copied)
            source = Path(item.row["file_path"])
            try:
                size = _copy_file(source, item.dest)
                source_mtime = source.stat().st_mtime
                if item.old_relative_path and device_root / item.old_relative_path != item.dest:
                    _remove_stale(device_root, item.old_relative_path)  # the old copy goes only once the new one is in place
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    result.aborted = "The device is full. What was copied so far is kept."
                    result.errors.append({"path": str(source), "error": "No space left on the device"})
                    break
                failures_in_a_row += 1
                gone = not device_root.exists()
                result.errors.append({"path": str(source), "error": "The device was disconnected" if gone else f"{exc.strerror or exc}"})
                if gone or failures_in_a_row >= ABORT_AFTER_ERRORS:
                    result.aborted = "The device stopped answering (was it unplugged?). What was copied so far is kept."
                    break
                continue
            failures_in_a_row = 0
            conn.execute(
                """
                INSERT INTO sync_manifest (device_id, track_id, dest_relative_path, source_file_hash, source_file_mtime, synced_at, status, size)
                VALUES (?, ?, ?, ?, ?, ?, 'synced', ?)
                ON CONFLICT(device_id, track_id) DO UPDATE SET
                    dest_relative_path = excluded.dest_relative_path,
                    source_file_hash = excluded.source_file_hash,
                    source_file_mtime = excluded.source_file_mtime,
                    synced_at = excluded.synced_at,
                    status = 'synced',
                    size = excluded.size
                """,
                (device_id, item.row["id"], str(item.dest.relative_to(device_root)), item.row["file_hash"], source_mtime, now, size),
            )
            result.copied += 1
            result.bytes_copied += size
            pending_commit += 1
            if pending_commit >= COMMIT_EVERY:
                conn.commit()
                pending_commit = 0
            if copy_covers and (source.parent, item.dest.parent) not in covered:
                covered.add((source.parent, item.dest.parent))
                result.covers += _copy_cover(source.parent, item.dest.parent)
        if on_progress and result.aborted is None:
            on_progress(total, total, result.bytes_copied)
    finally:
        conn.execute("UPDATE devices SET last_synced_at = ? WHERE id = ?", (now, device_id))
        conn.commit()
    return result


def apply_sync(
    conn: sqlite3.Connection, device_id: int, device_root: Path, plan: SyncPlan, prune: bool
) -> tuple[int, int]:
    """Command-line entry point: returns (copied, pruned)."""
    result = run_sync(conn, device_id, device_root, plan, prune)
    return result.copied, result.pruned


# ------------------------------------------------------------------------------------------------- playlists


def playlist_file_name(name: str) -> str:
    return sanitize_for_fat(name) + ".m3u8"


def write_playlists(
    conn: sqlite3.Connection, device_id: int, device_root: Path, playlists: list[tuple[str, list[int]]]
) -> tuple[int, list[str]]:
    """Write each playlist as `Playlists/<name>.m3u8` with paths relative to that folder, listing only songs that
    are on the device. Returns (written, names of playlist files this app wrote earlier and no longer wants)."""
    on_device = {
        m["track_id"]: m["dest_relative_path"]
        for m in conn.execute("SELECT track_id, dest_relative_path FROM sync_manifest WHERE device_id = ?", (device_id,))
    }
    folder = device_root / PLAYLIST_DIR
    written = 0
    names: set[str] = set()
    for name, track_ids in playlists:
        lines = ["#EXTM3U"]
        for track_id in track_ids:
            relative = on_device.get(track_id)
            if relative:
                lines.append(os.path.relpath(device_root / relative, folder).replace("\\", "/"))
        folder.mkdir(parents=True, exist_ok=True)
        file_name = playlist_file_name(name)
        (folder / file_name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        names.add(file_name)
        written += 1
    return written, sorted(names)


def remove_playlists(device_root: Path, file_names: list[str]) -> int:
    removed = 0
    folder = device_root / PLAYLIST_DIR
    for name in file_names:
        target = folder / name
        if target.is_file():
            target.unlink()
            removed += 1
    if folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
    return removed
