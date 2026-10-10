"""The statistics API: periods, time zones, and refusing nonsense."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


def epoch(year: int, month: int, day: int, hour: int = 12) -> int:
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def listened(web):
    """A history: three plays of Glacier and one of Gravel Dust in 2025, one play of Drift in 2024."""
    with web.ctx.db() as conn:
        for at, artist, title in [
            (epoch(2025, 3, 1), "Aurora Vale", "Glacier"), (epoch(2025, 3, 2), "Aurora Vale", "Glacier"),
            (epoch(2025, 4, 1), "Aurora Vale", "Glacier"), (epoch(2025, 4, 2), "The Fernwoods", "Gravel Dust"),
            (epoch(2024, 12, 31, 23), "Aurora Vale", "Drift"),
        ]:
            conn.execute(
                "INSERT INTO play_history (source, played_at_epoch, ms_played, raw_artist_name, raw_track_name) VALUES ('future_scrobble', ?, 200000, ?, ?)",
                (at, artist, title),
            )
        conn.commit()
    return web


def test_the_overview_describes_a_year(listened) -> None:
    result = listened.client.get("/api/stats/overview", params={"range": "year:2025"}).json()

    assert result["title"] == "2025" and result["empty"] is False
    assert result["totals"]["plays"] == 4 and result["totals"]["artists"] == 2
    assert [(t["title"], t["plays"]) for t in result["top_tracks"]] == [("Glacier", 3), ("Gravel Dust", 1)]
    assert [m["month"] for m in result["by_month"]] == ["2025-03", "2025-04"]


def test_the_viewers_time_zone_decides_which_year_a_play_belongs_to(listened) -> None:
    utc = listened.client.get("/api/stats/overview", params={"range": "year:2025", "tz": 0}).json()
    east = listened.client.get("/api/stats/overview", params={"range": "year:2025", "tz": 120}).json()

    assert (utc["totals"]["plays"], east["totals"]["plays"]) == (4, 5), "Drift on the 31st at 23:00 UTC is already 2025 two hours east"
    assert listened.client.get("/api/stats/years", params={"tz": 120}).json()["items"] == [{"year": 2025, "plays": 5}]
    assert listened.client.get("/api/stats/years").json()["items"] == [{"year": 2025, "plays": 4}, {"year": 2024, "plays": 1}]


def test_the_default_period_is_everything_and_the_size_of_the_tops_can_be_chosen(listened) -> None:
    result = listened.client.get("/api/stats/overview", params={"limit": 1}).json()

    assert result["title"] == "All time" and result["totals"]["plays"] == 5 and len(result["top_artists"]) == 1


@pytest.mark.parametrize("period", ["year", "year:20255", "month:2025-13", "days:abc", "all; DROP TABLE tracks", ""])
def test_nonsense_periods_are_refused_not_run(listened, period) -> None:
    response = listened.client.get("/api/stats/overview", params={"range": period})

    assert response.status_code == 422 and "period" in response.json()["detail"]


def test_an_empty_history_gives_an_empty_overview_not_an_error(web) -> None:
    result = web.client.get("/api/stats/overview").json()

    assert result["empty"] is True and result["totals"]["plays"] == 0
    assert web.client.get("/api/stats/years").json() == {"items": []}
