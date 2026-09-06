from pathlib import Path

from musictoolkit.db.connection import connect
from musictoolkit.ingest import organizer

SCHEME = "{album_artist}/{album}/{track:02d} - {title}.{ext}"


def _insert_track(conn, file_path: str, **overrides) -> int:
    fields = {
        "album_artist": None, "artist": None, "album": None, "title": None,
        "track_number": None, "year": None, "is_missing": 0,
    }
    fields.update(overrides)
    columns = ", ".join(["file_path"] + list(fields.keys()))
    placeholders = ", ".join(["?"] * (len(fields) + 1))
    conn.execute(f"INSERT INTO tracks ({columns}) VALUES ({placeholders})", [file_path] + list(fields.values()))
    conn.commit()
    return conn.execute("SELECT id FROM tracks WHERE file_path = ?", (file_path,)).fetchone()["id"]


def test_sanitize_path_component_strips_invalid_chars() -> None:
    assert organizer.sanitize_path_component('AC/DC: Back in Black?') == "AC_DC_ Back in Black_"


def test_compute_target_path_uses_scheme(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    old_path = library / "raw.mp3"
    old_path.write_bytes(b"x")
    track_id = _insert_track(
        conn, str(old_path.resolve()), artist="The Artist", album="The Album", title="The Title", track_number=5
    )
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()

    target = organizer.compute_target_path(row, library, SCHEME)
    assert target == (library / "The Artist" / "The Album" / "05 - The Title.mp3").resolve()
    conn.close()


def test_propose_organization_dry_run_does_not_move(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    old_path = library / "raw.mp3"
    old_path.write_bytes(b"x")
    _insert_track(conn, str(old_path.resolve()), artist="Artist", album="Album", title="Title", track_number=1)

    result = organizer.propose_organization(conn, library, SCHEME)

    assert len(result.proposals) == 1
    assert old_path.exists()
    conn.close()


def test_apply_organization_moves_file_and_updates_db(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    old_path = library / "raw.mp3"
    old_path.write_bytes(b"x")
    track_id = _insert_track(
        conn, str(old_path.resolve()), artist="Artist", album="Album", title="Title", track_number=1
    )

    result = organizer.propose_organization(conn, library, SCHEME)
    moved = organizer.apply_organization(conn, result.proposals)

    assert moved == 1
    assert not old_path.exists()
    new_path = library / "Artist" / "Album" / "01 - Title.mp3"
    assert new_path.exists()

    row = conn.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,)).fetchone()
    assert row["file_path"] == str(new_path.resolve())
    conn.close()


def test_propose_organization_flags_collisions(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    path_a = library / "a.mp3"
    path_b = library / "b.mp3"
    path_a.write_bytes(b"x")
    path_b.write_bytes(b"y")
    _insert_track(conn, str(path_a.resolve()), artist="Artist", album="Album", title="Same", track_number=1)
    _insert_track(conn, str(path_b.resolve()), artist="Artist", album="Album", title="Same", track_number=1)

    result = organizer.propose_organization(conn, library, SCHEME)

    # One of the two can legitimately claim the shared target; the other is
    # correctly flagged rather than silently overwriting it.
    assert len(result.proposals) == 1
    assert len(result.collisions) == 1
    conn.close()
