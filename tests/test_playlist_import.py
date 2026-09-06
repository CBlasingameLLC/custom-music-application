from pathlib import Path

from musictoolkit.db.connection import connect
from musictoolkit.sync import playlist_import


def _insert_track(conn, file_path: str) -> int:
    conn.execute("INSERT INTO tracks (file_path, is_missing) VALUES (?, 0)", (file_path,))
    conn.commit()
    return conn.execute("SELECT id FROM tracks WHERE file_path = ?", (file_path,)).fetchone()["id"]


def test_parse_m3u_resolves_relative_entries(tmp_path: Path) -> None:
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    (music_dir / "song.mp3").write_bytes(b"x")

    m3u_path = tmp_path / "playlist.m3u"
    m3u_path.write_text("#EXTM3U\n#EXTINF:180,Some Artist - Some Song\nmusic/song.mp3\n/absolute/other.mp3\n")

    entries = playlist_import.parse_m3u(m3u_path)
    assert entries == [(music_dir / "song.mp3").resolve(), Path("/absolute/other.mp3")]


def test_import_playlist_matches_existing_tracks_and_skips_missing(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    (music_dir / "known.mp3").write_bytes(b"x")
    _insert_track(conn, str((music_dir / "known.mp3").resolve()))

    m3u_path = music_dir / "playlist.m3u"
    m3u_path.write_text("known.mp3\nnever_scanned.mp3\n")

    matched = playlist_import.import_playlist(conn, m3u_path, playlist_name="My Playlist")
    assert matched == 1

    playlist = conn.execute("SELECT * FROM playlists WHERE name = 'My Playlist'").fetchone()
    assert playlist is not None
    track_count = conn.execute(
        "SELECT COUNT(*) AS c FROM playlist_tracks WHERE playlist_id = ?", (playlist["id"],)
    ).fetchone()["c"]
    assert track_count == 1
    conn.close()


def test_import_playlist_defaults_name_to_filename(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    m3u_path = tmp_path / "Discover Weekly.m3u"
    m3u_path.write_text("")
    playlist_import.import_playlist(conn, m3u_path)
    row = conn.execute("SELECT name FROM playlists").fetchone()
    assert row["name"] == "Discover Weekly"
    conn.close()
