"""Songs opened from outside the library folders (Open with in Explorer, files dropped on the window) are added one by
one; later scans must neither lose them nor duplicate them."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from musictoolkit.db.connection import connect
from musictoolkit.ingest import scanner
from tests.conftest import make_track


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "library.db")
    yield connection
    connection.close()


@pytest.fixture
def elsewhere(tmp_path: Path) -> Path:
    return tmp_path / "elsewhere"


def outcome(results, name: str) -> tuple[int | None, str]:
    (found,) = [(track_id, what) for given, track_id, what in results if Path(given).name == name]
    return found


def test_a_new_song_is_added_with_its_tags_and_a_second_look_finds_it_unchanged(conn, elsewhere) -> None:
    song = make_track(elsewhere, "one.mp3", title="One", artist="Someone", album="Somewhere", tracknumber=3)

    first = scanner.add_files(conn, [str(song)])
    second = scanner.add_files(conn, [str(song)])

    track_id, what = outcome(first, "one.mp3")
    assert what == "added" and track_id is not None
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    assert (row["title"], row["artist"], row["album"], row["track_number"], row["is_missing"]) == ("One", "Someone", "Somewhere", 3, 0)
    assert second == [(str(song), track_id, "known")]
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1


def test_a_song_changed_since_it_was_read_is_read_again_under_the_same_id(conn, elsewhere) -> None:
    song = make_track(elsewhere, "one.mp3", title="Before", artist="A")
    (track_id,) = [t for _, t, _ in scanner.add_files(conn, [str(song)])]

    make_track(elsewhere, "one.mp3", title="After", artist="A")
    os.utime(song, (song.stat().st_atime, song.stat().st_mtime + 10))
    again = scanner.add_files(conn, [str(song)])

    assert again == [(str(song), track_id, "updated")]
    assert conn.execute("SELECT title FROM tracks WHERE id = ?", (track_id,)).fetchone()["title"] == "After"


def test_a_song_marked_missing_comes_back_when_it_is_opened_again(conn, elsewhere) -> None:
    song = make_track(elsewhere, "one.mp3", title="One")
    (track_id,) = [t for _, t, _ in scanner.add_files(conn, [str(song)])]
    conn.execute("UPDATE tracks SET is_missing = 1 WHERE id = ?", (track_id,))
    conn.commit()

    again = scanner.add_files(conn, [str(song)])

    assert again == [(str(song), track_id, "updated")]
    assert conn.execute("SELECT is_missing FROM tracks WHERE id = ?", (track_id,)).fetchone()["is_missing"] == 0


def test_what_cannot_be_used_is_named_and_the_rest_still_goes_in(conn, elsewhere) -> None:
    song = make_track(elsewhere, "one.mp3", title="One")
    notes = elsewhere / "notes.txt"
    notes.write_text("not music")

    results = scanner.add_files(conn, [str(notes), str(elsewhere / "gone.mp3"), "relative/path.mp3", str(song)])

    assert [what for _, _, what in results] == ["not_audio", "missing", "unreadable", "added"]
    assert [track_id is None for _, track_id, _ in results] == [True, True, True, False]
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1


def test_a_folder_stands_for_the_songs_inside_it_and_a_huge_one_is_cut_off(conn, elsewhere, monkeypatch) -> None:
    for n in range(5):
        make_track(elsewhere / "album", f"{n}.mp3", title=f"Song {n}")
    (elsewhere / "album" / "cover.jpg").write_bytes(b"x")

    results = scanner.add_files(conn, [str(elsewhere)])
    assert sorted(what for _, _, what in results) == ["added"] * 5

    monkeypatch.setattr(scanner, "MAX_OPENED", 2)
    capped = scanner.add_files(conn, [str(elsewhere)])
    assert len(capped) == 2, "no more than the cap are taken from a folder"


def test_later_scans_leave_a_song_outside_their_folders_alone_and_adopt_it_when_its_folder_is_added(conn, elsewhere, tmp_path) -> None:
    song = make_track(elsewhere, "one.mp3", title="One")
    (track_id,) = [t for _, t, _ in scanner.add_files(conn, [str(song)])]
    other = tmp_path / "music"
    make_track(other, "two.mp3", title="Two")

    scanner.scan_library(conn, other)

    assert conn.execute("SELECT is_missing FROM tracks WHERE id = ?", (track_id,)).fetchone()["is_missing"] == 0
    song.unlink()
    scanner.scan_library(conn, elsewhere)  # now its own folder is scanned, and the song is gone from it
    assert conn.execute("SELECT is_missing FROM tracks WHERE id = ?", (track_id,)).fetchone()["is_missing"] == 1


def test_where_file_names_ignore_case_one_file_is_never_two_songs(conn, elsewhere, monkeypatch) -> None:
    song = make_track(elsewhere, "one.mp3", title="One")
    shouted = str(song).upper()  # how another program might have spelt the path that is already stored
    conn.execute("INSERT INTO tracks (file_path, title, file_size, file_mtime, is_missing) VALUES (?, 'One', 1, 1, 0)", (shouted,))
    conn.commit()

    monkeypatch.setattr(scanner, "CASE_INSENSITIVE_PATHS", False)
    scanner.add_files(conn, [str(song)])
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2, "on a case-sensitive system these are two files"

    conn.execute("DELETE FROM tracks WHERE file_path = ?", (str(song),))
    conn.commit()
    monkeypatch.setattr(scanner, "CASE_INSENSITIVE_PATHS", True)
    scanner.add_files(conn, [str(song)])
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1, "where case is ignored the stored spelling is the song"


def test_a_scan_still_counts_added_updated_and_unchanged_as_before(conn, tmp_path) -> None:
    music = tmp_path / "music"
    make_track(music, "a.mp3", title="A")
    make_track(music, "b.mp3", title="B")

    first = scanner.scan_library(conn, music)
    second = scanner.scan_library(conn, music)
    b = music / "b.mp3"
    os.utime(b, (b.stat().st_atime, b.stat().st_mtime + 10))
    third = scanner.scan_library(conn, music)

    assert (first.added, first.updated, first.unchanged) == (2, 0, 0)
    assert (second.added, second.updated, second.unchanged) == (0, 0, 2)
    assert (third.added, third.updated, third.unchanged) == (0, 1, 1)
