from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import mutagen

logger = logging.getLogger("musictoolkit")

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".wav", ".wma", ".aiff", ".ape"}


@dataclass
class ScanResult:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    missing: int = 0
    errors: int = 0


def find_audio_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS)


def _parse_leading_int(value: str) -> int | None:
    digits = ""
    for ch in value:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None


def _read_basic_tags(path: Path) -> dict:
    """Best-effort tag read via mutagen's Easy interface. Never raises —
    a file that fails to parse just yields all-None tag fields."""
    fields: dict = {
        "title": None, "artist": None, "album_artist": None, "album": None,
        "track_number": None, "disc_number": None, "year": None, "genre": None,
        "duration_seconds": None, "format": path.suffix.lower().lstrip("."), "bitrate": None,
    }
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        logger.warning("Failed to read tags from %s", path, exc_info=True)
        return fields

    if audio is None:
        return fields

    if audio.info is not None:
        fields["duration_seconds"] = getattr(audio.info, "length", None)
        fields["bitrate"] = getattr(audio.info, "bitrate", None)

    tags = audio.tags or {}

    def first(key: str) -> str | None:
        values = tags.get(key)
        return values[0] if values else None

    fields["title"] = first("title")
    fields["artist"] = first("artist")
    fields["album_artist"] = first("albumartist")
    fields["album"] = first("album")
    fields["genre"] = first("genre")

    track_raw = first("tracknumber")
    if track_raw:
        fields["track_number"] = _parse_leading_int(track_raw)

    disc_raw = first("discnumber")
    if disc_raw:
        fields["disc_number"] = _parse_leading_int(disc_raw)

    date_raw = first("date")
    if date_raw:
        fields["year"] = _parse_leading_int(date_raw[:4])

    return fields


def compute_content_hash(path: Path) -> str:
    """Real content hash — computed lazily, only for an explicit dedupe pass."""
    h = hashlib.blake2b()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_library(conn: sqlite3.Connection, root: Path) -> ScanResult:
    root = root.resolve()
    result = ScanResult()
    now = datetime.now(timezone.utc).isoformat()
    seen_paths: set[str] = set()

    for path in find_audio_files(root):
        seen_paths.add(str(path))
        try:
            stat = path.stat()
        except OSError:
            logger.warning("Could not stat %s, skipping", path)
            result.errors += 1
            continue

        existing = conn.execute(
            "SELECT id, file_size, file_mtime, is_missing FROM tracks WHERE file_path = ?",
            (str(path),),
        ).fetchone()

        if (
            existing is not None
            and not existing["is_missing"]
            and existing["file_size"] == stat.st_size
            and existing["file_mtime"] == stat.st_mtime
        ):
            result.unchanged += 1
            conn.execute("UPDATE tracks SET date_last_scanned = ? WHERE id = ?", (now, existing["id"]))
            continue

        tags = _read_basic_tags(path)

        if existing is None:
            conn.execute(
                """
                INSERT INTO tracks (
                    file_path, file_size, file_mtime, duration_seconds,
                    title, artist, album_artist, album, track_number,
                    disc_number, year, genre, format, bitrate,
                    tag_source, date_added, date_last_scanned, is_missing
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'original', ?, ?, 0)
                """,
                (
                    str(path), stat.st_size, stat.st_mtime, tags["duration_seconds"],
                    tags["title"], tags["artist"], tags["album_artist"], tags["album"],
                    tags["track_number"], tags["disc_number"], tags["year"], tags["genre"],
                    tags["format"], tags["bitrate"], now, now,
                ),
            )
            result.added += 1
        else:
            conn.execute(
                """
                UPDATE tracks SET
                    file_size = ?, file_mtime = ?, duration_seconds = ?,
                    title = ?, artist = ?, album_artist = ?, album = ?,
                    track_number = ?, disc_number = ?, year = ?, genre = ?,
                    format = ?, bitrate = ?, date_last_scanned = ?, is_missing = 0
                WHERE id = ?
                """,
                (
                    stat.st_size, stat.st_mtime, tags["duration_seconds"],
                    tags["title"], tags["artist"], tags["album_artist"], tags["album"],
                    tags["track_number"], tags["disc_number"], tags["year"], tags["genre"],
                    tags["format"], tags["bitrate"], now, existing["id"],
                ),
            )
            result.updated += 1

    all_tracks = conn.execute("SELECT id, file_path, is_missing FROM tracks").fetchall()
    for row in all_tracks:
        row_path = Path(row["file_path"])
        if row_path.is_relative_to(root) and row["file_path"] not in seen_paths and not row["is_missing"]:
            conn.execute("UPDATE tracks SET is_missing = 1 WHERE id = ?", (row["id"],))
            result.missing += 1

    conn.commit()
    return result
