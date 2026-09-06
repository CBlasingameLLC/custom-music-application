from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("musictoolkit")

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_path_component(value: str) -> str:
    cleaned = _INVALID_CHARS.sub("_", value).strip().rstrip(".")
    return cleaned or "Unknown"


@dataclass
class MoveProposal:
    track_id: int
    old_path: Path
    new_path: Path


@dataclass
class OrganizeResult:
    proposals: list[MoveProposal]
    unchanged: int = 0
    collisions: list[tuple[Path, Path]] = field(default_factory=list)


def compute_target_path(row: sqlite3.Row, library_root: Path, scheme: str) -> Path:
    fields = {
        "album_artist": sanitize_path_component(row["album_artist"] or row["artist"] or "Unknown Artist"),
        "artist": sanitize_path_component(row["artist"] or "Unknown Artist"),
        "album": sanitize_path_component(row["album"] or "Unknown Album"),
        "title": sanitize_path_component(row["title"] or Path(row["file_path"]).stem),
        "track": row["track_number"] or 0,
        "year": row["year"] or 0,
        "ext": Path(row["file_path"]).suffix.lstrip(".").lower(),
    }
    relative = scheme.format(**fields)
    return (library_root / relative).resolve()


def propose_organization(conn: sqlite3.Connection, library_root: Path, scheme: str) -> OrganizeResult:
    library_root = library_root.resolve()
    result = OrganizeResult(proposals=[])
    rows = conn.execute("SELECT * FROM tracks WHERE is_missing = 0").fetchall()
    rows = [r for r in rows if Path(r["file_path"]).is_relative_to(library_root)]

    planned_targets: set[Path] = set()
    for row in rows:
        old_path = Path(row["file_path"])
        try:
            new_path = compute_target_path(row, library_root, scheme)
        except (KeyError, ValueError, IndexError) as exc:
            logger.warning("Could not compute target path for %s: %s", old_path, exc)
            continue

        if new_path == old_path:
            result.unchanged += 1
            continue

        if new_path in planned_targets or (new_path.exists() and new_path != old_path):
            result.collisions.append((old_path, new_path))
            continue

        planned_targets.add(new_path)
        result.proposals.append(MoveProposal(track_id=row["id"], old_path=old_path, new_path=new_path))

    return result


def apply_organization(conn: sqlite3.Connection, proposals: list[MoveProposal]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    moved = 0
    for p in proposals:
        if p.new_path.exists():
            logger.warning("Skipping %s -> %s: destination now exists", p.old_path, p.new_path)
            continue
        p.new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p.old_path), str(p.new_path))
        logger.info("Moved %s -> %s", p.old_path, p.new_path)
        conn.execute(
            "UPDATE tracks SET file_path = ?, date_last_scanned = ? WHERE id = ?",
            (str(p.new_path), now, p.track_id),
        )
        moved += 1
    conn.commit()
    return moved
