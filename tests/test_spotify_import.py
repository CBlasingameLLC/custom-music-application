import json
import zipfile
from pathlib import Path

import pytest

from musictoolkit.db.connection import connect
from musictoolkit.history import spotify_import

SAMPLE_RECORDS = [
    {
        "ts": "2024-01-15T10:00:00Z",
        "platform": "android",
        "ms_played": 210000,
        "master_metadata_track_name": "Song One",
        "master_metadata_album_artist_name": "Artist A",
        "master_metadata_album_album_name": "Album A",
        "spotify_track_uri": "spotify:track:aaa111",
        "episode_name": None,
        "spotify_episode_uri": None,
    },
    {
        "ts": "2024-01-16T11:30:00Z",
        "platform": "web_player",
        "ms_played": 180000,
        "master_metadata_track_name": "Song Two",
        "master_metadata_album_artist_name": "Artist A",
        "master_metadata_album_album_name": "Album A",
        "spotify_track_uri": "spotify:track:bbb222",
        "episode_name": None,
        "spotify_episode_uri": None,
    },
    {
        # A podcast episode — no spotify_track_uri, must be excluded from music stats/import.
        "ts": "2024-01-17T09:00:00Z",
        "platform": "android",
        "ms_played": 900000,
        "master_metadata_track_name": None,
        "master_metadata_album_artist_name": None,
        "master_metadata_album_album_name": None,
        "spotify_track_uri": None,
        "episode_name": "Some Podcast Episode",
        "spotify_episode_uri": "spotify:episode:ccc333",
    },
]


def _write_export_folder(base: Path, records: list[dict]) -> Path:
    export_dir = base / "export"
    export_dir.mkdir()
    (export_dir / "Streaming_History_Audio_2024_0.json").write_text(json.dumps(records))
    return export_dir


def _write_export_zip(base: Path, records: list[dict]) -> Path:
    zip_path = base / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Spotify Extended Streaming History/Streaming_History_Audio_2024_0.json", json.dumps(records))
    return zip_path


def test_import_from_folder_skips_podcast_rows_and_summarizes(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    export_dir = _write_export_folder(tmp_path, SAMPLE_RECORDS)

    summary = spotify_import.import_history(conn, export_dir, tmp_path / "work")

    assert summary.total_rows_seen == 3
    assert summary.music_rows == 2
    assert summary.podcast_rows_skipped == 1
    assert summary.inserted == 2
    assert summary.date_range is not None
    assert ("Artist A", 2) in summary.top_artists

    rows = conn.execute("SELECT * FROM play_history ORDER BY played_at_epoch").fetchall()
    assert len(rows) == 2
    assert rows[0]["raw_track_name"] == "Song One"
    assert rows[0]["source"] == "spotify_import"
    conn.close()


def test_import_from_zip_extracts_nested_json(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    zip_path = _write_export_zip(tmp_path, SAMPLE_RECORDS)

    summary = spotify_import.import_history(conn, zip_path, tmp_path / "work")

    assert summary.inserted == 2
    conn.close()


def test_reimporting_same_export_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    export_dir = _write_export_folder(tmp_path, SAMPLE_RECORDS)

    spotify_import.import_history(conn, export_dir, tmp_path / "work1")
    summary2 = spotify_import.import_history(conn, export_dir, tmp_path / "work2")

    assert summary2.inserted == 0
    assert summary2.duplicates_skipped == 2
    total = conn.execute("SELECT COUNT(*) AS c FROM play_history").fetchone()["c"]
    assert total == 2
    conn.close()


def test_missing_export_files_raises_helpful_error(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(ValueError, match="Extended streaming history"):
        spotify_import.import_history(conn, empty_dir, tmp_path / "work")
    conn.close()


def test_cross_references_to_owned_library(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO tracks (file_path, artist, title, is_missing) VALUES (?, ?, ?, 0)",
        ("/library/song_one.mp3", "Artist A", "Song One"),
    )
    conn.commit()
    export_dir = _write_export_folder(tmp_path, SAMPLE_RECORDS)

    summary = spotify_import.import_history(conn, export_dir, tmp_path / "work")

    assert summary.matched_to_library == 1
    matched_row = conn.execute(
        "SELECT * FROM play_history WHERE raw_track_name = 'Song One'"
    ).fetchone()
    assert matched_row["track_id"] is not None
    unmatched_row = conn.execute(
        "SELECT * FROM play_history WHERE raw_track_name = 'Song Two'"
    ).fetchone()
    assert unmatched_row["track_id"] is None
    conn.close()


def test_backfill_to_listenbrainz_submits_and_marks_resumable(tmp_path: Path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    export_dir = _write_export_folder(tmp_path, SAMPLE_RECORDS)
    spotify_import.import_history(conn, export_dir, tmp_path / "work")

    submitted_batches = []

    def fake_submit_listens(user_token, listens):
        assert user_token == "fake-token"
        submitted_batches.append(listens)

    monkeypatch.setattr(spotify_import.listenbrainz_client, "submit_listens", fake_submit_listens)

    submitted = spotify_import.backfill_to_listenbrainz(conn, "fake-token")

    assert submitted == 2
    assert len(submitted_batches) == 1
    assert submitted_batches[0][0]["track_metadata"]["artist_name"] == "Artist A"

    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM play_history WHERE listenbrainz_submitted = 0"
    ).fetchone()["c"]
    assert pending == 0

    # Re-running must not re-submit already-submitted rows.
    monkeypatch.setattr(
        spotify_import.listenbrainz_client,
        "submit_listens",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called again")),
    )
    submitted_again = spotify_import.backfill_to_listenbrainz(conn, "fake-token")
    assert submitted_again == 0
    conn.close()
