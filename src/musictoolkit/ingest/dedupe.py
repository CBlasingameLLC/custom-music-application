from __future__ import annotations

import logging
import shutil
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from musictoolkit.ingest.scanner import compute_content_hash

logger = logging.getLogger("musictoolkit")


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch.lower() for ch in value if ch.isalnum())


@dataclass
class DuplicateGroup:
    key: str
    reason: str
    track_ids: list[int]
    file_paths: list[str]


def find_duplicate_groups(
    conn: sqlite3.Connection, library_root: Path, use_content_hash: bool = False
) -> list[DuplicateGroup]:
    library_root = library_root.resolve()
    rows = conn.execute("SELECT * FROM tracks WHERE is_missing = 0").fetchall()
    rows = [r for r in rows if Path(r["file_path"]).is_relative_to(library_root)]

    groups: list[DuplicateGroup] = []
    covered_ids: set[int] = set()

    # Pass 1: exact MusicBrainz recording ID match — highest confidence.
    by_mbid: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        if row["musicbrainz_recording_id"]:
            by_mbid[row["musicbrainz_recording_id"]].append(row)
    for mbid, group_rows in by_mbid.items():
        if len(group_rows) > 1:
            groups.append(
                DuplicateGroup(
                    key=mbid,
                    reason="musicbrainz_recording_id",
                    track_ids=[r["id"] for r in group_rows],
                    file_paths=[r["file_path"] for r in group_rows],
                )
            )
            covered_ids.update(r["id"] for r in group_rows)

    # Pass 2: normalized artist+title+duration (2s bucket tolerance).
    by_signature: dict[tuple[str, str, int], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        if row["id"] in covered_ids:
            continue
        duration_bucket = round((row["duration_seconds"] or 0) / 2) * 2
        sig = (_normalize(row["artist"]), _normalize(row["title"]), duration_bucket)
        if sig[0] and sig[1]:
            by_signature[sig].append(row)
    for sig, group_rows in by_signature.items():
        if len(group_rows) > 1:
            groups.append(
                DuplicateGroup(
                    key=f"{sig[0]}::{sig[1]}",
                    reason="normalized_artist_title_duration",
                    track_ids=[r["id"] for r in group_rows],
                    file_paths=[r["file_path"] for r in group_rows],
                )
            )
            covered_ids.update(r["id"] for r in group_rows)

    # Pass 3 (opt-in, expensive): identical content hash for anything still ungrouped.
    if use_content_hash:
        by_hash: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            if row["id"] in covered_ids:
                continue
            file_hash = row["file_hash"] or compute_content_hash(Path(row["file_path"]))
            if row["file_hash"] != file_hash:
                conn.execute("UPDATE tracks SET file_hash = ? WHERE id = ?", (file_hash, row["id"]))
            by_hash[file_hash].append(row)
        conn.commit()
        for file_hash, group_rows in by_hash.items():
            if len(group_rows) > 1:
                groups.append(
                    DuplicateGroup(
                        key=file_hash,
                        reason="content_hash",
                        track_ids=[r["id"] for r in group_rows],
                        file_paths=[r["file_path"] for r in group_rows],
                    )
                )

    return groups


def _pick_keeper(conn: sqlite3.Connection, track_ids: list[int]) -> int:
    """Keep the highest-bitrate file as the best available-quality proxy; ties go to the oldest track id."""
    placeholders = ",".join("?" * len(track_ids))
    rows = conn.execute(f"SELECT id, bitrate FROM tracks WHERE id IN ({placeholders})", track_ids).fetchall()
    return max(rows, key=lambda r: (r["bitrate"] or 0, -r["id"]))["id"]


def apply_quarantine(conn: sqlite3.Connection, groups: list[DuplicateGroup], library_root: Path) -> int:
    quarantine_dir = library_root.resolve() / "_duplicates_review"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    moved = 0

    for group in groups:
        keeper_id = _pick_keeper(conn, group.track_ids)
        for track_id, file_path in zip(group.track_ids, group.file_paths):
            if track_id == keeper_id:
                continue
            old_path = Path(file_path)
            if not old_path.exists():
                continue
            new_path = quarantine_dir / old_path.name
            counter = 1
            while new_path.exists():
                new_path = quarantine_dir / f"{old_path.stem}_{counter}{old_path.suffix}"
                counter += 1
            shutil.move(str(old_path), str(new_path))
            conn.execute(
                "UPDATE tracks SET file_path = ?, date_last_scanned = ? WHERE id = ?",
                (str(new_path), now, track_id),
            )
            logger.info("Quarantined duplicate %s -> %s (keeper: track %d)", old_path, new_path, keeper_id)
            moved += 1
    conn.commit()
    return moved
