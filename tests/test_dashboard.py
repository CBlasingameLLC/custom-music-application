from pathlib import Path

from fastapi.testclient import TestClient

from musictoolkit.dashboard import app as dashboard_module
from musictoolkit.db.connection import connect


def _client(db_path: Path) -> TestClient:
    dashboard_module.configure(db_path)
    return TestClient(dashboard_module.app)


def test_home_shows_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    conn.execute("INSERT INTO tracks (file_path, is_missing) VALUES ('/a.mp3', 0)")
    conn.execute(
        "INSERT INTO recommendations (artist_name, track_name, source, score, status) "
        "VALUES ('Artist', 'Song', 'listenbrainz_cf', 0.9, 'new')"
    )
    conn.commit()
    conn.close()

    response = _client(db_path).get("/")
    assert response.status_code == 200
    assert "1 tracks in library" in response.text
    assert "1 recommendations awaiting review" in response.text


def test_library_search_filters_results(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO tracks (file_path, artist, title, is_missing) VALUES ('/a.mp3', 'Radiohead', 'Karma Police', 0)"
    )
    conn.execute(
        "INSERT INTO tracks (file_path, artist, title, is_missing) VALUES ('/b.mp3', 'Other Band', 'Other Song', 0)"
    )
    conn.commit()
    conn.close()

    client = _client(db_path)
    all_results = client.get("/library")
    assert "Radiohead" in all_results.text
    assert "Other Band" in all_results.text

    filtered = client.get("/library", params={"q": "Radiohead"})
    assert "Radiohead" in filtered.text
    assert "Other Band" not in filtered.text


def test_library_escapes_html_in_track_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO tracks (file_path, artist, title, is_missing) VALUES ('/a.mp3', ?, 'Title', 0)",
        ("<script>alert(1)</script>",),
    )
    conn.commit()
    conn.close()

    response = _client(db_path).get("/library")
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_recommendations_page_lists_pending_only(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO recommendations (artist_name, track_name, source, score, status) "
        "VALUES ('New Artist', 'New Song', 'listenbrainz_cf', 0.8, 'new')"
    )
    conn.execute(
        "INSERT INTO recommendations (artist_name, track_name, source, score, status) "
        "VALUES ('Old Artist', 'Old Song', 'listenbrainz_cf', 0.5, 'accepted')"
    )
    conn.commit()
    conn.close()

    response = _client(db_path).get("/recommendations")
    assert "New Artist" in response.text
    assert "Old Artist" not in response.text


def test_recommendation_status_update_redirects_and_persists(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO recommendations (artist_name, track_name, source, score, status) "
        "VALUES ('Some Artist', 'Some Song', 'listenbrainz_cf', 0.8, 'new')"
    )
    conn.commit()
    rec_id = conn.execute("SELECT id FROM recommendations").fetchone()["id"]
    conn.close()

    client = _client(db_path)
    response = client.post(f"/recommendations/{rec_id}/status", data={"status": "accepted"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/recommendations"

    conn2 = connect(db_path)
    row = conn2.execute("SELECT status FROM recommendations WHERE id = ?", (rec_id,)).fetchone()
    assert row["status"] == "accepted"
    conn2.close()


def test_devices_page_shows_sync_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    conn.execute("INSERT INTO tracks (file_path, is_missing) VALUES ('/a.mp3', 0)")
    conn.execute("INSERT INTO devices (label, last_seen_mount_path, created_at) VALUES ('My DAP', '/media/dap', '2024-01-01')")
    device_id = conn.execute("SELECT id FROM devices").fetchone()["id"]
    track_id = conn.execute("SELECT id FROM tracks").fetchone()["id"]
    conn.execute(
        "INSERT INTO sync_manifest (device_id, track_id, dest_relative_path, status) VALUES (?, ?, 'a.mp3', 'synced')",
        (device_id, track_id),
    )
    conn.commit()
    conn.close()

    response = _client(db_path).get("/devices")
    assert "My DAP" in response.text
    assert "/media/dap" in response.text


def test_history_page_empty_state(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    connect(db_path).close()
    response = _client(db_path).get("/history")
    assert "No play history imported yet" in response.text


def test_history_page_shows_top_artists(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    for _ in range(3):
        conn.execute(
            "INSERT INTO play_history (source, played_at_epoch, raw_artist_name) VALUES ('spotify_import', 1, 'Popular Artist')"
        )
    conn.commit()
    conn.close()

    response = _client(db_path).get("/history")
    assert "Popular Artist (3)" in response.text
