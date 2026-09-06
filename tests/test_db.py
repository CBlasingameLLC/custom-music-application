from pathlib import Path

from musictoolkit.db.connection import connect

EXPECTED_TABLES = {
    "tracks",
    "devices",
    "sync_manifest",
    "playlists",
    "playlist_tracks",
    "play_history",
    "recommendations",
}


def test_migrations_apply_all_tables(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test_library.db")
    try:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert EXPECTED_TABLES.issubset(tables)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        conn.close()


def test_reconnect_does_not_reapply_migrations(tmp_path: Path) -> None:
    db_path = tmp_path / "test_library.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO tracks (file_path, title, artist) VALUES (?, ?, ?)",
        ("/music/a.mp3", "Test Title", "Test Artist"),
    )
    conn.commit()
    conn.close()

    conn2 = connect(db_path)
    try:
        assert conn2.execute("PRAGMA user_version").fetchone()[0] == 3
        row = conn2.execute(
            "SELECT * FROM tracks WHERE file_path = ?", ("/music/a.mp3",)
        ).fetchone()
        assert row["title"] == "Test Title"
    finally:
        conn2.close()


def test_track_uniqueness_on_file_path(tmp_path: Path) -> None:
    import sqlite3

    conn = connect(tmp_path / "test_library.db")
    try:
        conn.execute("INSERT INTO tracks (file_path) VALUES ('/music/a.mp3')")
        conn.commit()
        try:
            conn.execute("INSERT INTO tracks (file_path) VALUES ('/music/a.mp3')")
            conn.commit()
            assert False, "expected UNIQUE constraint violation"
        except sqlite3.IntegrityError:
            pass
    finally:
        conn.close()
