from pathlib import Path

from musictoolkit.db.connection import connect
from musictoolkit.ingest import dedupe


def _insert_track(conn, file_path: str, **overrides) -> int:
    fields = {
        "artist": None, "title": None, "duration_seconds": None,
        "musicbrainz_recording_id": None, "bitrate": None, "is_missing": 0,
    }
    fields.update(overrides)
    columns = ", ".join(["file_path"] + list(fields.keys()))
    placeholders = ", ".join(["?"] * (len(fields) + 1))
    conn.execute(f"INSERT INTO tracks ({columns}) VALUES ({placeholders})", [file_path] + list(fields.values()))
    conn.commit()
    return conn.execute("SELECT id FROM tracks WHERE file_path = ?", (file_path,)).fetchone()["id"]


def test_finds_duplicates_by_musicbrainz_id(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    for name in ("a.mp3", "b.mp3"):
        (library / name).write_bytes(b"x")
        _insert_track(conn, str((library / name).resolve()), musicbrainz_recording_id="mb-1", bitrate=128)

    groups = dedupe.find_duplicate_groups(conn, library)
    assert len(groups) == 1
    assert groups[0].reason == "musicbrainz_recording_id"
    assert len(groups[0].track_ids) == 2
    conn.close()


def test_finds_duplicates_by_normalized_artist_title_duration(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    (library / "a.mp3").write_bytes(b"x")
    (library / "b.mp3").write_bytes(b"y")
    _insert_track(
        conn, str((library / "a.mp3").resolve()),
        artist="The Band", title="A Song!", duration_seconds=180.0, bitrate=128,
    )
    _insert_track(
        conn, str((library / "b.mp3").resolve()),
        artist="the band", title="a song", duration_seconds=180.9, bitrate=320,
    )

    groups = dedupe.find_duplicate_groups(conn, library)
    assert len(groups) == 1
    assert groups[0].reason == "normalized_artist_title_duration"
    conn.close()


def test_no_false_positive_for_distinct_tracks(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    (library / "a.mp3").write_bytes(b"x")
    (library / "b.mp3").write_bytes(b"y")
    _insert_track(conn, str((library / "a.mp3").resolve()), artist="Artist One", title="Song One", duration_seconds=180, bitrate=128)
    _insert_track(conn, str((library / "b.mp3").resolve()), artist="Artist Two", title="Song Two", duration_seconds=200, bitrate=128)

    groups = dedupe.find_duplicate_groups(conn, library)
    assert len(groups) == 0
    conn.close()


def test_apply_quarantine_keeps_highest_bitrate(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    low_path = library / "low.mp3"
    high_path = library / "high.mp3"
    low_path.write_bytes(b"x")
    high_path.write_bytes(b"y")
    low_id = _insert_track(conn, str(low_path.resolve()), musicbrainz_recording_id="mb-1", bitrate=128)
    high_id = _insert_track(conn, str(high_path.resolve()), musicbrainz_recording_id="mb-1", bitrate=320)

    groups = dedupe.find_duplicate_groups(conn, library)
    moved = dedupe.apply_quarantine(conn, groups, library)

    assert moved == 1
    assert high_path.exists()
    assert not low_path.exists()

    quarantined = list((library / "_duplicates_review").iterdir())
    assert len(quarantined) == 1

    low_row = conn.execute("SELECT file_path FROM tracks WHERE id = ?", (low_id,)).fetchone()
    assert "_duplicates_review" in low_row["file_path"]
    high_row = conn.execute("SELECT file_path FROM tracks WHERE id = ?", (high_id,)).fetchone()
    assert high_row["file_path"] == str(high_path.resolve())
    conn.close()
