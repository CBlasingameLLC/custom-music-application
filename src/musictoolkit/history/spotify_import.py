from __future__ import annotations

import json
import logging
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from musictoolkit.integrations import listenbrainz_client

logger = logging.getLogger("musictoolkit")

_AUDIO_HISTORY_GLOB = "Streaming_History_Audio_*.json"


@dataclass
class ImportSummary:
    total_rows_seen: int = 0
    music_rows: int = 0
    podcast_rows_skipped: int = 0
    date_range: tuple[str, str] | None = None
    top_artists: list[tuple[str, int]] = field(default_factory=list)
    inserted: int = 0
    duplicates_skipped: int = 0
    matched_to_library: int = 0


def _extract_json_files(source: Path, work_dir: Path) -> list[Path]:
    if source.is_dir():
        return sorted(source.rglob(_AUDIO_HISTORY_GLOB))
    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as zf:
            names = [n for n in zf.namelist() if Path(n).match(_AUDIO_HISTORY_GLOB)]
            zf.extractall(work_dir, members=names)
        return sorted((work_dir / n) for n in names)
    raise ValueError(f"Expected a directory or .zip file, got: {source}")


def _load_records(json_files: list[Path]) -> pd.DataFrame:
    frames = []
    for path in json_files:
        with path.open("r", encoding="utf-8") as f:
            frames.append(pd.DataFrame(json.load(f)))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _music_mask(df: pd.DataFrame) -> pd.Series:
    """Extended Streaming History mixes music and podcast-episode rows in
    the same Audio files; podcast rows have no spotify_track_uri."""
    if "spotify_track_uri" not in df.columns:
        return pd.Series(False, index=df.index)
    return df["spotify_track_uri"].notna()


def summarize(df: pd.DataFrame) -> ImportSummary:
    summary = ImportSummary(total_rows_seen=len(df))
    if df.empty:
        return summary

    music_df = df[_music_mask(df)]
    summary.music_rows = len(music_df)
    summary.podcast_rows_skipped = summary.total_rows_seen - summary.music_rows

    if not music_df.empty:
        timestamps = pd.to_datetime(music_df["ts"], utc=True, errors="coerce").dropna()
        if not timestamps.empty:
            summary.date_range = (timestamps.min().isoformat(), timestamps.max().isoformat())

        if "master_metadata_album_artist_name" in music_df.columns:
            top = music_df["master_metadata_album_artist_name"].dropna().value_counts().head(10)
            summary.top_artists = list(top.items())

    return summary


def import_history(
    conn: sqlite3.Connection, source: Path, work_dir: Path, import_batch_id: str | None = None
) -> ImportSummary:
    """Parse a Spotify Extended Streaming History export and insert music
    listens into play_history. Safe to re-run on the same or an overlapping
    export: duplicate (source, spotify_track_uri, played_at_epoch) rows are
    silently skipped via the table's own uniqueness constraint."""
    json_files = _extract_json_files(source, work_dir)
    if not json_files:
        raise ValueError(
            f"No Streaming_History_Audio_*.json files found in {source}. "
            "Make sure you requested 'Extended streaming history' from Spotify, not just 'Account data'."
        )

    df = _load_records(json_files)
    summary = summarize(df)
    if df.empty:
        return summary

    batch_id = import_batch_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for _, record in df[_music_mask(df)].iterrows():
        played_at = pd.to_datetime(record.get("ts"), utc=True, errors="coerce")
        if pd.isna(played_at):
            continue

        ms_played = record.get("ms_played")
        raw_artist = record.get("master_metadata_album_artist_name")
        raw_track = record.get("master_metadata_track_name")
        raw_album = record.get("master_metadata_album_album_name")

        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO play_history (
                track_id, source, played_at_epoch, ms_played,
                raw_artist_name, raw_track_name, raw_album_name,
                spotify_track_uri, listenbrainz_submitted, import_batch_id
            ) VALUES (NULL, 'spotify_import', ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                int(played_at.timestamp()),
                int(ms_played) if pd.notna(ms_played) else None,
                raw_artist if pd.notna(raw_artist) else None,
                raw_track if pd.notna(raw_track) else None,
                raw_album if pd.notna(raw_album) else None,
                record.get("spotify_track_uri"),
                batch_id,
            ),
        )
        if cursor.rowcount:
            summary.inserted += 1
        else:
            summary.duplicates_skipped += 1

    conn.commit()
    _cross_reference_to_library(conn, batch_id)
    summary.matched_to_library = conn.execute(
        "SELECT COUNT(*) AS c FROM play_history WHERE import_batch_id = ? AND track_id IS NOT NULL", (batch_id,)
    ).fetchone()["c"]

    return summary


def _normalize(value) -> str:
    if not value:
        return ""
    return "".join(ch.lower() for ch in str(value) if ch.isalnum())


def _cross_reference_to_library(conn: sqlite3.Connection, batch_id: str) -> None:
    """Best-effort match of imported history rows to owned tracks by
    normalized artist+title. No fuzzy matching in v1 — this only feeds the
    local recommendation ranking signal, it isn't a correctness-critical
    path, so a missed match just means one fewer ranking data point."""
    unmatched = conn.execute(
        "SELECT id, raw_artist_name, raw_track_name FROM play_history WHERE import_batch_id = ? AND track_id IS NULL",
        (batch_id,),
    ).fetchall()
    if not unmatched:
        return

    library_index: dict[tuple[str, str], int] = {}
    for row in conn.execute("SELECT id, artist, title FROM tracks WHERE is_missing = 0"):
        key = (_normalize(row["artist"]), _normalize(row["title"]))
        if key[0] and key[1]:
            library_index.setdefault(key, row["id"])

    for row in unmatched:
        key = (_normalize(row["raw_artist_name"]), _normalize(row["raw_track_name"]))
        track_id = library_index.get(key)
        if track_id:
            conn.execute("UPDATE play_history SET track_id = ? WHERE id = ?", (track_id, row["id"]))
    conn.commit()


def _build_listen_payload(row: sqlite3.Row) -> dict:
    metadata = {"artist_name": row["raw_artist_name"], "track_name": row["raw_track_name"]}
    if row["raw_album_name"]:
        metadata["release_name"] = row["raw_album_name"]
    if row["spotify_track_uri"]:
        metadata["additional_info"] = {
            "spotify_id": row["spotify_track_uri"].replace("spotify:track:", "https://open.spotify.com/track/")
        }
    return {"listened_at": row["played_at_epoch"], "track_metadata": metadata}


def backfill_to_listenbrainz(conn: sqlite3.Connection, user_token: str) -> int:
    """Submit not-yet-submitted spotify_import listens to the user's own
    ListenBrainz account, batched to the API's limit. Each batch is marked
    submitted immediately after it succeeds, so an interrupted run resumes
    from where it left off rather than re-submitting or losing progress."""
    pending = conn.execute(
        "SELECT * FROM play_history WHERE source = 'spotify_import' AND listenbrainz_submitted = 0"
    ).fetchall()

    submittable = [row for row in pending if row["raw_artist_name"] and row["raw_track_name"]]
    submitted_count = 0
    batch_size = listenbrainz_client.MAX_LISTENS_PER_REQUEST

    for start in range(0, len(submittable), batch_size):
        batch = submittable[start : start + batch_size]
        listenbrainz_client.submit_listens(user_token, [_build_listen_payload(row) for row in batch])
        conn.executemany(
            "UPDATE play_history SET listenbrainz_submitted = 1 WHERE id = ?", [(row["id"],) for row in batch]
        )
        conn.commit()
        submitted_count += len(batch)

    return submitted_count
