from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

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


SCHEME_FIELDS = ("album_artist", "artist", "album", "title", "track", "year", "ext")


def _is_plain_relative(relative: str) -> bool:
    """False for rooted ("/x", "\\x"), drive-qualified ("C:x") and parent-escaping ("../x") paths.

    Judged by both path flavours, whatever the platform: "/x" has no drive letter, so Windows does not
    call it absolute, yet joined onto a folder it still lands at the drive root, outside the library."""
    windows = PureWindowsPath(relative)
    if windows.drive or windows.root or PurePosixPath(relative).is_absolute():
        return False
    return ".." not in windows.parts and ".." not in PurePosixPath(relative).parts


def validate_scheme(scheme: str) -> None:
    """Raise ValueError (with a message fit for the Settings page) if the template can't be filled in."""
    sample = {"album_artist": "A", "artist": "A", "album": "B", "title": "C", "track": 1, "year": 2000, "ext": "mp3"}
    try:
        relative = scheme.format(**sample)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            f"Invalid template ({type(exc).__name__}: {exc}). Available fields: "
            + ", ".join("{" + name + "}" for name in SCHEME_FIELDS)
        ) from None
    if not scheme.strip() or not _is_plain_relative(relative):
        raise ValueError("The template must be a relative path like {album_artist}/{album}/{track:02d} - {title}.{ext}")
    if "{ext}" not in scheme:
        raise ValueError("The template must end with the file extension: {ext}")


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
    if not _is_plain_relative(relative):
        raise ValueError(f"template result {relative!r} is not a path inside the library")
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
