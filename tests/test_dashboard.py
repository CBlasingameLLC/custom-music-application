import html
import logging
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from musictoolkit.dashboard import app as dashboard_module
from musictoolkit.db.connection import connect
from musictoolkit.ingest import scanner

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _fresh_scan_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan state is module-level; give every test a clean one."""
    monkeypatch.setattr(dashboard_module, "_scan_state", dashboard_module._ScanState())
    monkeypatch.setattr(dashboard_module, "_scan_thread", None)


def _client(db_path: Path) -> TestClient:
    dashboard_module.configure(db_path)
    return TestClient(dashboard_module.app)


def _wait_for_scan() -> None:
    thread = dashboard_module._scan_thread
    assert thread is not None, "no scan was started"
    thread.join(timeout=30)
    assert not thread.is_alive(), "scan did not finish"


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


def test_first_run_with_no_database_yet_serves_every_page(tmp_path: Path) -> None:
    """Regression: a freshly installed desktop app starts the dashboard with no
    data directory or database at all. Every page used to answer 500."""
    db_path = tmp_path / "not" / "created" / "yet" / "library.db"
    assert not db_path.parent.exists()

    client = _client(db_path)

    assert db_path.exists()
    for page in ("/", "/library", "/scan", "/recommendations", "/devices", "/history"):
        assert client.get(page).status_code == 200, page
    home = client.get("/").text
    assert "0 tracks in library" in home
    assert "Your library is empty" in home


def test_home_hides_empty_hint_once_library_has_tracks(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    conn.execute("INSERT INTO tracks (file_path, is_missing) VALUES ('/a.mp3', 0)")
    conn.commit()
    conn.close()

    assert "Your library is empty" not in _client(db_path).get("/").text


def test_unhandled_error_returns_readable_page_and_logs_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    log_dir = tmp_path / "logs"
    dashboard_module.configure(tmp_path / "test.db", log_dir=log_dir)

    def boom() -> None:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(dashboard_module, "_connection", boom)
    client = TestClient(dashboard_module.app, raise_server_exceptions=False)

    with caplog.at_level(logging.ERROR, logger="musictoolkit"):
        response = client.get("/")

    assert response.status_code == 500
    assert "RuntimeError: simulated failure" in response.text
    assert html.escape(str(log_dir / "musictoolkit.log")) in response.text
    assert any(r.exc_info and "simulated failure" in str(r.exc_info[1]) for r in caplog.records)


def test_scan_form_scans_folder_and_shows_result(tmp_path: Path) -> None:
    client = _client(tmp_path / "test.db")

    response = client.post("/scan", data={"path": str(FIXTURES_DIR)}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/scan"
    _wait_for_scan()

    page = client.get("/scan").text
    assert "Scan finished" in page
    assert "2 added" in page
    assert "2 tracks in library" in client.get("/").text
    assert "Fixture Artist" in client.get("/library").text


def test_scan_accepts_a_path_pasted_with_quotes(tmp_path: Path) -> None:
    """Explorer's "Copy as path" wraps the path in double quotes."""
    client = _client(tmp_path / "test.db")

    response = client.post("/scan", data={"path": f'  "{FIXTURES_DIR}"  '}, follow_redirects=False)

    assert response.status_code == 303
    _wait_for_scan()
    assert "2 added" in client.get("/scan").text


def test_scan_rejects_a_folder_that_does_not_exist(tmp_path: Path) -> None:
    client = _client(tmp_path / "test.db")

    response = client.post("/scan", data={"path": str(tmp_path / "nope")})

    assert response.status_code == 400
    assert "Folder not found" in response.text
    assert dashboard_module._scan_thread is None
    assert dashboard_module._scan_state.running is False


def test_scan_shows_progress_and_refuses_a_second_scan_while_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path / "test.db")
    started = threading.Event()
    release = threading.Event()

    def slow_scan(conn, root, on_progress=None):
        on_progress(1, 4)
        started.set()
        release.wait(timeout=30)
        return scanner.ScanResult(added=4)

    monkeypatch.setattr(dashboard_module.scanner, "scan_library", slow_scan)

    assert client.post("/scan", data={"path": str(FIXTURES_DIR)}, follow_redirects=False).status_code == 303
    assert started.wait(timeout=30)
    try:
        page = client.get("/scan").text
        assert "1 of 4 files (25%)" in page
        assert 'http-equiv="refresh"' in page
        assert dashboard_module._start_scan(FIXTURES_DIR) is False
    finally:
        release.set()
    _wait_for_scan()

    page = client.get("/scan").text
    assert "4 added" in page
    assert 'http-equiv="refresh"' not in page


def test_scan_failure_is_reported_on_the_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path / "test.db")

    def broken_scan(conn, root, on_progress=None):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(dashboard_module.scanner, "scan_library", broken_scan)

    client.post("/scan", data={"path": str(FIXTURES_DIR)}, follow_redirects=False)
    _wait_for_scan()

    page = client.get("/scan").text
    assert "The last scan failed" in page
    assert "disk on fire" in page
    assert dashboard_module._scan_state.running is False
