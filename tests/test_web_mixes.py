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
