"""Listening statistics: what the numbers count, how periods are cut, and how local time moves plays between days."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from musictoolkit.db.connection import connect
from musictoolkit.history import stats


def epoch(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "stats.db")
    yield connection
    connection.close()


def add_track(conn, title: str, artist: str, album: str = "Album", genre: str | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO tracks (file_path, title, artist, album, genre, is_missing) VALUES (?, ?, ?, ?, ?, 0)",
        (f"/music/{artist}/{title}.mp3", title, artist, album, genre),
    )
    conn.commit()
    return cursor.lastrowid


def play(conn, at: int, artist: str | None, title: str | None, album: str | None = None, ms: int | None = 180_000, track_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO play_history (track_id, source, played_at_epoch, ms_played, raw_artist_name, raw_track_name, raw_album_name) "
        "VALUES (?, 'future_scrobble', ?, ?, ?, ?, ?)",
        (track_id, at, ms, artist, title, album),
    )
    conn.commit()


# ------------------------------------------------------------------------------------------- periods


def test_a_period_is_cut_in_the_viewers_time_zone() -> None:
    start, end, title = stats.resolve_range("year:2025", 120)  # UTC+2

    assert (start, end, title) == (epoch(2024, 12, 31, 22), epoch(2025, 12, 31, 22), "2025")
    assert stats.resolve_range("month:2025-12", 0)[:2] == (epoch(2025, 12, 1, 0), epoch(2026, 1, 1, 0))
    assert stats.resolve_range("month:2025-02", 0)[2] == "February 2025"
    now = datetime(2025, 6, 10, 12, tzinfo=timezone.utc)
    assert stats.resolve_range("days:30", 0, now) == (epoch(2025, 5, 11), epoch(2025, 6, 10) + 1, "The last 30 days")
    assert stats.resolve_range("all", 0, now)[0] == 0


@pytest.mark.parametrize("spec", ["", "year", "year:25", "month:2025-13", "days:", "days:-3", "all;drop table tracks", "YEAR:2025"])
def test_nonsense_periods_are_refused(spec) -> None:
    with pytest.raises(stats.BadRange):
        stats.resolve_range(spec, 0)


def test_an_impossible_offset_is_brought_back_to_a_real_one() -> None:
    assert stats.clamp_offset(99_999) == 14 * 60 and stats.clamp_offset(-99_999) == -14 * 60 and stats.clamp_offset(-300) == -300


# ------------------------------------------------------------------------------------------- what is counted


def test_totals_count_every_play_and_each_song_and_artist_once(conn) -> None:
    for day in (1, 2, 3):
        play(conn, epoch(2025, 3, day), "Aurora Vale", "Glacier", "Northern Lights")
    play(conn, epoch(2025, 3, 3, 13), "aurora vale", "GLACIER")  # same song, written differently
    play(conn, epoch(2025, 3, 4), "Aurora Vale", "Drift")
    play(conn, epoch(2025, 3, 5), "The Fernwoods", "Gravel Dust", ms=None)

    totals = stats.overview(conn, "all")["totals"]

    assert (totals["plays"], totals["artists"], totals["tracks"], totals["active_days"]) == (6, 2, 3, 5)
    assert totals["minutes"] == 15 and totals["first_play"] == epoch(2025, 3, 1) and totals["last_play"] == epoch(2025, 3, 5)


def test_the_tops_are_ranked_by_plays_and_named_as_first_written(conn) -> None:
    for _ in range(3):
        play(conn, epoch(2025, 3, 1), "Aurora Vale", "Glacier", "Northern Lights")
    for _ in range(2):
        play(conn, epoch(2025, 3, 2), "The Fernwoods", "Gravel Dust", "Back Roads")
    play(conn, epoch(2025, 3, 3), "Aurora Vale", "Drift", "Northern Lights")

    result = stats.overview(conn, "all", limit=2)

    assert [(a["name"], a["plays"]) for a in result["top_artists"]] == [("Aurora Vale", 4), ("The Fernwoods", 2)]
    assert [(t["title"], t["plays"]) for t in result["top_tracks"]] == [("Glacier", 3), ("Gravel Dust", 2)]
    assert [(a["album"], a["artist"], a["plays"]) for a in result["top_albums"]] == [("Northern Lights", "Aurora Vale", 4), ("Back Roads", "The Fernwoods", 2)]


def test_a_play_without_names_borrows_them_from_the_song_and_genres_need_a_song(conn) -> None:
    glacier = add_track(conn, "Glacier", "Aurora Vale", "Northern Lights", "Electronic")
    play(conn, epoch(2025, 3, 1), None, None, track_id=glacier)
    play(conn, epoch(2025, 3, 2), "Someone Else", "Not In The Library")  # imported history of a song the library does not have

    result = stats.overview(conn, "all")

    assert {a["name"] for a in result["top_artists"]} == {"Aurora Vale", "Someone Else"}
    assert [(g["genre"], g["plays"]) for g in result["top_genres"]] == [("Electronic", 1)]
    track = next(t for t in result["top_tracks"] if t["title"] == "Glacier")
    assert track["track_id"] == glacier and next(t for t in result["top_tracks"] if t["artist"] == "Someone Else")["track_id"] is None


def test_only_plays_inside_the_period_count(conn) -> None:
    play(conn, epoch(2024, 12, 31, 20), "A", "Last year")
    play(conn, epoch(2025, 6, 1), "A", "This year")
    play(conn, epoch(2026, 1, 1, 1), "A", "Next year")

    assert stats.overview(conn, "year:2025")["totals"]["plays"] == 1
    assert stats.overview(conn, "year:2025", offset_minutes=240)["totals"]["plays"] == 2, "in UTC+4 the 31st of December at 20:00 is already 2025"
    assert stats.overview(conn, "month:2025-06")["top_tracks"][0]["title"] == "This year"


def test_a_period_with_no_plays_says_so(conn) -> None:
    result = stats.overview(conn, "year:2025")

    assert result["empty"] is True and result["totals"]["plays"] == 0 and result["by_hour"] == [0] * 24 and result["top_artists"] == []


# ------------------------------------------------------------------------------------------- when


def test_months_with_nothing_still_appear_so_a_chart_has_no_gaps(conn) -> None:
    play(conn, epoch(2025, 1, 15), "A", "One")
    play(conn, epoch(2025, 1, 16), "A", "One")
    play(conn, epoch(2025, 3, 15), "A", "Two")

    months = stats.overview(conn, "year:2025")["by_month"]

    assert [(m["month"], m["plays"]) for m in months] == [("2025-01", 2), ("2025-02", 0), ("2025-03", 1)]


def test_local_time_moves_a_late_play_into_the_next_day_hour_and_weekday(conn) -> None:
    play(conn, epoch(2025, 3, 3, 23, 30), "A", "Late")  # Monday 23:30 UTC

    utc = stats.overview(conn, "all", offset_minutes=0)
    berlin = stats.overview(conn, "all", offset_minutes=60)  # Tuesday 00:30

    assert (utc["by_hour"][23], utc["by_weekday"][0]) == (1, 1), "weeks start on Monday"
    assert (berlin["by_hour"][0], berlin["by_weekday"][1]) == (1, 1)
    assert stats.overview(conn, "year:2025", offset_minutes=60)["by_day"] == [{"date": "2025-03-04", "plays": 1}]


def test_the_longest_run_of_days_with_a_play(conn) -> None:
    for day in (1, 2, 3, 5, 6, 7, 8, 12):
        play(conn, epoch(2025, 3, day), "A", "Song")

    streak = stats.overview(conn, "all")["streak"]

    assert streak == {"days": 4, "from": "2025-03-05", "to": "2025-03-08"}
    assert stats.overview(conn, "month:2024-01")["streak"]["days"] == 0


def test_artists_first_heard_in_the_period_are_the_discoveries(conn) -> None:
    play(conn, epoch(2024, 5, 1), "Old Favourite", "Song")
    play(conn, epoch(2025, 2, 1), "Old Favourite", "Song")
    for day in (3, 4):
        play(conn, epoch(2025, 2, day), "Fresh", "New song")
    play(conn, epoch(2025, 7, 1), "Also Fresh", "Other")

    found = stats.overview(conn, "year:2025")["discoveries"]

    assert found["count"] == 2 and [(d["name"], d["plays"]) for d in found["items"]] == [("Fresh", 2), ("Also Fresh", 1)]
    assert stats.overview(conn, "all")["discoveries"]["count"] == 3


def test_each_month_has_its_most_played_song(conn) -> None:
    for _ in range(3):
        play(conn, epoch(2025, 1, 10), "A", "January song")
    play(conn, epoch(2025, 1, 11), "A", "Other")
    play(conn, epoch(2025, 2, 10), "B", "February song")

    favourites = stats.overview(conn, "year:2025")["month_favorites"]

    assert [(f["month"], f["title"], f["plays"]) for f in favourites] == [("2025-01", "January song", 3), ("2025-02", "February song", 1)]


def test_the_years_to_choose_from_follow_the_viewers_calendar(conn) -> None:
    play(conn, epoch(2024, 12, 31, 23, 30), "A", "One")
    play(conn, epoch(2025, 6, 1), "A", "Two")

    assert stats.years(conn, 0) == [{"year": 2025, "plays": 1}, {"year": 2024, "plays": 1}]
    assert stats.years(conn, 60) == [{"year": 2025, "plays": 2}]


def test_a_period_longer_than_a_year_does_not_list_every_day(conn) -> None:
    play(conn, epoch(2023, 1, 1), "A", "One")
    play(conn, epoch(2025, 1, 1), "A", "Two")

    assert stats.overview(conn, "all")["by_day"] == []
    assert len(stats.overview(conn, "year:2025")["by_day"]) == 1
