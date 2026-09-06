from pathlib import Path

import pytest

from musictoolkit.db.connection import connect
from musictoolkit.sync import selector


def _insert_track(conn, file_path: str, **overrides) -> int:
    fields = {"genre": None, "rating": None, "is_missing": 0}
    fields.update(overrides)
    columns = ", ".join(["file_path"] + list(fields.keys()))
    placeholders = ", ".join(["?"] * (len(fields) + 1))
    conn.execute(f"INSERT INTO tracks ({columns}) VALUES ({placeholders})", [file_path] + list(fields.values()))
    conn.commit()
    return conn.execute("SELECT id FROM tracks WHERE file_path = ?", (file_path,)).fetchone()["id"]


def test_requires_exactly_one_mode(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    with pytest.raises(ValueError):
        selector.resolve_selection(conn)
    with pytest.raises(ValueError):
        selector.resolve_selection(conn, playlist_name="X", select_all=True)
    conn.close()


def test_select_all(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    _insert_track(conn, "/a.mp3")
    _insert_track(conn, "/b.mp3")
    rows = selector.resolve_selection(conn, select_all=True)
    assert len(rows) == 2
    conn.close()


def test_select_by_min_rating(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    _insert_track(conn, "/a.mp3", rating=5)
    _insert_track(conn, "/b.mp3", rating=2)
    rows = selector.resolve_selection(conn, min_rating=4)
    assert len(rows) == 1
    assert rows[0]["file_path"] == "/a.mp3"
    conn.close()


def test_select_by_tag(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    _insert_track(conn, "/a.mp3", genre="Rock")
    _insert_track(conn, "/b.mp3", genre="Jazz")
    rows = selector.resolve_selection(conn, tag_filter="Rock")
    assert len(rows) == 1
    assert rows[0]["file_path"] == "/a.mp3"
    conn.close()


def test_select_by_playlist_respects_position_order(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    id_a = _insert_track(conn, "/a.mp3")
    id_b = _insert_track(conn, "/b.mp3")
    conn.execute("INSERT INTO playlists (name, source) VALUES ('Road Trip', 'manual')")
    playlist_id = conn.execute("SELECT id FROM playlists WHERE name = 'Road Trip'").fetchone()["id"]
    conn.execute("INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, 1)", (playlist_id, id_b))
    conn.execute("INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, 0)", (playlist_id, id_a))
    conn.commit()

    rows = selector.resolve_selection(conn, playlist_name="Road Trip")
    assert [r["file_path"] for r in rows] == ["/a.mp3", "/b.mp3"]
    conn.close()


def test_missing_tracks_excluded_from_every_mode(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    _insert_track(conn, "/gone.mp3", rating=5, is_missing=1)
    rows = selector.resolve_selection(conn, min_rating=1)
    assert rows == []
    conn.close()
