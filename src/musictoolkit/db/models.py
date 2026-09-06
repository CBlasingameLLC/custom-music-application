from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class Track:
    id: int | None
    file_path: str
    file_hash: str | None = None
    file_size: int | None = None
    file_mtime: float | None = None
    duration_seconds: float | None = None
    title: str | None = None
    artist: str | None = None
    album_artist: str | None = None
    album: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    year: int | None = None
    genre: str | None = None
    musicbrainz_recording_id: str | None = None
    musicbrainz_release_id: str | None = None
    musicbrainz_artist_id: str | None = None
    mb_match_confidence: float | None = None
    tag_source: str | None = None
    rating: int | None = None
    format: str | None = None
    bitrate: int | None = None
    date_added: str | None = None
    date_last_scanned: str | None = None
    is_missing: bool = False

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Track:
        data = dict(row)
        data["is_missing"] = bool(data.get("is_missing", 0))
        return cls(**data)


@dataclass
class PlayHistoryEvent:
    id: int | None
    track_id: int | None
    source: str
    played_at_epoch: int
    ms_played: int | None = None
    raw_artist_name: str | None = None
    raw_track_name: str | None = None
    raw_album_name: str | None = None
    spotify_track_uri: str | None = None
    listenbrainz_submitted: bool = False
    import_batch_id: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PlayHistoryEvent:
        data = dict(row)
        data["listenbrainz_submitted"] = bool(data.get("listenbrainz_submitted", 0))
        return cls(**data)


@dataclass
class Recommendation:
    id: int | None
    artist_name: str
    track_name: str | None
    musicbrainz_artist_id: str | None = None
    musicbrainz_recording_id: str | None = None
    source: str = ""
    score: float | None = None
    reason: str | None = None
    status: str = "new"
    date_suggested: str | None = None
    date_reviewed: str | None = None
    notes: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Recommendation:
        return cls(**dict(row))


@dataclass
class Device:
    id: int | None
    label: str | None
    last_seen_mount_path: str | None = None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Device:
        return cls(**dict(row))


@dataclass
class SyncManifestEntry:
    id: int | None
    device_id: int
    track_id: int
    dest_relative_path: str | None = None
    source_file_hash: str | None = None
    source_file_mtime: float | None = None
    synced_at: str | None = None
    status: str = "synced"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> SyncManifestEntry:
        return cls(**dict(row))


@dataclass
class Playlist:
    id: int | None
    name: str | None
    source: str | None = None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Playlist:
        return cls(**dict(row))


@dataclass
class PlaylistTrack:
    playlist_id: int
    track_id: int
    position: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PlaylistTrack:
        return cls(**dict(row))
