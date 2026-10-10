"""Mixes and song radio: what each one asks of the library, and that a day's draw stays put."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from musictoolkit.db.connection import connect
from musictoolkit.web import mixes

NOW = datetime(2025, 6, 15, 12, tzinfo=timezone.utc)
DAY = 86400


def ago(days: float) -> int:
    return int((NOW - timedelta(days=days)).timestamp())


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "mixes.db")
    yield connection
    connection.close()


def add(conn, title: str, artist: str = "Artist", genre: str | None = "Rock", year: int | None = 2005, favorite: int = 0, rating: int | None = None,
        added_days_ago: float = 400, missing: int = 0) -> int:
    added = (NOW - timedelta(days=added_days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute(
        "INSERT INTO tracks (file_path, title, artist, album, genre, year, favorite, rating, date_added, is_missing) VALUES (?, ?, ?, 'Album', ?, ?, ?, ?, ?, ?)",
        (f"/music/{artist}/{title}.mp3", title, artist, genre, year, favorite, rating, added, missing),
    )
    conn.commit()
    return cursor.lastrowid


def play(conn, track_id: int, days_ago: float, times: int = 1) -> None:
    for n in range(times):
        conn.execute(
            "INSERT INTO play_history (track_id, source, played_at_epoch, ms_played, raw_artist_name, raw_track_name) VALUES (?, 'future_scrobble', ?, 180000, 'a', 't')",
            (track_id, ago(days_ago) - n * 600),
        )
    conn.commit()


def ids(mix) -> list[int]:
    return [t["id"] for t in mix["tracks"]]


# ------------------------------------------------------------------------------------------- the mixes


def test_on_repeat_is_what_was_played_twice_or_more_in_the_last_month_most_first(conn) -> None:
    most, some, once, old = (add(conn, name) for name in ("most", "some", "once", "old"))
    play(conn, most, 3, times=5)
    play(conn, some, 10, times=2)
    play(conn, once, 4)
    play(conn, old, 90, times=9)

    assert ids(mixes.build(conn, "on-repeat", NOW)) == [most, some]


def test_rediscover_is_what_was_loved_and_has_gone_quiet(conn) -> None:
    played_a_lot_long_ago = add(conn, "worn")
    play(conn, played_a_lot_long_ago, 200, times=4)
    played_a_lot_lately = add(conn, "current")
    play(conn, played_a_lot_lately, 10, times=4)
    starred_and_quiet = add(conn, "starred", favorite=1)
    play(conn, starred_and_quiet, 100)
    starred_never_played = add(conn, "fresh star", favorite=1)
    barely_played = add(conn, "barely")
    play(conn, barely_played, 300, times=2)

    result = mixes.build(conn, "rediscover", NOW)

    assert sorted(ids(result)) == sorted([played_a_lot_long_ago, starred_and_quiet])
    assert starred_never_played not in ids(result)


def test_on_this_day_is_what_was_played_around_this_date_in_earlier_years(conn) -> None:
    last_year = add(conn, "a year ago")
    play(conn, last_year, 365 + 1)  # 14 June 2024
    two_years = add(conn, "two years ago")
    play(conn, two_years, 365 * 2 + 3)
    far = add(conn, "far from today")
    play(conn, far, 365 + 60)
    this_year = add(conn, "this year")
    play(conn, this_year, 1)  # 14 June 2025: not "earlier years"

    assert ids(mixes.build(conn, "on-this-day", NOW)) == [last_year, two_years], "equally played songs come in library order"


def test_new_arrivals_are_the_last_thirty_days_newest_first(conn) -> None:
    newest = add(conn, "newest", added_days_ago=1)
    newer = add(conn, "newer", added_days_ago=20)
    add(conn, "old", added_days_ago=45)

    assert ids(mixes.build(conn, "new-arrivals", NOW)) == [newest, newer]


def test_never_played_and_favorites_are_what_they_say(conn) -> None:
    heard = add(conn, "heard")
    play(conn, heard, 5)
    never = [add(conn, f"never {n}") for n in range(3)]
    loved = add(conn, "loved", favorite=1)

    assert sorted(ids(mixes.build(conn, "unheard", NOW))) == sorted([*never, loved])
    assert ids(mixes.build(conn, "favorites", NOW)) == [loved]


def test_decades_and_genres_appear_only_when_there_are_enough_songs(conn) -> None:
    for n in range(mixes.MIN_TRACKS):
        add(conn, f"nineties {n}", genre="Folk", year=1990 + n % 10)
    add(conn, "lonely", genre="Jazz", year=1975)

    offered = {m["id"]: m for m in mixes.available(conn, NOW, minimum=1)}

    assert offered["decade-1990"]["title"] == "The 1990s" and offered["decade-1990"]["count"] == mixes.MIN_TRACKS
    titles = {m["title"] for m in offered.values()}
    assert "decade-1970" not in offered and "Jazz" not in titles and "Folk" in titles
    assert ids(mixes.build(conn, "decade-1990", NOW)) and all(t["year"] // 10 == 199 for t in mixes.build(conn, "decade-1990", NOW)["tracks"])


def test_a_mix_with_too_few_songs_is_not_offered_at_all(conn) -> None:
    for n in range(3):
        add(conn, f"new {n}", added_days_ago=2)

    assert [m["id"] for m in mixes.available(conn, NOW)] == []
    assert [m["id"] for m in mixes.available(conn, NOW, minimum=3) if m["id"] == "new-arrivals"] == ["new-arrivals"]


def test_a_genre_with_awkward_characters_survives_its_own_id(conn) -> None:
    for n in range(mixes.MIN_TRACKS):
        add(conn, f"song {n}", genre="Rock/Pop & Soul?")

    (genre_mix,) = [m for m in mixes.available(conn, NOW) if m["title"] == "Rock/Pop & Soul?"]

    assert mixes.genre_of(genre_mix["id"]) == "Rock/Pop & Soul?" and mixes.genre_of("decade-1990") is None and mixes.genre_of("genre-!!!") is None
    assert all(c.isalnum() or c in "-_" for c in genre_mix["id"]), genre_mix["id"]
    assert len(mixes.build(conn, genre_mix["id"], NOW)["tracks"]) == mixes.MIN_TRACKS


def test_a_random_mix_is_the_same_all_day_and_a_new_draw_tomorrow(conn) -> None:
    for n in range(60):
        add(conn, f"song {n}")

    morning = ids(mixes.build(conn, "unheard", NOW, size=10))
    evening = ids(mixes.build(conn, "unheard", NOW + timedelta(hours=8), size=10))
    tomorrow = ids(mixes.build(conn, "unheard", NOW + timedelta(days=1), size=10))

    assert len(morning) == 10 and morning == evening and morning != tomorrow
    assert ids(mixes.build(conn, "unheard", NOW, size=10, seed=7)) != morning


def test_songs_whose_files_are_gone_are_never_in_a_mix_and_unknown_mixes_are_nothing(conn) -> None:
    gone = add(conn, "gone", favorite=1, missing=1)
    here = add(conn, "here", favorite=1)

    assert ids(mixes.build(conn, "favorites", NOW)) == [here] and gone not in ids(mixes.build(conn, "favorites", NOW))
    assert mixes.build(conn, "no-such-mix", NOW) is None
    assert mixes.build(conn, "new-arrivals", NOW) is None, "an empty mix is not a mix"


# ------------------------------------------------------------------------------------------- song radio


def test_radio_starts_with_the_song_and_prefers_the_same_sitting_then_genre(conn) -> None:
    seed = add(conn, "seed", artist="Aurora", genre="Electronic", year=2019)
    same_sitting = add(conn, "same sitting", artist="Other", genre="Jazz", year=1960)
    same_genre = add(conn, "same genre", artist="Third", genre="Electronic", year=2018)
    unrelated = add(conn, "unrelated", artist="Fourth", genre="Country", year=1975)
    play(conn, seed, 5)
    play(conn, same_sitting, 5 - 0.005)  # a few minutes later

    order = ids({"tracks": mixes.radio(conn, seed, size=10)})

    assert order == [seed, same_sitting, same_genre], "the same sitting first, then the same genre and era; nothing in common, not at all"


def test_radio_does_not_play_one_artist_back_to_back_or_let_one_artist_take_over(conn) -> None:
    seed = add(conn, "seed", artist="Aurora", genre="Rock")
    own = [add(conn, f"own {n}", artist="Aurora", genre="Rock") for n in range(10)]
    others = [add(conn, f"other {n}", artist=f"Band {n % 4}", genre="Rock") for n in range(12)]

    result = mixes.radio(conn, seed, size=15, seed=3)
    artists = [t["artist"] for t in result]

    assert result[0]["id"] == seed and len(result) == 15
    assert all(a != b for a, b in zip(artists, artists[1:])), artists
    assert sum(1 for a in artists if a == "Aurora") <= 1 + 15 // 3
    assert set(ids({"tracks": result})) <= {seed, *own, *others}


def test_radio_is_repeatable_and_ignores_missing_files_and_unknown_seeds(conn) -> None:
    seed = add(conn, "seed", genre="Rock")
    for n in range(20):
        add(conn, f"song {n}", artist=f"Band {n}", genre="Rock")
    gone = add(conn, "gone", artist="Gone", genre="Rock", missing=1)

    first = ids({"tracks": mixes.radio(conn, seed, size=10, seed=1)})

    assert first == ids({"tracks": mixes.radio(conn, seed, size=10, seed=1)})
    assert gone not in first
    assert mixes.radio(conn, 99_999) == [] and mixes.radio(conn, gone) == []
