"""What a Subsonic client writes down: stars, ratings, and what was listened to."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from musictoolkit.web.subsonic import catalog
from tests.subsonic_support import Rest, album_named, songs_of


ENVELOPE = {"status", "version", "type", "serverVersion", "openSubsonic"}


@pytest.fixture
def rest(web: SimpleNamespace) -> Rest:
    return Rest(web.client)


def starred(rest: Rest) -> list[str]:
    return sorted(song["title"] for song in rest.ok("getStarred2")["starred2"]["song"])


def numbers_in(web: SimpleNamespace, where: str = "1 = 1") -> list[int]:
    with web.ctx.db() as conn:
        return [row["id"] for row in conn.execute(f"SELECT id FROM tracks WHERE {where} ORDER BY id")]


# ------------------------------------------------------------------------------------------------------------------------------- stars


def test_a_song_is_starred_and_unstarred_and_the_app_sees_it_the_same(rest: Rest, web: SimpleNamespace) -> None:
    song = songs_of(rest, album_named(rest, "Back Roads")["id"])[0]
    assert set(rest.ok("star", id=song["id"])) == ENVELOPE  # (the answer has nothing in it but that it is done)
    assert starred(rest) == ["Gravel Dust"] and web.client.get(f"/api/tracks/{song['id']}").json()["favorite"] is True
    assert rest.ok("getSong", id=song["id"])["song"]["starred"].endswith("Z")
    rest.ok("unstar", id=song["id"])
    assert starred(rest) == [] and web.client.get(f"/api/tracks/{song['id']}").json()["favorite"] is False and "starred" not in rest.ok("getSong", id=song["id"])["song"]


def test_a_star_the_app_gave_is_a_star_of_the_client(rest: Rest, web: SimpleNamespace) -> None:
    number = songs_of(rest, album_named(rest, "Back Roads")["id"])[1]["id"]
    assert web.client.post("/api/tracks/bulk", json={"ids": [int(number)], "favorite": True}).status_code == 200
    assert starred(rest) == ["Porch Light"]


def test_an_album_or_an_artist_named_is_all_of_its_songs_whichever_way_it_is_named(rest: Rest) -> None:
    northern = album_named(rest, "Northern Lights")
    rest.ok("star", albumId=northern["id"])
    assert starred(rest) == ["Drift", "Glacier", "Polar Night"]
    rest.ok("unstar", id=northern["id"])  # (a client that browses by folder names an album with `id`)
    assert starred(rest) == []
    rest.ok("star", artistId=northern["artistId"])
    assert starred(rest) == ["Drift", "Glacier", "Polar Night", "Salt and Stone"]
    rest.ok("unstar", id=northern["artistId"])
    assert starred(rest) == []


def test_several_things_are_starred_in_one_call_and_one_named_twice_is_starred_once(rest: Rest) -> None:
    fern = songs_of(rest, album_named(rest, "Back Roads")["id"])
    quinn = album_named(rest, "Static Hearts")
    rest.ok("star", id=[fern[0]["id"], fern[0]["id"], fern[1]["id"]], albumId=[quinn["id"], quinn["id"]])
    assert len(starred(rest)) == 5


def test_a_name_that_is_nobodys_fails_the_whole_call_and_nothing_is_starred(rest: Rest) -> None:
    song = songs_of(rest, album_named(rest, "Back Roads")["id"])[0]
    for extra in ({"id": [song["id"], "999999"]}, {"id": song["id"], "albumId": "al-WyJ4IiwgInkiXQ"}, {"id": song["id"], "artistId": "ar-QQ"}, {"id": [song["id"], "pl-1"]}, {"id": [song["id"], "nope"]}):
        rest.fails("star", 70, **extra)
    assert starred(rest) == []


@pytest.mark.parametrize("extra", [{"albumId": "12"}, {"albumId": "ar-QQ"}, {"artistId": "12"}, {"artistId": "al-x"}])
def test_an_album_id_that_is_not_an_albums_and_an_artist_id_that_is_not_an_artists_are_not_found(rest: Rest, extra: dict) -> None:
    rest.fails("star", 70, **extra)
    rest.fails("unstar", 70, **extra)


def test_a_call_that_names_nothing_says_what_it_needs(rest: Rest) -> None:
    assert rest.fails("star", 10) == "Required parameter is missing: id, albumId or artistId"
    assert rest.fails("unstar", 10) == "Required parameter is missing: id, albumId or artistId"


def test_a_song_that_has_gone_from_the_library_cannot_be_starred(rest: Rest, web: SimpleNamespace) -> None:
    number = songs_of(rest, album_named(rest, "Back Roads")["id"])[0]["id"]
    with web.ctx.db() as conn:
        conn.execute("UPDATE tracks SET is_missing = 1 WHERE id = ?", (int(number),))
        conn.commit()
    rest.fails("star", 70, id=number)


def test_more_songs_than_one_statement_can_hold_are_all_starred(rest: Rest, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "CHUNK", 2)
    every = [song["id"] for song in rest.ok("search3", query="", songCount=500)["searchResult3"]["song"]]
    rest.ok("star", id=every)
    assert len(starred(rest)) == 9
    rest.ok("unstar", id=every)
    assert starred(rest) == []


# ----------------------------------------------------------------------------------------------------------------------------- ratings


@pytest.mark.parametrize("stars", [1, 2, 3, 4, 5])
def test_a_rating_is_kept_and_the_app_sees_it(rest: Rest, web: SimpleNamespace, stars: int) -> None:
    number = songs_of(rest, album_named(rest, "Back Roads")["id"])[0]["id"]
    rest.ok("setRating", id=number, rating=stars)
    assert rest.ok("getSong", id=number)["song"]["userRating"] == stars and web.client.get(f"/api/tracks/{number}").json()["rating"] == stars


def test_a_rating_of_nothing_takes_the_rating_away(rest: Rest, web: SimpleNamespace) -> None:
    number = songs_of(rest, album_named(rest, "Back Roads")["id"])[0]["id"]
    rest.ok("setRating", id=number, rating=4)
    rest.ok("setRating", id=number, rating=0)
    assert "userRating" not in rest.ok("getSong", id=number)["song"]
    with web.ctx.db() as conn:
        assert conn.execute("SELECT rating FROM tracks WHERE id = ?", (int(number),)).fetchone()["rating"] is None


@pytest.mark.parametrize("rating", [-1, 6, 100, "x", "2.5"])
def test_a_rating_that_is_not_from_0_to_5_is_an_error_and_changes_nothing(rest: Rest, rating: object) -> None:
    number = songs_of(rest, album_named(rest, "Back Roads")["id"])[0]["id"]
    rest.ok("setRating", id=number, rating=3)
    rest.fails("setRating", 0, id=number, rating=rating)
    assert rest.ok("getSong", id=number)["song"]["userRating"] == 3


def test_a_rating_is_of_a_song_that_is_there_and_is_said(rest: Rest) -> None:
    assert rest.fails("setRating", 10, id="1") == "Required parameter is missing: rating"
    assert rest.fails("setRating", 10, rating=3) == "Required parameter is missing: id"
    rest.fails("setRating", 70, id="999999", rating=3)
    rest.fails("setRating", 70, id=album_named(rest, "Back Roads")["id"], rating=3)  # (an album has no rating of its own: its songs have)


# ----------------------------------------------------------------------------------------------------------------------------- listens


class Scrobbler:
    """What ListenBrainz would be told, written down."""

    def __init__(self) -> None:
        self.woken = 0
        self.playing: list[dict] = []

    def wake(self) -> None:
        self.woken += 1

    def now_playing(self, track: dict) -> bool:
        self.playing.append(track)
        return True


def history(web: SimpleNamespace, number: str) -> list[dict]:
    with web.ctx.db() as conn:
        rows = conn.execute(
            "SELECT source, played_at_epoch, ms_played, raw_artist_name, raw_track_name, raw_album_name FROM play_history WHERE track_id = ? ORDER BY id", (int(number),)
        ).fetchall()
    return [dict(row) for row in rows]


def length_of(web: SimpleNamespace, number: str) -> int:
    with web.ctx.db() as conn:
        return round(conn.execute("SELECT duration_seconds FROM tracks WHERE id = ?", (int(number),)).fetchone()["duration_seconds"] * 1000)


def test_a_listen_goes_into_the_history_as_the_whole_song_with_the_names_kept_and_counts_as_a_play(rest: Rest, web: SimpleNamespace) -> None:
    song = songs_of(rest, album_named(rest, "Northern Lights")["id"])[1]
    before = time.time()
    rest.ok("scrobble", id=song["id"])
    (row,) = history(web, song["id"])
    assert (row["source"], row["raw_artist_name"], row["raw_track_name"], row["raw_album_name"]) == ("future_scrobble", "Aurora Vale", "Polar Night", "Northern Lights")
    assert row["ms_played"] == length_of(web, song["id"]) and before - 2 <= row["played_at_epoch"] <= time.time()
    assert rest.ok("getSong", id=song["id"])["song"]["playCount"] == 1


def test_the_time_a_client_gives_is_when_the_listen_began_and_is_written_in_seconds(rest: Rest, web: SimpleNamespace) -> None:
    song = songs_of(rest, album_named(rest, "Northern Lights")["id"])[0]
    rest.ok("scrobble", id=song["id"], time=1_700_000_000_999)
    assert [row["played_at_epoch"] for row in history(web, song["id"])] == [1_700_000_000]


def test_the_same_listen_sent_twice_is_one_listen_and_another_time_is_another(rest: Rest, web: SimpleNamespace) -> None:
    song = songs_of(rest, album_named(rest, "Northern Lights")["id"])[0]
    for sent in (1_700_000_000_000, 1_700_000_000_400, 1_700_000_000_000, 1_700_000_100_000):
        rest.ok("scrobble", id=song["id"], time=sent)
    assert [row["played_at_epoch"] for row in history(web, song["id"])] == [1_700_000_000, 1_700_000_100]
    rest.ok("scrobble", id=song["id"])
    rest.ok("scrobble", id=song["id"])  # (no time to tell it from the other: two listens, as the app's own player writes them)
    assert len(history(web, song["id"])) == 4


def test_several_songs_in_one_call_each_have_the_time_in_the_place_they_have_and_those_without_one_are_now(rest: Rest, web: SimpleNamespace) -> None:
    first, second, third = songs_of(rest, album_named(rest, "Northern Lights")["id"])
    rest.ok("scrobble", id=[first["id"], second["id"], third["id"]], time=[1_700_000_000_000, 1_700_000_500_000])
    assert [row["played_at_epoch"] for row in history(web, first["id"])] == [1_700_000_000]
    assert [row["played_at_epoch"] for row in history(web, second["id"])] == [1_700_000_500]
    assert abs(history(web, third["id"])[0]["played_at_epoch"] - time.time()) < 5


def test_a_song_that_is_only_playing_is_told_to_listenbrainz_and_not_written_in_the_history(rest: Rest, web: SimpleNamespace) -> None:
    web.ctx.scrobbler = Scrobbler()
    song = songs_of(rest, album_named(rest, "Northern Lights")["id"])[0]
    rest.ok("scrobble", id=song["id"], submission="false")
    assert history(web, song["id"]) == [] and web.ctx.scrobbler.woken == 0
    assert web.ctx.scrobbler.playing == [{"id": int(song["id"]), "artist": "Aurora Vale", "title": "Glacier", "album": "Northern Lights"}]
    rest.ok("scrobble", id=song["id"], submission="true")
    assert len(history(web, song["id"])) == 1 and web.ctx.scrobbler.woken == 1 and len(web.ctx.scrobbler.playing) == 1


def test_a_song_that_is_only_playing_is_not_looked_at_twice_for_the_same_time(rest: Rest, web: SimpleNamespace) -> None:
    web.ctx.scrobbler = Scrobbler()
    song = songs_of(rest, album_named(rest, "Northern Lights")["id"])[0]
    for _ in range(2):
        rest.ok("scrobble", id=song["id"], submission="false", time=1_700_000_000_000)
    assert len(web.ctx.scrobbler.playing) == 2 and history(web, song["id"]) == []


def test_nothing_is_written_when_any_song_of_the_call_is_not_there(rest: Rest, web: SimpleNamespace) -> None:
    song = songs_of(rest, album_named(rest, "Northern Lights")["id"])[0]
    rest.fails("scrobble", 70, id=[song["id"], "999999"])
    assert history(web, song["id"]) == []
    rest.fails("scrobble", 70, id=album_named(rest, "Northern Lights")["id"])


def test_a_listen_needs_a_song_and_a_time_that_is_a_number(rest: Rest, web: SimpleNamespace) -> None:
    assert rest.fails("scrobble", 10) == "Required parameter is missing: id"
    song = songs_of(rest, album_named(rest, "Northern Lights")["id"])[0]
    assert rest.fails("scrobble", 0, id=song["id"], time="yesterday") == "time is not a whole number of milliseconds"
    assert history(web, song["id"]) == []


def test_a_song_of_no_known_length_is_a_listen_of_no_time(rest: Rest, web: SimpleNamespace) -> None:
    song = songs_of(rest, album_named(rest, "Northern Lights")["id"])[0]
    with web.ctx.db() as conn:
        conn.execute("UPDATE tracks SET duration_seconds = NULL WHERE id = ?", (int(song["id"]),))
        conn.commit()
    rest.ok("scrobble", id=song["id"])
    assert history(web, song["id"])[0]["ms_played"] == 0


# ------------------------------------------------------------------------------------------------------------------------------- the words


def test_what_is_not_found_is_said_with_what_it_is(rest: Rest) -> None:
    song = songs_of(rest, album_named(rest, "Back Roads")["id"])[0]["id"]
    assert rest.fails("star", 70, id="999999") == "The requested song was not found"
    assert rest.fails("star", 70, id=song, albumId="al-WyJ4IiwgInkiXQ") == "The requested album was not found"
    assert rest.fails("star", 70, id=song, artistId="ar-QQ") == "The requested artist was not found"
    assert rest.fails("star", 70, id="al-WyJ4IiwgInkiXQ") == "The requested album was not found"
    assert rest.fails("star", 70, id="ar-QQ") == "The requested artist was not found"
    assert rest.fails("star", 70, id="pl-1") == "The requested data was not found"
    assert rest.fails("star", 70, albumId="12") == "The requested album was not found"
    assert rest.fails("star", 70, artistId="12") == "The requested artist was not found"
    assert rest.fails("setRating", 70, id="999999", rating=3) == "The requested song was not found"
    assert rest.fails("setRating", 70, id="al-x", rating=3) == "The requested song was not found"
    assert rest.fails("scrobble", 70, id="999999") == "The requested song was not found"
    assert rest.fails("setRating", 0, id=song, rating=9) == "rating is from 1 to 5, or 0 to take it away"
