"""The shelves on Home read the first few of a very large listing without ordering all of it. Each must give exactly
what listing everything and cutting it short would, so the listing itself is the reference here."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from musictoolkit.db.connection import connect
from musictoolkit.web import queries
from musictoolkit.web.queries import (
    ALBUM_ARTIST_FIND,
    ALBUM_ARTIST_SQL,
    ALBUM_FIND,
    ALBUM_SQL,
    build_selection,
    list_tracks,
)

NEVER_PLAYED = {"rules": [{"field": "unplayed", "op": "is", "value": False}]}


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "library.db")
    yield connection
    connection.close()


def add(conn, n, *, artist="Artist", album_artist=None, album="Album", added=None, year=2000, duration=100.0, missing=0, disc=None, track=None) -> int:
    conn.execute(
        "INSERT INTO tracks (id, file_path, title, artist, album_artist, album, year, duration_seconds, date_added, is_missing, disc_number, track_number) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (n, f"/m/{n}.mp3", f"Song {n}", artist, album_artist, album, year, duration, added or f"2024-01-01 00:00:{n % 60:02d}", missing, disc, track),
    )
    return n


def play(conn, track_id: int, at: int) -> None:
    conn.execute("INSERT INTO play_history (track_id, source, played_at_epoch) VALUES (?, 'future_scrobble', ?)", (track_id, at))


def reference_leaders(conn, by: str, limit: int) -> list[dict]:
    selection = build_selection(conn, rules=NEVER_PLAYED, sort=by, direction="desc")
    return list_tracks(conn, selection, 0, limit)["items"]


def reference_albums(conn) -> list[tuple]:
    inner = (
        f"SELECT {ALBUM_ARTIST_SQL} AS aa, {ALBUM_SQL} AS al, t.year AS year, t.duration_seconds AS dur, t.date_added AS added, t.id AS id "
        f"FROM tracks t WHERE t.is_missing = 0"
    )
    rows = conn.execute(
        f"SELECT aa, al, MIN(year) AS year, COUNT(*) AS n, SUM(dur) AS dur, MAX(added) AS added, MIN(id) AS first_id "
        f"FROM ({inner}) GROUP BY aa, al ORDER BY added DESC"
    ).fetchall()
    return [tuple(r) for r in rows]


# ------------------------------------------------------------------------------------------- most played, most recent


def a_library_with_many_ties(conn, seed: int = 3) -> None:
    """Eighty songs, a tenth of them missing, with few distinct play counts and last-played times so that ties at the
    cut-off are the rule, and names that differ so that the order within a tie matters."""
    rnd = random.Random(seed)
    for n in range(1, 81):
        add(conn, n, artist=f"Artist {n % 7}", album=f"Album {n % 5}", missing=int(n % 10 == 0), track=n % 4)
        for _ in range(rnd.choice([0, 0, 1, 1, 1, 2, 2, 3, 5])):
            play(conn, n, 1_700_000_000 + 1000 * rnd.choice([0, 1, 2, 3, 4, 5, 6]))
    conn.commit()


@pytest.mark.parametrize("by", ["plays", "last_played"])
@pytest.mark.parametrize("limit", [1, 3, 5, 10, 12, 30, 80, 200])
def test_the_leaders_are_what_listing_everything_and_cutting_it_short_gives(conn, by, limit) -> None:
    a_library_with_many_ties(conn)

    assert queries.played_leaders(conn, by, limit) == reference_leaders(conn, by, limit)


def test_when_fewer_songs_were_played_than_asked_for_all_of_them_are_returned(conn) -> None:
    for n in range(1, 6):
        add(conn, n)
    play(conn, 2, 100)
    play(conn, 4, 200)
    play(conn, 4, 300)
    conn.commit()

    assert [t["id"] for t in queries.played_leaders(conn, "plays", 10)] == [4, 2]
    assert [t["id"] for t in queries.played_leaders(conn, "last_played", 10)] == [4, 2]


def test_a_missing_song_is_not_a_leader_and_does_not_use_up_a_place(conn) -> None:
    add(conn, 1, missing=1)
    add(conn, 2)
    add(conn, 3)
    for _ in range(9):
        play(conn, 1, 500)  # the most played and most recent, but gone from the disk
    play(conn, 2, 100)
    play(conn, 3, 200)
    conn.commit()

    assert [t["id"] for t in queries.played_leaders(conn, "plays", 2)] == [2, 3]  # one play each: the library's own order
    assert [t["id"] for t in queries.played_leaders(conn, "last_played", 1)] == [3]


def test_nothing_played_gives_an_empty_shelf(conn) -> None:
    add(conn, 1)
    conn.commit()

    assert queries.played_leaders(conn, "plays", 10) == [] and queries.played_leaders(conn, "last_played", 10) == []


def test_the_leaders_are_found_through_the_indexes_and_not_by_ordering_every_played_song(conn) -> None:
    for n in range(1, 30):
        add(conn, n)
        play(conn, n, 1000 + n)
        play(conn, n, 2000 + n % 5)
    conn.commit()

    for by, index in (("plays", "idx_track_plays_plays"), ("last_played", "idx_track_plays_last")):
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        queries.played_leaders(conn, by, 3)
        conn.set_trace_callback(None)
        cut, page = [s for s in statements if "FROM track_plays h" in s]
        walk = " | ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + cut))
        assert index in walk and "TEMP B-TREE" not in walk, walk  # the index gives the order, and the LIMIT stops the walk
        reach = " | ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + page))
        assert index in reach and "SCAN" not in reach, reach  # only the songs at or above the cut are read


# ------------------------------------------------------------------------------------------- recently added albums


def test_recently_added_albums_are_the_albums_with_the_newest_songs_newest_first(conn) -> None:
    rnd = random.Random(5)
    n = 0
    for album in range(40):
        for track in range(rnd.randrange(1, 6)):
            n += 1
            add(conn, n, artist=f"Artist {album % 6}", album_artist=None if album % 3 == 0 else f"Artist {album % 6}", album=f"Album {album}" if album % 7 else "",
                added=f"2024-{1 + rnd.randrange(12):02d}-{1 + rnd.randrange(28):02d} {rnd.randrange(24):02d}:{rnd.randrange(60):02d}:{n % 60:02d}",
                year=rnd.choice([None, 1999, 2005, 2020]), duration=float(rnd.randrange(100, 400)), missing=int(n % 11 == 0))
    conn.commit()
    everything = reference_albums(conn)

    for limit in (1, 5, 12, 100):
        found = [tuple(r) for r in queries.newest_albums(conn, limit)]
        assert [(r[0], r[1]) for r in found] == [(r[0], r[1]) for r in everything[:limit]], limit
        assert all(row in everything for row in found), "an album's figures are those of the whole album"


def test_a_few_big_albums_added_last_do_not_hide_the_ones_before_them(conn) -> None:
    # Three albums of thirty songs were added last; reading the newest forty songs finds two albums, not five.
    n = 0
    for single in range(10):
        n += 1
        add(conn, n, album=f"Single {single}", added=f"2023-06-{10 + single:02d} 12:00:00")
    for big in range(3):
        for track in range(30):
            n += 1
            add(conn, n, album=f"Big {big}", added=f"2024-0{big + 1}-01 12:00:{track:02d}")
    conn.commit()

    found = [r["al"] for r in queries.newest_albums(conn, 5)]

    assert found == ["Big 2", "Big 1", "Big 0", "Single 9", "Single 8"]


def test_every_album_is_listed_when_the_library_has_fewer_than_asked_for(conn) -> None:
    add(conn, 1, album="One", added="2024-01-01 00:00:00")
    add(conn, 2, album="Two", added="2024-02-01 00:00:00")
    conn.commit()

    assert [r["al"] for r in queries.newest_albums(conn, 12)] == ["Two", "One"]


def test_missing_songs_do_not_make_an_album_new_or_count_in_it(conn) -> None:
    add(conn, 1, album="Old", added="2020-01-01 00:00:00")
    add(conn, 2, album="Old", added="2025-01-01 00:00:00", missing=1)
    add(conn, 3, album="Newer", added="2022-01-01 00:00:00")
    conn.commit()

    found = queries.newest_albums(conn, 12)

    assert [(r["al"], r["n"]) for r in found] == [("Newer", 1), ("Old", 1)]


# ------------------------------------------------------------------------------------------- random albums


def some_albums(conn, sizes: list[int]) -> dict[tuple[str, str], int]:
    n = 0
    for index, size in enumerate(sizes):
        for _ in range(size):
            n += 1
            add(conn, n, artist=f"Artist {index}", album=f"Album {index}", duration=float(n))
    conn.commit()
    return {(f"Artist {index}", f"Album {index}"): size for index, size in enumerate(sizes)}


@pytest.mark.parametrize("sampled", [False, True])
def test_random_albums_are_distinct_and_carry_the_figures_of_the_whole_album(conn, monkeypatch, sampled) -> None:
    monkeypatch.setattr(queries, "SMALL_LIBRARY", 0 if sampled else 10_000)
    sizes = some_albums(conn, [3, 1, 4, 2, 6, 1, 1, 5, 2, 3, 2, 4, 1, 7, 2])
    songs = sum(sizes.values())
    everything = {(r[0], r[1]): r for r in reference_albums(conn)}

    for _ in range(20):
        found = queries.random_albums(conn, 6, songs)
        keys = [(r["aa"], r["al"]) for r in found]
        assert len(keys) == 6 and len(set(keys)) == 6
        assert all(tuple(r) == everything[(r["aa"], r["al"])] for r in found)


def test_random_albums_are_as_likely_whatever_their_length(conn, monkeypatch) -> None:
    # One album of 200 songs among 20 singles: picking a random song would give the big album nine times in ten.
    monkeypatch.setattr(queries, "SMALL_LIBRARY", 0)
    sizes = some_albums(conn, [200] + [1] * 20)
    random.seed(11)

    picks = [queries.random_albums(conn, 1, sum(sizes.values()))[0]["al"] for _ in range(400)]

    assert 0.01 < picks.count("Album 0") / len(picks) < 0.12, "expected about one in twenty-one"
    assert len(set(picks)) >= 18, "the singles turn up too"


def test_a_library_of_few_long_albums_falls_back_to_listing_them(conn, monkeypatch) -> None:
    monkeypatch.setattr(queries, "SMALL_LIBRARY", 0)
    monkeypatch.setattr(queries, "MAX_PROBES", 40)
    sizes = some_albums(conn, [30, 30, 30])

    found = queries.random_albums(conn, 12, sum(sizes.values()))

    assert sorted(r["al"] for r in found) == ["Album 0", "Album 1", "Album 2"]


def test_random_albums_skip_missing_songs(conn, monkeypatch) -> None:
    monkeypatch.setattr(queries, "SMALL_LIBRARY", 0)
    add(conn, 1, album="Here")
    add(conn, 2, album="Gone", missing=1)
    conn.commit()

    for _ in range(20):
        assert [r["al"] for r in queries.random_albums(conn, 5, 1)] == ["Here"]


# ------------------------------------------------------------------------------------------- the album index


NAMES = [None, "", "x", "y"]


def test_the_lookup_spelling_of_the_album_names_means_what_the_grouping_spelling_does(conn) -> None:
    n = 0
    for artist in NAMES:
        for album_artist in NAMES:
            for album in NAMES:
                n += 1
                conn.execute("INSERT INTO tracks (id, file_path, artist, album_artist, album) VALUES (?, ?, ?, ?, ?)", (n, f"/m/{n}.mp3", artist, album_artist, album))
    conn.commit()

    rows = conn.execute(f"SELECT {ALBUM_ARTIST_SQL} AS a, {ALBUM_ARTIST_FIND} AS b, {ALBUM_SQL} AS c, {ALBUM_FIND} AS d FROM tracks t").fetchall()

    assert len(rows) == 64 and all(r["a"] == r["b"] and r["c"] == r["d"] for r in rows)


def plan(conn, sql: str, params=()) -> str:
    return " | ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_one_album_is_found_through_the_index_and_grouping_the_library_does_not_use_it(conn) -> None:
    some_albums(conn, [3, 2, 4])

    lookup = plan(conn, f"SELECT t.id FROM tracks t WHERE t.is_missing = 0 AND {ALBUM_ARTIST_FIND} = ? AND {ALBUM_FIND} = ?", ("Artist 1", "Album 1"))
    grouping = plan(conn, f"SELECT {ALBUM_ARTIST_SQL} AS aa, {ALBUM_SQL} AS al, COUNT(*) FROM tracks t WHERE t.is_missing = 0 GROUP BY aa, al")
    listing = plan(conn, "SELECT COUNT(*) FROM tracks t LEFT JOIN track_plays h ON h.track_id = t.id WHERE t.is_missing = 0")

    assert "idx_tracks_album_key" in lookup, lookup
    assert "idx_tracks_album_key" not in grouping + listing, (grouping, listing)


def test_an_albums_songs_are_found_by_the_names_the_library_shows(conn) -> None:
    add(conn, 1, artist="Solo", album_artist=None, album="Record", disc=1, track=2)
    add(conn, 2, artist="Solo", album_artist="", album="Record", disc=1, track=1)
    add(conn, 3, artist="Guest", album_artist="Solo", album="Record", disc=2, track=1)
    add(conn, 4, artist="Solo", album_artist=None, album=None)
    add(conn, 5, artist=None, album_artist=None, album="")
    conn.commit()

    named = build_selection(conn, album=queries.album_key("Solo", "Record"))
    unknown = build_selection(conn, album=queries.album_key("Unknown Artist", "Unknown Album"))
    solo_unknown = build_selection(conn, album=queries.album_key("Solo", "Unknown Album"))

    assert [t["id"] for t in list_tracks(conn, named, 0, 10)["items"]] == [2, 1, 3]
    assert [t["id"] for t in list_tracks(conn, unknown, 0, 10)["items"]] == [5]
    assert [t["id"] for t in list_tracks(conn, solo_unknown, 0, 10)["items"]] == [4]
