from pathlib import Path

import sqlite3

from musictoolkit.db.connection import _migration_files, connect

LATEST_VERSION = max(version for version, _name, _sql in _migration_files())

EXPECTED_TABLES = {
    "tracks",
    "devices",
    "sync_manifest",
    "playlists",
    "playlist_tracks",
    "play_history",
    "recommendations",
    "kv",
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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
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
        assert conn2.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
        row = conn2.execute(
            "SELECT * FROM tracks WHERE file_path = ?", ("/music/a.mp3",)
        ).fetchone()
        assert row["title"] == "Test Title"
    finally:
        conn2.close()


def test_upgrade_from_v3_keeps_existing_data_and_fills_defaults(tmp_path: Path) -> None:
    """The first desktop release shipped schema v3. A library scanned with it
    must survive the upgrade, with the new columns defaulted sensibly."""
    db_path = tmp_path / "old.db"
    old = sqlite3.connect(db_path)
    for version, _name, sql in _migration_files():
        if version <= 3:
            old.executescript(sql)
            old.execute(f"PRAGMA user_version = {version}")
    old.execute("INSERT INTO tracks (file_path, title, artist, rating) VALUES ('/m/a.mp3', 'Song', 'Band', 4)")
    old.execute("INSERT INTO playlists (name, source, created_at) VALUES ('Old list', 'm3u_import', '2024-01-01')")
    old.execute("INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (1, 1, 0)")
    old.commit()
    old.close()

    conn = connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
        track = conn.execute("SELECT * FROM tracks").fetchone()
        assert (track["title"], track["artist"], track["rating"]) == ("Song", "Band", 4)
        assert track["favorite"] == 0
        playlist = conn.execute("SELECT * FROM playlists").fetchone()
        assert (playlist["name"], playlist["kind"], playlist["rules_json"]) == ("Old list", "manual", None)
        assert conn.execute("SELECT COUNT(*) AS c FROM playlist_tracks").fetchone()["c"] == 1
    finally:
        conn.close()


def test_connection_uses_wal_so_the_gui_can_read_during_a_scan(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test_library.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_track_uniqueness_on_file_path(tmp_path: Path) -> None:
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
