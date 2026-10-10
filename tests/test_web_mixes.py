"""The mixes API: what Home offers, opening one, and song radio."""

from __future__ import annotations

from tests.conftest import ids_by_title


def test_home_offers_only_the_mixes_the_library_can_fill(web) -> None:
    items = web.client.get("/api/mixes").json()["items"]

    assert items == [] or all({"id", "title", "subtitle", "count", "cover_track_id"} <= set(i) for i in items)
    assert all(i["count"] >= 8 for i in items), "nine songs cannot fill most mixes, which want eight or more of a kind"


def test_a_mix_opens_with_songs_in_play_order_and_an_unknown_one_is_a_clear_404(web) -> None:
    ids = ids_by_title(web.client)
    for title in ("Glacier", "Polar Night", "Drift"):
        web.client.patch(f"/api/tracks/{ids[title]}", json={"favorite": True})

    opened = web.client.get("/api/mixes/favorites").json()

    assert opened["title"] == "Your favorites" and sorted(t["title"] for t in opened["tracks"]) == ["Drift", "Glacier", "Polar Night"]
    missing = web.client.get("/api/mixes/no-such-mix")
    assert missing.status_code == 404 and "no such mix" in missing.json()["detail"]
    assert web.client.get("/api/mixes/unheard", params={"size": 2}).json()["tracks"].__len__() == 2


def test_song_radio_starts_with_the_song_and_stays_in_the_library(web) -> None:
    ids = ids_by_title(web.client)

    radio = web.client.get(f"/api/radio/track/{ids['Glacier']}", params={"size": 5}).json()

    assert radio["seed_track_id"] == ids["Glacier"] and radio["tracks"][0]["id"] == ids["Glacier"]
    assert 2 <= len(radio["tracks"]) <= 5 and {t["id"] for t in radio["tracks"]} <= set(ids.values())
    assert web.client.get("/api/radio/track/999999").status_code == 404


# ------------------------------------------------------------------------------------------- reusing the shelf

import pytest  # noqa: E402


@pytest.fixture
def shelves(monkeypatch):
    """Count how often the shelf is really worked out."""
    from musictoolkit.web import mixes

    real = mixes.available
    calls = []

    def counting(conn, now=None, offset_minutes=0, size=mixes.DEFAULT_SIZE, minimum=mixes.MIN_TRACKS):
        calls.append((offset_minutes, size))
        return real(conn, now, offset_minutes, size, minimum)

    monkeypatch.setattr(mixes, "available", counting)
    return calls


def test_the_shelf_is_worked_out_once_while_nothing_changes(web, shelves) -> None:
    first = web.client.get("/api/mixes").json()
    second = web.client.get("/api/mixes").json()

    assert first == second and len(shelves) == 1


def test_a_different_time_zone_or_size_is_a_different_shelf(web, shelves) -> None:
    web.client.get("/api/mixes")
    web.client.get("/api/mixes", params={"tz": 120})
    web.client.get("/api/mixes", params={"size": 20})
    web.client.get("/api/mixes")

    assert len(shelves) == 3


@pytest.mark.parametrize("change", [
    "INSERT INTO play_history (track_id, source, played_at_epoch) VALUES (1, 'future_scrobble', 1000)",
    "INSERT INTO tracks (file_path, title) VALUES ('/new.mp3', 'New')",
    "UPDATE tracks SET favorite = 1 WHERE id = 1",
    "UPDATE tracks SET is_missing = 1 WHERE id = 2",
])
def test_a_change_to_the_library_or_the_history_makes_the_shelf_be_worked_out_again(web, shelves, change) -> None:
    web.client.get("/api/mixes")

    with web.ctx.db() as conn:
        conn.execute(change)
        conn.commit()
    web.client.get("/api/mixes")

    assert len(shelves) == 2


def test_the_shelf_is_new_each_day_and_does_not_outlive_five_minutes(web, shelves, monkeypatch) -> None:
    from musictoolkit.web import mixes
    from musictoolkit.web.routers import mixes as router

    clock = [100.0]
    day = [20_000]
    monkeypatch.setattr(router.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mixes, "daily_seed", lambda now: day[0])
    web.client.get("/api/mixes")

    clock[0] += router.CACHE_SECONDS - 1
    web.client.get("/api/mixes")
    assert len(shelves) == 1

    clock[0] += 2
    web.client.get("/api/mixes")
    assert len(shelves) == 2

    day[0] += 1
    web.client.get("/api/mixes")
    assert len(shelves) == 3
