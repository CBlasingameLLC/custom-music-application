from __future__ import annotations

import json
import logging
import sqlite3
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from musictoolkit.integrations import listenbrainz_client
from musictoolkit.integrations.submission import submit_with_isolation, with_patience

logger = logging.getLogger("musictoolkit")

_AUDIO_HISTORY_GLOB = "Streaming_History_Audio_*.json"
MAX_EXPORT_BYTES = 2 * 1024**3  # a real export is a few hundred MB at most; anything near this is not one
PROGRESS_EVERY = 1000  # rows between progress reports while importing


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
    batch_id: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["top_artists"] = [{"name": name, "plays": int(plays)} for name, plays in self.top_artists]
        data["date_range"] = list(self.date_range) if self.date_range else None
        return data


def _check_size(total: int) -> None:
    if total > MAX_EXPORT_BYTES:
        raise ValueError("That is far bigger than a Spotify history export (over 2 GB of listening data). Is it the right file?")


def _extract_json_files(source: Path, work_dir: Path) -> list[Path]:
    if source.is_dir():
        files = sorted(source.rglob(_AUDIO_HISTORY_GLOB))
        _check_size(sum(f.stat().st_size for f in files))
        return files
    if source.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(source) as zf:
                members = [i for i in zf.infolist() if not i.is_dir() and Path(i.filename).match(_AUDIO_HISTORY_GLOB)]
                _check_size(sum(i.file_size for i in members))
                # extract() says where each file really went: the library drops "..", drive letters and leading
                # slashes from the names in the archive, so a path rebuilt from the name could point elsewhere.
                unpacked = {Path(zf.extract(member, work_dir)) for member in members}
        except zipfile.BadZipFile:
            raise ValueError("That file is not a valid ZIP archive.") from None
        return sorted(unpacked)
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


def preview(source: Path, work_dir: Path) -> ImportSummary:
    """Read an export and say what is in it, without touching the library."""
    json_files = _extract_json_files(source, work_dir)
    if not json_files:
        raise ValueError(_NO_FILES.format(source=source))
    return summarize(_load_records(json_files))


_NO_FILES = (
    "No Streaming_History_Audio_*.json files found in {source}. "
    "Make sure you requested 'Extended streaming history' from Spotify, not just 'Account data'."
)


def import_history(
    conn: sqlite3.Connection,
    source: Path,
    work_dir: Path,
    import_batch_id: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> ImportSummary:
    """Parse a Spotify Extended Streaming History export and insert music
    listens into play_history. Safe to re-run on the same or an overlapping
    export: duplicate (source, spotify_track_uri, played_at_epoch) rows are
    silently skipped via the table's own uniqueness constraint."""
    json_files = _extract_json_files(source, work_dir)
    if not json_files:
        raise ValueError(_NO_FILES.format(source=source))

    df = _load_records(json_files)
    summary = summarize(df)
    if df.empty:
        return summary

    batch_id = import_batch_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary.batch_id = batch_id

    music = df[_music_mask(df)]
    for done, (_, record) in enumerate(music.iterrows(), start=1):
        if on_progress and done % PROGRESS_EVERY == 0:
            on_progress(done, len(music))  # may raise to stop: nothing is committed until the end
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


_UNSENT = (
    "source = 'spotify_import' AND listenbrainz_submitted = 0 "
    "AND raw_artist_name IS NOT NULL AND raw_artist_name != '' AND raw_track_name IS NOT NULL AND raw_track_name != ''"
)


def unsent_count(conn: sqlite3.Connection) -> int:
    """Imported plays ListenBrainz has not been given yet."""
    return conn.execute(f"SELECT COUNT(*) FROM play_history WHERE {_UNSENT}").fetchone()[0]


def backfill_to_listenbrainz(
    conn: sqlite3.Connection,
    user_token: str,
    on_progress: Callable[[int, int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_refused: Callable[[sqlite3.Row], None] | None = None,
) -> int:
    """Submit not-yet-submitted spotify_import listens to the user's own
    ListenBrainz account, batched to the API's limit. Each batch is marked
    submitted immediately after it succeeds, so an interrupted run resumes
    from where it left off rather than re-submitting or losing progress.
    A busy or unreachable service is waited out a few times; a listen the
    service refuses is marked (2) and skipped instead of blocking the rest."""
    pending = conn.execute(f"SELECT * FROM play_history WHERE {_UNSENT} ORDER BY played_at_epoch, id").fetchall()
    batch_size = listenbrainz_client.MAX_LISTENS_PER_REQUEST
    submitted_count = 0

    def mark(rows: list[sqlite3.Row], value: int) -> None:
        conn.executemany("UPDATE play_history SET listenbrainz_submitted = ? WHERE id = ?", [(value, row["id"]) for row in rows])
        conn.commit()

    def refused(row: sqlite3.Row) -> None:
        mark([row], 2)
        if on_refused:
            on_refused(row)

    def sent(rows: list[sqlite3.Row]) -> None:
        nonlocal submitted_count
        mark(rows, 1)
        submitted_count += len(rows)

    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        with_patience(
            lambda batch=batch: submit_with_isolation(listenbrainz_client, user_token, batch, _build_listen_payload, sent, refused),
            sleep=sleep,
        )
        if on_progress:
            on_progress(min(start + len(batch), len(pending)), len(pending))

    return submitted_count
