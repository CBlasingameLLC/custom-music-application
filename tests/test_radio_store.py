"""radio.store: the stations the person keeps."""

from __future__ import annotations

from pathlib import Path

import pytest

from musictoolkit.db.connection import connect
from musictoolkit.radio import store


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "library.db")
    yield connection
    connection.close()


def kept(conn, name="Jazz FM", url="http://stream.example/jazz", **more):
    return store.add_station(conn, name=name, stream_url=url, **more)


def test_a_station_is_kept_with_what_is_known_about_it(conn) -> None:
    station = kept(conn, tags="Jazz, jazz, Smooth Jazz,, ", country="France", country_code="fr", codec="MP3", bitrate=128, homepage="http://jazz.example")
    assert station["name"] == "Jazz FM" and station["kind"] == "station" and station["favorite"] is False
    assert station["tags"] == ["jazz", "smooth jazz"] and station["country_code"] == "FR" and station["bitrate"] == 128
    assert station["has_art"] is False and station["play_count"] == 0 and station["last_played_at"] is None


def test_names_are_tidied_and_never_empty(conn) -> None:
    assert kept(conn, name="   Jazz \n  FM  ")["name"] == "Jazz FM"
    assert kept(conn, name="   ", url="http://stream.example/other")["name"] == "http://stream.example/other"
    assert len(kept(conn, name="x" * 500, url="http://stream.example/long")["name"]) == store.MAX_NAME


def test_keeping_the_same_stream_again_refreshes_it_and_keeps_the_marks(conn) -> None:
    first = kept(conn, favorite=True, tags="jazz", country="France")
    store.record_play(conn, first["id"])
    again = kept(conn, name="Jazz FM (new name)", tags="jazz, smooth", country=None, bitrate=192)
    assert again["id"] == first["id"] and len(store.list_stations(conn)) == 1
    assert again["name"] == "Jazz FM (new name)" and again["tags"] == ["jazz", "smooth"] and again["bitrate"] == 192
    assert again["favorite"] is True and again["play_count"] == 1 and again["country"] == "France", "what the directory did not say is not forgotten"


def test_favorites_come_first_and_the_scopes_pick_what_they_say(conn) -> None:
    a = kept(conn, name="Zulu", url="http://s/z", favorite=True)
    b = kept(conn, name="alpha", url="http://s/a")
    c = kept(conn, name="Mike", url="http://s/m", favorite=True)
    store.record_play(conn, b["id"])
    store.record_play(conn, a["id"])
    assert [s["name"] for s in store.list_stations(conn)] == ["Mike", "Zulu", "alpha"]
    assert [s["name"] for s in store.list_stations(conn, "favorites")] == ["Mike", "Zulu"]
    assert [s["name"] for s in store.list_stations(conn, "recent")] == ["Zulu", "alpha"], "latest first, and only those that have been played"
    assert c["id"] not in [s["id"] for s in store.list_stations(conn, "recent")]


def test_a_station_can_be_renamed_marked_and_removed(conn) -> None:
    station = kept(conn)
    assert store.update_station(conn, station["id"], favorite=True)["favorite"] is True
    assert store.update_station(conn, station["id"], name="  My jazz ")["name"] == "My jazz"
    assert store.update_station(conn, station["id"], name="   ")["name"] == "My jazz", "an empty name changes nothing"
    assert store.update_station(conn, 9999, favorite=True) is None
    assert store.delete_station(conn, station["id"]) is True and store.get_station(conn, station["id"]) is None
    assert store.delete_station(conn, station["id"]) is False


def test_playing_a_station_counts_and_dates_it(conn) -> None:
    station = kept(conn)
    store.record_play(conn, station["id"])
    store.record_play(conn, station["id"])
    got = store.get_station(conn, station["id"])
    assert got["play_count"] == 2 and got["last_played_at"]
