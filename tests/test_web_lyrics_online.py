"""Lyrics from lrclib.net through the API: off until asked for, kept in the app's database, never written next to the music."""

from __future__ import annotations

from pathlib import Path

import pytest

from musictoolkit.media import lrclib
from tests.conftest import ids_by_title
from tests.fake_services import serve

HIT = {"id": 7, "trackName": "Wires", "artistName": "Mara Quinn", "albumName": "Static Hearts", "duration": 12.0,
       "instrumental": False, "plainLyrics": "Sparks\nand wire", "syncedLyrics": "[00:01.00]Sparks\n[00:04.00]and wire\n"}


@pytest.fixture
def lrclib_service(monkeypatch):
    lrclib._trouble.clear()
    with serve({"/get": (200, HIT)}) as service:
        monkeypatch.setattr(lrclib, "BASE_URL", service.url)
        yield service
    lrclib._trouble.clear()


def lyrics(web, title: str, method: str = "get") -> dict:
    track_id = ids_by_title(web.client)[title]
    path = f"/api/tracks/{track_id}/lyrics" + ("/lookup" if method == "post" else "")
    response = getattr(web.client, method)(path)
    assert response.status_code == 200, response.text
    return response.json()


def turn_on(web) -> None:
    assert web.client.put("/api/settings", json={"app": {"lyrics_lrclib": True}}).status_code == 200


def test_nothing_is_asked_online_until_the_person_turns_it_on(web, lrclib_service) -> None:
    answer = lyrics(web, "Wires")

    assert answer == {"source": None, "synced": None, "plain": None, "instrumental": False, "online": "off"}
    assert lrclib_service.requests == []


def test_with_lookups_on_a_song_without_lyrics_gets_them_and_they_are_kept(web, lrclib_service) -> None:
    turn_on(web)

    first = lyrics(web, "Wires")
    again = lyrics(web, "Wires")

    assert first["online"] == "found" and first["source"] == "lrclib"
    assert first["synced"] == [{"t": 1.0, "text": "Sparks"}, {"t": 4.0, "text": "and wire"}] and first["plain"] is None
    assert again == first
    assert lrclib_service.count("/get") == 1
    request = lrclib_service.requests[0]["query"]
    assert (request["artist_name"], request["track_name"], request["album_name"]) == ("Mara Quinn", "Wires", "Static Hearts")


def test_nothing_is_written_next_to_the_music(web, music_dir, lrclib_service) -> None:
    turn_on(web)
    before = sorted(p.name for p in music_dir.rglob("*"))

    lyrics(web, "Wires")

    assert sorted(p.name for p in music_dir.rglob("*")) == before, "no .lrc file, no tag change"
    with web.ctx.db() as conn:
        assert conn.execute("SELECT status FROM lyrics_cache").fetchall()[0]["status"] == "found"


def test_pressing_look_up_works_with_automatic_lookups_off(web, lrclib_service) -> None:
    assert lyrics(web, "Wires")["online"] == "off"

    pressed = lyrics(web, "Wires", method="post")

    assert pressed["online"] == "found" and pressed["source"] == "lrclib"
    assert lyrics(web, "Wires")["online"] == "found", "and it is kept, so the song shows them from then on"
    assert lrclib_service.count("/get") == 1


def test_a_song_that_has_lyrics_of_its_own_is_never_looked_up(web, music_dir, lrclib_service) -> None:
    turn_on(web)
    track_id = ids_by_title(web.client)["Wires"]
    song = Path(web.client.get(f"/api/tracks/{track_id}").json()["path"])
    song.with_suffix(".lrc").write_text("[00:02.00]Mine\n", encoding="utf-8")

    for method in ("get", "post"):
        answer = lyrics(web, "Wires", method)
        assert answer["source"] == "lrc-file" and answer["synced"] == [{"t": 2.0, "text": "Mine"}] and answer["online"] is None

    assert lrclib_service.requests == []


def test_a_song_lrclib_does_not_have_says_so_and_is_not_asked_again_at_once(web, lrclib_service) -> None:
    turn_on(web)
    lrclib_service.routes["/get"] = (404, None)
    lrclib_service.routes["/search"] = (200, [])

    first = lyrics(web, "Wires")
    lyrics(web, "Wires")

    assert first == {"source": None, "synced": None, "plain": None, "instrumental": False, "online": "none"}
    assert lrclib_service.count("/get") == 1


def test_trouble_is_reported_as_unreachable_with_a_reason_and_is_not_an_error_response(web, lrclib_service) -> None:
    turn_on(web)
    lrclib_service.routes["/get"] = (503, {"message": "busy"})

    answer = lyrics(web, "Wires")

    assert answer["online"] == "unreachable" and "busy" in answer["message"] and answer["source"] is None

    lrclib_service.routes["/get"] = (200, HIT)
    assert lyrics(web, "Wires", method="post")["online"] == "found", "Look up tries again at once"


def test_an_instrumental_is_reported_as_one(web, lrclib_service) -> None:
    turn_on(web)
    lrclib_service.routes["/get"] = (200, {"id": 3, "instrumental": True, "plainLyrics": None, "syncedLyrics": None})

    answer = lyrics(web, "Wires")

    assert answer["online"] == "found" and answer["instrumental"] is True and answer["synced"] is None and answer["plain"] is None


def test_a_song_that_is_not_in_the_library_is_still_a_404(web, lrclib_service) -> None:
    turn_on(web)

    assert web.client.get("/api/tracks/424242/lyrics").status_code == 404
    assert web.client.post("/api/tracks/424242/lyrics/lookup").status_code == 404
    assert lrclib_service.requests == []
