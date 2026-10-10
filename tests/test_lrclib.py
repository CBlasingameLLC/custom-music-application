"""Lyrics from lrclib.net: what is asked, what is believed, and what is kept (against a local stand-in for the service)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from musictoolkit.db.connection import connect
from musictoolkit.media import lrclib
from tests.fake_services import serve

SYNCED = "[00:01.00]First line\n[00:05.50]Second line\n"
HIT = {"id": 7, "trackName": "Glacier", "artistName": "Aurora Vale", "albumName": "Northern Lights", "duration": 200.0,
       "instrumental": False, "plainLyrics": "First line\nSecond line", "syncedLyrics": SYNCED}
SONG = {"id": 1, "artist": "Aurora Vale", "title": "Glacier", "album": "Northern Lights", "duration": 200.4}


@pytest.fixture(autouse=True)
def forget_trouble():
    lrclib._trouble.clear()
    yield
    lrclib._trouble.clear()


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "library.db")
    connection.execute("INSERT INTO tracks (id, file_path, title, artist) VALUES (1, 'a.mp3', 'Glacier', 'Aurora Vale')")
    connection.commit()
    yield connection
    connection.close()


def against(monkeypatch, routes):
    context = serve(routes)
    service = context.__enter__()
    monkeypatch.setattr(lrclib, "BASE_URL", service.url)
    return service, context


@pytest.fixture
def service(monkeypatch):
    holders = []

    def start(routes):
        started, context = against(monkeypatch, routes)
        holders.append(context)
        return started

    yield start
    for context in holders:
        context.__exit__(None, None, None)


# ------------------------------------------------------------------------------------------- asking


def test_a_song_is_asked_for_by_artist_title_album_and_length_and_the_app_says_who_it_is(service) -> None:
    lrclib_service = service({"/get": (200, HIT)})

    found = lrclib.lookup("Aurora Vale", "Glacier", "Northern Lights", 200.4)

    request = lrclib_service.requests[0]
    assert request["query"] == {"artist_name": "Aurora Vale", "track_name": "Glacier", "album_name": "Northern Lights", "duration": "200"}
    assert request["headers"]["User-Agent"].startswith("Music Toolkit/") and "github.com" in request["headers"]["User-Agent"]
    assert found["synced"] == [{"t": 1.0, "text": "First line"}, {"t": 5.5, "text": "Second line"}]
    assert found["plain"] is None, "timed lyrics are kept as timed, not twice"
    assert found["id"] == 7


def test_only_what_is_known_about_the_song_is_sent(service) -> None:
    lrclib_service = service({"/get": (200, {**HIT, "syncedLyrics": None})})

    found = lrclib.lookup("Aurora Vale", "Glacier", None, None)

    assert lrclib_service.requests[0]["query"] == {"artist_name": "Aurora Vale", "track_name": "Glacier"}
    assert found["synced"] is None and found["plain"] == "First line\nSecond line"


@pytest.mark.parametrize(("artist", "title"), [(None, "Glacier"), ("Aurora Vale", ""), ("", None)])
def test_a_song_without_an_artist_or_a_title_is_not_looked_up(service, artist, title) -> None:
    lrclib_service = service({"/get": (200, HIT)})

    assert lrclib.lookup(artist, title) is None
    assert lrclib_service.requests == []


def test_an_instrumental_is_reported_as_one(service) -> None:
    service({"/get": (200, {"id": 9, "instrumental": True, "plainLyrics": None, "syncedLyrics": None})})

    assert lrclib.lookup("Aurora Vale", "Glacier", "Northern Lights", 200) == {"synced": None, "plain": None, "instrumental": True, "id": 9}


def test_an_entry_without_any_lyrics_is_not_a_hit(service) -> None:
    service({"/get": (200, {"id": 9, "instrumental": False, "plainLyrics": " \n", "syncedLyrics": ""})})

    assert lrclib.lookup("Aurora Vale", "Glacier", None, 200) is None


# ------------------------------------------------------------------------------------------- another edition


def edition(duration, synced=True, **fields):
    return {**HIT, "duration": duration, "syncedLyrics": SYNCED if synced else None, **fields}


def test_when_the_exact_request_finds_nothing_a_search_finds_the_same_length(service) -> None:
    lrclib_service = service({
        "/get": (404, {"code": 404}),
        "/search": (200, [edition(260.0, id=1), edition(201.0, id=2), edition(199.0, id=3, synced=False)]),
    })

    found = lrclib.lookup("Aurora Vale", "Glacier", "Some Compilation", 200.0)

    assert found["id"] == 2, "within three seconds of the file, and timed"
    assert lrclib_service.requests[1]["query"] == {"artist_name": "Aurora Vale", "track_name": "Glacier"}


def test_a_different_length_would_not_line_up_and_is_not_used(service) -> None:
    service({"/get": (404, None), "/search": (200, [edition(262.0), edition(190.0)])})

    assert lrclib.lookup("Aurora Vale", "Glacier", None, 200.0) is None


def test_a_search_result_for_another_song_or_artist_is_not_used(service) -> None:
    service({"/get": (404, None), "/search": (200, [edition(200.0, trackName="Glacier Ii"), edition(200.0, artistName="Someone Else")])})

    assert lrclib.lookup("Aurora Vale", "Glacier", None, 200.0) is None


def test_names_that_differ_only_in_case_and_punctuation_are_the_same_song(service) -> None:
    service({"/get": (404, None), "/search": (200, [edition(200.0, trackName="GLACIER!", artistName="aurora  vale")])})

    assert lrclib.lookup("Aurora Vale", "Glacier", None, 200.0)["id"] == 7


def test_without_a_length_the_first_timed_edition_will_do(service) -> None:
    service({"/get": (404, None), "/search": (200, [edition(123.0, synced=False, id=1), edition(321.0, id=2)])})

    assert lrclib.lookup("Aurora Vale", "Glacier", None, None)["id"] == 2


# ------------------------------------------------------------------------------------------- trouble


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_busy_service_is_trouble_not_an_answer(service, status) -> None:
    service({"/get": (status, {"message": "busy"})})

    with pytest.raises(lrclib.LrclibError, match="busy"):
        lrclib.lookup("Aurora Vale", "Glacier", None, 200)


def test_an_unexpected_refusal_is_trouble_with_its_number(service) -> None:
    service({"/get": (400, {"message": "bad request"})})

    with pytest.raises(lrclib.LrclibError, match="error 400"):
        lrclib.lookup("Aurora Vale", "Glacier", None, 200)


def test_no_answer_at_all_is_trouble(monkeypatch) -> None:
    monkeypatch.setattr(lrclib, "BASE_URL", "http://127.0.0.1:9")  # nothing listens on the discard port

    with pytest.raises(lrclib.LrclibError, match="Could not reach"):
        lrclib.lookup("Aurora Vale", "Glacier", None, 200)


def test_an_answer_that_is_not_json_is_trouble(service, monkeypatch) -> None:
    lrclib_service = service({})
    lrclib_service.routes["/get"] = (200, None)  # an empty body with a 200

    with pytest.raises(lrclib.LrclibError, match="could not be read"):
        lrclib.lookup("Aurora Vale", "Glacier", None, 200)


# ------------------------------------------------------------------------------------------- what is kept


def test_an_answer_is_kept_and_not_asked_for_again(service, conn) -> None:
    lrclib_service = service({"/get": (200, HIT)})

    first = lrclib.resolve(conn, SONG, fetch=True)
    again = lrclib.resolve(conn, SONG, fetch=False)  # even with lookups off: it is already known

    assert first["state"] == again["state"] == "found"
    assert again["synced"] == first["synced"] and again["plain"] is None
    assert lrclib_service.count("/get") == 1


def test_a_song_lrclib_does_not_have_is_remembered_for_two_weeks(service, conn) -> None:
    lrclib_service = service({"/get": (404, None), "/search": (200, [])})
    day = 86400.0

    assert lrclib.resolve(conn, SONG, fetch=True, now=1000.0)["state"] == "none"
    assert lrclib.resolve(conn, SONG, fetch=True, now=1000.0 + 13 * day)["state"] == "none"
    assert lrclib_service.count("/get") == 1, "asked once in thirteen days"

    lrclib_service.routes["/get"] = (200, HIT)
    assert lrclib.resolve(conn, SONG, fetch=True, now=1000.0 + 15 * day)["state"] == "found", "asked again after fourteen"


def test_pressing_look_up_asks_again_about_a_song_that_was_not_found(service, conn) -> None:
    lrclib_service = service({"/get": (404, None), "/search": (200, [])})
    assert lrclib.resolve(conn, SONG, fetch=True)["state"] == "none"

    lrclib_service.routes["/get"] = (200, HIT)

    assert lrclib.resolve(conn, SONG, fetch=True)["state"] == "none", "automatic lookups do not"
    assert lrclib.resolve(conn, SONG, fetch=True, force=True)["state"] == "found", "a person asking does"


def test_a_retagged_song_is_asked_about_again(service, conn) -> None:
    lrclib_service = service({"/get": (200, HIT)})
    lrclib.resolve(conn, SONG, fetch=True)

    lrclib.resolve(conn, {**SONG, "title": "Glacier (Remastered)"}, fetch=True)
    lrclib.resolve(conn, {**SONG, "duration": 245.0}, fetch=True)

    assert lrclib_service.count("/get") == 3
    assert [r["query"]["track_name"] for r in lrclib_service.requests] == ["Glacier", "Glacier (Remastered)", "Glacier"]


def test_with_lookups_off_nothing_is_asked_and_nothing_is_kept(service, conn) -> None:
    lrclib_service = service({"/get": (200, HIT)})

    assert lrclib.resolve(conn, SONG, fetch=False) == {"state": "off"}

    assert lrclib_service.requests == []
    assert conn.execute("SELECT COUNT(*) FROM lyrics_cache").fetchone()[0] == 0


def test_trouble_is_not_kept_and_is_not_retried_at_once(service, conn) -> None:
    lrclib_service = service({"/get": (503, {"message": "busy"})})

    first = lrclib.resolve(conn, SONG, fetch=True, now=100.0)
    soon = lrclib.resolve(conn, SONG, fetch=True, now=130.0)

    assert first["state"] == soon["state"] == "unreachable" and "busy" in first["message"]
    assert lrclib_service.count("/get") == 1, "within five minutes it does not ask again by itself"
    assert conn.execute("SELECT COUNT(*) FROM lyrics_cache").fetchone()[0] == 0

    lrclib_service.routes["/get"] = (200, HIT)
    assert lrclib.resolve(conn, SONG, fetch=True, now=130.0, force=True)["state"] == "found", "Look up does"
    assert lrclib.resolve(conn, SONG, fetch=True, now=500.0)["state"] == "found"
    assert lrclib_service.count("/get") == 2, "the failure and the one success; the success is kept"


def test_trouble_is_asked_about_again_by_itself_after_five_minutes(service, conn) -> None:
    lrclib_service = service({"/get": (503, None)})
    lrclib.resolve(conn, SONG, fetch=True, now=100.0)

    lrclib_service.routes["/get"] = (200, HIT)

    assert lrclib.resolve(conn, SONG, fetch=True, now=100.0 + lrclib.RETRY_AFTER_TROUBLE + 1)["state"] == "found"


def test_the_answers_live_in_the_database_and_go_with_the_song(service, conn) -> None:
    service({"/get": (200, HIT)})
    lrclib.resolve(conn, SONG, fetch=True)
    assert conn.execute("SELECT status, source_id FROM lyrics_cache WHERE track_id = 1").fetchone()[:] == ("found", 7)

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM tracks WHERE id = 1")

    assert conn.execute("SELECT COUNT(*) FROM lyrics_cache").fetchone()[0] == 0


def test_the_cache_table_is_created_by_the_migration(tmp_path: Path) -> None:
    conn = sqlite3.connect(connect(tmp_path / "x.db").execute("PRAGMA database_list").fetchone()[2])
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"lyrics_cache", "fresh_releases"} <= names
