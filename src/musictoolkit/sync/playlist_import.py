from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def parse_m3u(path: Path) -> list[Path]:
    """Parse an M3U/M3U8 playlist into resolved file paths. Relative entries
    resolve against the playlist file's own directory, matching how every
    common player writes and reads these files. '#'-prefixed lines (EXTM3U
    header, EXTINF metadata) are directives, not entries."""
    base_dir = path.resolve().parent
    encoding = "utf-8" if path.suffix.lower() == ".m3u8" else "utf-8-sig"
    entries: list[Path] = []
    with path.open("r", encoding=encoding, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            entry_path = Path(line)
            if not entry_path.is_absolute():
                entry_path = (base_dir / entry_path).resolve()
            entries.append(entry_path)
    return entries


def import_playlist(conn: sqlite3.Connection, m3u_path: Path, playlist_name: str | None = None) -> int:
    """Import an M3U's entries as a playlist, matching each line to an
    existing track by resolved file path. Entries not already in the
    library are silently skipped (reflected in the returned match count),
    not treated as an error — the playlist may reference files never
    scanned into this library."""
    name = playlist_name or m3u_path.stem
    now = datetime.now(timezone.utc).isoformat()

    conn.execute("INSERT INTO playlists (name, source, created_at) VALUES (?, 'm3u_import', ?)", (name, now))
    playlist_id = conn.execute(
        "SELECT id FROM playlists WHERE name = ? AND source = 'm3u_import' ORDER BY id DESC LIMIT 1", (name,)
    ).fetchone()["id"]

    matched = 0
    for position, entry_path in enumerate(parse_m3u(m3u_path)):
        row = conn.execute("SELECT id FROM tracks WHERE file_path = ?", (str(entry_path),)).fetchone()
        if row is None:
            continue
        conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
            (playlist_id, row["id"], position),
        )
        matched += 1

    conn.commit()
    return matched
