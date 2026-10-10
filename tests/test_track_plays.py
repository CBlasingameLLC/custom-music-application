"""The per-song play counts that every library screen reads: they must always equal what the history says."""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

import pytest

from musictoolkit.db.connection import _migration_files, connect


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "library.db")
    for n in range(1, 6):
        connection.execute("INSERT INTO tracks (id, file_path, title) VALUES (?, ?, ?)", (n, f"/m/{n}.mp3", f"Song {n}"))
    connection.commit()
    yield connection
    connection.close()


def counted(conn) -> dict[int, tuple[int, int | None]]:
    return {r["track_id"]: (r["plays"], r["last_played"]) for r in conn.execute("SELECT * FROM track_plays")}


def recomputed(conn) -> dict[int, tuple[int, int | None]]:
    return {
        r["track_id"]: (r["plays"], r["last_played"])
        for r in conn.execute("SELECT track_id, COUNT(*) AS plays, MAX(played_at_epoch) AS last_played FROM play_history WHERE track_id IS NOT NULL GROUP BY track_id")
    }


def play(conn, track_id, at, source="future_scrobble", uri=None) -> int:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO play_history (track_id, source, played_at_epoch, spotify_track_uri) VALUES (?, ?, ?, ?)", (track_id, source, at, uri))
    conn.commit()
    return cursor.lastrowid if cursor.rowcount else 0


# ------------------------------------------------------------------------------------------- the triggers


def test_a_play_counts_and_remembers_when_the_song_was_last_played(conn) -> None:
    play(conn, 1, 100)
    play(conn, 1, 300)
    play(conn, 1, 200)  # an older play arriving later (an import) does not move "last played" back

    assert counted(conn) == {1: (3, 300)}


def test_a_play_with_no_song_is_not_counted(conn) -> None:
    play(conn, None, 100)

    assert counted(conn) == {}


def test_a_play_that_is_ignored_as_a_duplicate_is_not_counted(conn) -> None:
    play(conn, 1, 100, source="spotify_import", uri="spotify:track:x")
    assert play(conn, 1, 100, source="spotify_import", uri="spotify:track:x") == 0

    assert counted(conn) == {1: (1, 100)}


def test_removing_a_play_lowers_the_count_and_finds_the_new_last_played(conn) -> None:
    newest = play(conn, 1, 300)
    play(conn, 1, 100)
    play(conn, 1, 200)

    conn.execute("DELETE FROM play_history WHERE id = ?", (newest,))
    conn.commit()

    assert counted(conn) == {1: (2, 200)}


def test_a_song_with_no_plays_left_has_no_row(conn) -> None:
    only = play(conn, 1, 100)

    conn.execute("DELETE FROM play_history WHERE id = ?", (only,))
    conn.commit()

    assert counted(conn) == {}


def test_moving_a_play_to_another_song_moves_the_count(conn) -> None:
    moved = play(conn, 1, 500)
    play(conn, 1, 100)
    play(conn, 2, 200)

    conn.execute("UPDATE play_history SET track_id = 2 WHERE id = ?", (moved,))
    conn.commit()

    assert counted(conn) == {1: (1, 100), 2: (2, 500)}


def test_a_play_given_a_song_it_had_none_of_counts_for_it(conn) -> None:
    unmatched = play(conn, None, 700)

    conn.execute("UPDATE play_history SET track_id = 3 WHERE id = ?", (unmatched,))
    conn.commit()

    assert counted(conn) == {3: (1, 700)}


def test_a_play_whose_song_was_forgotten_stops_counting_for_it(conn) -> None:
    play(conn, 4, 100)
    forgotten = play(conn, 4, 200)

    conn.execute("UPDATE play_history SET track_id = NULL WHERE id = ?", (forgotten,))
    conn.commit()

    assert counted(conn) == {4: (1, 100)}


def test_changing_something_else_about_a_play_changes_no_count(conn) -> None:
    mine = play(conn, 1, 100)

    conn.execute("UPDATE play_history SET listenbrainz_submitted = 1, ms_played = 5 WHERE id = ?", (mine,))
    conn.execute("UPDATE play_history SET track_id = 1 WHERE id = ?", (mine,))  # to the song it already had
    conn.commit()

    assert counted(conn) == {1: (1, 100)}


def test_the_counts_stay_equal_to_the_history_through_any_run_of_changes(conn) -> None:
    rnd = random.Random(42)
    ids: list[int] = []
    for step in range(600):
        action = rnd.choice(["add", "add", "add", "delete", "move", "forget", "match"])
        if action == "add" or not ids:
            ids.append(play(conn, rnd.choice([None, 1, 2, 3, 4, 5]), rnd.randrange(1, 10_000)))
        elif action == "delete":
            conn.execute("DELETE FROM play_history WHERE id = ?", (ids.pop(rnd.randrange(len(ids))),))
        elif action == "move":
            conn.execute("UPDATE play_history SET track_id = ? WHERE id = ?", (rnd.choice([1, 2, 3, 4, 5]), rnd.choice(ids)))
        elif action == "forget":
            conn.execute("UPDATE play_history SET track_id = NULL WHERE track_id = ?", (rnd.choice([1, 2, 3, 4, 5]),))
        else:
            conn.execute("UPDATE play_history SET track_id = ? WHERE track_id IS NULL AND id = ?", (rnd.choice([1, 2, 3]), rnd.choice(ids)))
        conn.commit()
        if step % 25 == 0:
            assert counted(conn) == recomputed(conn), f"differ after {step} changes"
    assert counted(conn) == recomputed(conn)


def test_deleting_many_plays_at_once_keeps_the_counts_right(conn) -> None:
    for n in range(50):
        play(conn, 1 + n % 3, 1000 + n, source="spotify_import", uri=f"u{n}")
    conn.execute("UPDATE play_history SET import_batch_id = 'b' WHERE source = 'spotify_import'")
    conn.commit()

    conn.execute("DELETE FROM play_history WHERE source = 'spotify_import' AND import_batch_id = 'b'")
    conn.commit()

    assert counted(conn) == {} == recomputed(conn)


# ------------------------------------------------------------------------------------------- upgrading


def test_a_library_with_history_gets_its_counts_when_it_upgrades(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    for version, _name, sql in _migration_files():
        if version <= 8:
            old.executescript(sql)
            old.execute(f"PRAGMA user_version = {version}")
    old.execute("INSERT INTO tracks (id, file_path) VALUES (1, '/m/a.mp3'), (2, '/m/b.mp3')")
    old.executemany("INSERT INTO play_history (track_id, source, played_at_epoch) VALUES (?, 'future_scrobble', ?)", [(1, 10), (1, 30), (2, 20), (None, 40)])
    old.commit()
    old.close()

    conn = connect(path)
    try:
        assert counted(conn) == {1: (2, 30), 2: (1, 20)} == recomputed(conn)
        conn.execute("INSERT INTO play_history (track_id, source, played_at_epoch) VALUES (2, 'future_scrobble', 50)")
        assert counted(conn)[2] == (2, 50), "and it keeps counting from there"
    finally:
        conn.close()


def test_the_library_screens_read_the_counts_without_counting_the_history(conn) -> None:
    from musictoolkit.web.queries import HISTORY_JOIN

    assert "track_plays" in HISTORY_JOIN and "COUNT" not in HISTORY_JOIN.upper()
