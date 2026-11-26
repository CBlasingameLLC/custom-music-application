"""The playlists of a Subsonic client: those that are made by hand can be changed from here; those that fill themselves from rules can be listened to."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from tests.subsonic_support import Rest

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
ENVELOPE = {"status", "version", "type", "serverVersion", "openSubsonic"}


@pytest.fixture
def rest(web: SimpleNamespace) -> Rest:
    return Rest(web.client)


@pytest.fixture
def songs(rest: Rest) -> dict[str, dict]:
    every = rest.ok("search3", query="", songCount=500)["searchResult3"]["song"]
    return {song["title"]: song for song in every}


def entries(answer: dict) -> list[str]:
    return [song["title"] for song in answer["playlist"].get("entry", [])]


def smart(web: SimpleNamespace, name: str = "Folk", genre: str = "Folk") -> str:
    made = web.client.post("/api/playlists", json={"name": name, "kind": "smart", "rules": {"match": "all", "rules": [{"field": "genre", "op": "is", "value": genre}]}}).json()
    return f"pl-{made['id']}"


# ---------------------------------------------------------------------------------------------------------------------------------- making


def test_a_playlist_is_made_with_a_name_and_songs_and_comes_back_with_them_in_order(rest: Rest, web: SimpleNamespace, songs: dict) -> None:
    ordered = [songs["Wires"]["id"], songs["Glacier"]["id"], songs["Drift"]["id"]]
    answer = rest.ok("createPlaylist", name="My mix", songId=ordered)
    playlist = answer["playlist"]
    assert entries(answer) == ["Wires", "Glacier", "Drift"] and [song["id"] for song in playlist["entry"]] == ordered
    assert re.fullmatch(r"pl-\d+", playlist["id"]) and playlist["name"] == "My mix" and playlist["songCount"] == 3 and playlist["coverArt"] == playlist["id"]
    assert playlist["owner"] == "tester" and playlist["public"] is False and "readonly" not in playlist and "comment" not in playlist
    assert ISO.match(playlist["created"]) and ISO.match(playlist["changed"])
    with web.ctx.db() as conn:
        total = conn.execute(f"SELECT SUM(duration_seconds) AS total FROM tracks WHERE id IN ({', '.join(ordered)})").fetchone()["total"]
    assert playlist["duration"] == round(total)


def test_the_same_song_may_be_in_a_playlist_more_than_once(rest: Rest, songs: dict) -> None:
    answer = rest.ok("createPlaylist", name="Twice", songId=[songs["Glacier"]["id"], songs["Glacier"]["id"], songs["Drift"]["id"]])
    assert entries(answer) == ["Glacier", "Glacier", "Drift"] and answer["playlist"]["songCount"] == 3


def test_a_playlist_with_no_songs_is_a_playlist(rest: Rest) -> None:
    answer = rest.ok("createPlaylist", name="Empty")
    assert answer["playlist"]["songCount"] == 0 and answer["playlist"]["duration"] == 0 and answer["playlist"]["entry"] == []


def test_the_name_is_trimmed_and_may_be_any_text_up_to_200_letters(rest: Rest) -> None:
    assert rest.ok("createPlaylist", name="  é名前 \U0001f600  ")["playlist"]["name"] == "é名前 \U0001f600"
    assert rest.ok("createPlaylist", name="x" * 200)["playlist"]["name"] == "x" * 200
    assert rest.fails("createPlaylist", 0, name="x" * 201) == "A playlist's name can be at most 200 characters"


@pytest.mark.parametrize("name", [None, "", "   "])
def test_a_playlist_needs_a_name(rest: Rest, name: str | None) -> None:
    assert rest.fails("createPlaylist", 10, name=name) == "Required parameter is missing: name"
    assert rest.ok("getPlaylists")["playlists"] == {"playlist": []}


def test_a_song_that_is_not_there_fails_the_call_and_makes_no_playlist(rest: Rest, songs: dict) -> None:
    for given in ("999999", "al-WyJ4IiwgInkiXQ", "nope"):
        rest.fails("createPlaylist", 70, name="Bad", songId=[songs["Glacier"]["id"], given])
    assert rest.ok("getPlaylists")["playlists"] == {"playlist": []}


def test_the_app_sees_what_a_client_made(rest: Rest, web: SimpleNamespace, songs: dict) -> None:
    rest.ok("createPlaylist", name="Shared", songId=[songs["Glacier"]["id"], songs["Drift"]["id"]])
    (listed,) = web.client.get("/api/playlists").json()["items"]
    assert (listed["name"], listed["kind"], listed["source"], listed["tracks"]) == ("Shared", "manual", "manual", 2)


# --------------------------------------------------------------------------------------------------------------------------------- replacing


def test_a_playlist_that_is_made_again_with_its_id_has_the_new_songs_and_keeps_its_name(rest: Rest, songs: dict) -> None:
    made = rest.ok("createPlaylist", name="Keep", songId=[songs["Glacier"]["id"], songs["Drift"]["id"]])["playlist"]
    again = rest.ok("createPlaylist", playlistId=made["id"], songId=[songs["Wires"]["id"]])
    assert entries(again) == ["Wires"] and again["playlist"]["id"] == made["id"] and again["playlist"]["name"] == "Keep"
    renamed = rest.ok("createPlaylist", playlistId=made["id"], name="Renamed", songId=[songs["Neon Rain"]["id"], songs["Wires"]["id"]])
    assert entries(renamed) == ["Neon Rain", "Wires"] and renamed["playlist"]["name"] == "Renamed"
    assert len(rest.ok("getPlaylists")["playlists"]["playlist"]) == 1
    assert entries(rest.ok("createPlaylist", playlistId=made["id"])) == []


def test_a_playlist_that_is_not_there_cannot_be_made_again_and_a_smart_one_is_not_to_be_changed(rest: Rest, web: SimpleNamespace, songs: dict) -> None:
    rest.fails("createPlaylist", 70, playlistId="pl-9999", songId=songs["Glacier"]["id"])
    rest.fails("createPlaylist", 70, playlistId="12", songId=songs["Glacier"]["id"])
    given = smart(web)
    assert "smart playlist" in rest.fails("createPlaylist", 50, playlistId=given, songId=songs["Glacier"]["id"])
    assert entries(rest.ok("getPlaylist", id=given)) == ["Gravel Dust", "Porch Light"]


# ---------------------------------------------------------------------------------------------------------------------------------- reading


def test_the_playlists_are_in_the_order_of_their_names_whatever_the_case(rest: Rest, songs: dict) -> None:
    for name in ("banana", "Apple", "cherry", "apple"):
        rest.ok("createPlaylist", name=name, songId=songs["Glacier"]["id"])
    assert [p["name"] for p in rest.ok("getPlaylists")["playlists"]["playlist"]] == ["Apple", "apple", "banana", "cherry"]


def test_a_list_of_playlists_has_the_counts_and_not_the_songs(rest: Rest, songs: dict) -> None:
    rest.ok("createPlaylist", name="Counted", songId=[songs["Glacier"]["id"], songs["Drift"]["id"]])
    (playlist,) = rest.ok("getPlaylists")["playlists"]["playlist"]
    assert playlist["songCount"] == 2 and "entry" not in playlist and playlist["owner"] == "tester"


def test_a_smart_playlist_is_listed_as_one_that_cannot_be_changed_with_the_songs_its_rules_pick(rest: Rest, web: SimpleNamespace) -> None:
    given = smart(web)
    (playlist,) = rest.ok("getPlaylists")["playlists"]["playlist"]
    assert playlist["id"] == given and playlist["readonly"] is True and playlist["songCount"] == 2
    answer = rest.ok("getPlaylist", id=given)
    assert entries(answer) == ["Gravel Dust", "Porch Light"] and answer["playlist"]["readonly"] is True


def test_only_the_songs_that_can_be_played_are_in_a_playlist_and_counted(rest: Rest, web: SimpleNamespace, songs: dict) -> None:
    made = rest.ok("createPlaylist", name="Some gone", songId=[songs["Glacier"]["id"], songs["Drift"]["id"], songs["Wires"]["id"]])["playlist"]
    with web.ctx.db() as conn:
        conn.execute("UPDATE tracks SET is_missing = 1 WHERE id = ?", (int(songs["Drift"]["id"]),))
        conn.execute("UPDATE tracks SET no_audio = 1 WHERE id = ?", (int(songs["Wires"]["id"]),))
        conn.commit()
    answer = rest.ok("getPlaylist", id=made["id"])
    assert entries(answer) == ["Glacier"] and answer["playlist"]["songCount"] == 1
    assert rest.ok("getPlaylists")["playlists"]["playlist"][0]["songCount"] == 1


@pytest.mark.parametrize("given", ["pl-9999", "12", "al-x", "ar-QQ", "nope", "pl-x"])
def test_a_playlist_that_is_not_there_is_not_found(rest: Rest, given: str) -> None:
    rest.fails("getPlaylist", 70, id=given)
    rest.fails("deletePlaylist", 70, id=given)
    rest.fails("updatePlaylist", 70, playlistId=given, name="x")


def test_a_call_that_needs_an_id_says_so(rest: Rest) -> None:
    assert rest.fails("getPlaylist", 10) == "Required parameter is missing: id"
    assert rest.fails("deletePlaylist", 10) == "Required parameter is missing: id"
    assert rest.fails("updatePlaylist", 10) == "Required parameter is missing: playlistId"


# ---------------------------------------------------------------------------------------------------------------------------------- changing


def make(rest: Rest, songs: dict, *titles: str, name: str = "Edit") -> str:
    return rest.ok("createPlaylist", name=name, songId=[songs[title]["id"] for title in titles])["playlist"]["id"]


def test_a_playlist_is_renamed_and_the_comment_and_publicity_are_taken_and_not_kept(rest: Rest, songs: dict) -> None:
    given = make(rest, songs, "Glacier")
    assert set(rest.ok("updatePlaylist", playlistId=given, name="Better", comment="words", public="true")) == ENVELOPE
    answer = rest.ok("getPlaylist", id=given)["playlist"]
    assert answer["name"] == "Better" and "comment" not in answer and answer["public"] is False


def test_songs_are_added_at_the_end_in_the_order_given(rest: Rest, songs: dict) -> None:
    given = make(rest, songs, "Glacier")
    rest.ok("updatePlaylist", playlistId=given, songIdToAdd=[songs["Wires"]["id"], songs["Drift"]["id"], songs["Wires"]["id"]])
    assert entries(rest.ok("getPlaylist", id=given)) == ["Glacier", "Wires", "Drift", "Wires"]


def test_songs_are_taken_out_by_the_place_they_have_in_the_list_the_client_was_shown(rest: Rest, songs: dict) -> None:
    given = make(rest, songs, "Glacier", "Drift", "Wires", "Neon Rain")
    rest.ok("updatePlaylist", playlistId=given, songIndexToRemove=[0, 2])
    assert entries(rest.ok("getPlaylist", id=given)) == ["Drift", "Neon Rain"]


def test_what_is_taken_out_goes_before_what_is_put_in_so_the_places_are_those_of_the_old_list(rest: Rest, songs: dict) -> None:
    given = make(rest, songs, "Glacier", "Drift")
    rest.ok("updatePlaylist", playlistId=given, songIndexToRemove=1, songIdToAdd=[songs["Wires"]["id"], songs["Drift"]["id"]], name="Both")
    answer = rest.ok("getPlaylist", id=given)
    assert entries(answer) == ["Glacier", "Wires", "Drift"] and answer["playlist"]["name"] == "Both"


def test_a_place_that_is_not_in_the_list_is_no_song_to_take_out(rest: Rest, songs: dict) -> None:
    given = make(rest, songs, "Glacier", "Drift")
    rest.ok("updatePlaylist", playlistId=given, songIndexToRemove=[5, -1, 99999])
    assert entries(rest.ok("getPlaylist", id=given)) == ["Glacier", "Drift"]
    assert rest.fails("updatePlaylist", 0, playlistId=given, songIndexToRemove="first") == "songIndexToRemove is not a whole number"
    assert entries(rest.ok("getPlaylist", id=given)) == ["Glacier", "Drift"]


def test_a_song_that_is_the_same_as_another_in_the_list_is_taken_out_by_its_place_not_by_what_it_is(rest: Rest, songs: dict) -> None:
    given = make(rest, songs, "Glacier", "Drift", "Glacier", "Drift")
    rest.ok("updatePlaylist", playlistId=given, songIndexToRemove=2)
    assert entries(rest.ok("getPlaylist", id=given)) == ["Glacier", "Drift", "Drift"]


def test_the_places_are_those_of_the_songs_that_can_be_played_not_those_of_every_song_the_playlist_remembers(rest: Rest, web: SimpleNamespace, songs: dict) -> None:
    given = make(rest, songs, "Glacier", "Drift", "Wires", "Neon Rain")
    with web.ctx.db() as conn:
        conn.execute("UPDATE tracks SET is_missing = 1 WHERE id = ?", (int(songs["Glacier"]["id"]),))  # (not in the list the client was shown)
        conn.commit()
    assert entries(rest.ok("getPlaylist", id=given)) == ["Drift", "Wires", "Neon Rain"]
    rest.ok("updatePlaylist", playlistId=given, songIndexToRemove=0)
    assert entries(rest.ok("getPlaylist", id=given)) == ["Wires", "Neon Rain"]
    with web.ctx.db() as conn:
        kept = conn.execute("SELECT t.title FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id WHERE pt.playlist_id = ? ORDER BY pt.position", (int(given[3:]),)).fetchall()
    assert [row["title"] for row in kept] == ["Glacier", "Wires", "Neon Rain"]  # (the one that is away is still there for when it comes back)


def test_a_song_that_is_not_there_fails_the_call_and_changes_nothing(rest: Rest, songs: dict) -> None:
    given = make(rest, songs, "Glacier", "Drift")
    rest.fails("updatePlaylist", 70, playlistId=given, songIdToAdd=[songs["Wires"]["id"], "999999"], songIndexToRemove=0, name="Changed")
    answer = rest.ok("getPlaylist", id=given)
    assert entries(answer) == ["Glacier", "Drift"] and answer["playlist"]["name"] == "Edit"


def test_a_name_that_is_not_one_fails_the_call_and_changes_nothing(rest: Rest, songs: dict) -> None:
    given = make(rest, songs, "Glacier", "Drift")
    rest.fails("updatePlaylist", 0, playlistId=given, name="x" * 201, songIndexToRemove=0)
    rest.fails("updatePlaylist", 10, playlistId=given, name="  ", songIndexToRemove=0)
    assert entries(rest.ok("getPlaylist", id=given)) == ["Glacier", "Drift"]


def test_the_time_of_the_last_change_moves_when_a_playlist_is_changed(rest: Rest, web: SimpleNamespace, songs: dict) -> None:
    given = make(rest, songs, "Glacier")
    with web.ctx.db() as conn:
        conn.execute("UPDATE playlists SET created_at = '2020-01-01T00:00:00+00:00', updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (int(given[3:]),))
        conn.commit()
    before = rest.ok("getPlaylist", id=given)["playlist"]
    assert (before["created"], before["changed"]) == ("2020-01-01T00:00:00.000Z", "2020-01-01T00:00:00.000Z")
    rest.ok("updatePlaylist", playlistId=given, songIdToAdd=songs["Drift"]["id"])
    after = rest.ok("getPlaylist", id=given)["playlist"]
    assert after["created"] == "2020-01-01T00:00:00.000Z" and ISO.match(after["changed"]) and after["changed"] != after["created"]
    rest.ok("updatePlaylist", playlistId=given, name="Again")
    with web.ctx.db() as conn:
        conn.execute("UPDATE playlists SET updated_at = NULL WHERE id = ?", (int(given[3:]),))
        conn.commit()
    assert rest.ok("getPlaylist", id=given)["playlist"]["changed"] == "2020-01-01T00:00:00.000Z"  # (never changed: it is as old as it is)


def test_a_smart_playlist_can_be_renamed_and_its_songs_cannot_be_changed(rest: Rest, web: SimpleNamespace, songs: dict) -> None:
    given = smart(web)
    assert "smart playlist" in rest.fails("updatePlaylist", 50, playlistId=given, songIdToAdd=songs["Glacier"]["id"])
    assert "smart playlist" in rest.fails("updatePlaylist", 50, playlistId=given, songIndexToRemove=0)
    assert "smart playlist" in rest.fails("updatePlaylist", 50, playlistId=given, songIndexToRemove=0, name="Changed")
    rest.ok("updatePlaylist", playlistId=given, name="Renamed")
    answer = rest.ok("getPlaylist", id=given)
    assert answer["playlist"]["name"] == "Renamed" and entries(answer) == ["Gravel Dust", "Porch Light"]


# ---------------------------------------------------------------------------------------------------------------------------------- deleting


def test_a_playlist_is_deleted_with_its_songs_listed_and_the_songs_themselves_stay(rest: Rest, web: SimpleNamespace, songs: dict) -> None:
    given = make(rest, songs, "Glacier", "Drift")
    other = make(rest, songs, "Wires", name="Other")
    assert set(rest.ok("deletePlaylist", id=given)) == ENVELOPE
    assert [p["name"] for p in rest.ok("getPlaylists")["playlists"]["playlist"]] == ["Other"]
    with web.ctx.db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM playlist_tracks WHERE playlist_id = ?", (int(given[3:]),)).fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM playlist_tracks WHERE playlist_id = ?", (int(other[3:]),)).fetchone()["n"] == 1
    assert len(rest.ok("search3", query="", songCount=500)["searchResult3"]["song"]) == 9
    rest.fails("getPlaylist", 70, id=given)


def test_a_smart_playlist_can_be_deleted_and_the_songs_its_rules_picked_stay(rest: Rest, web: SimpleNamespace) -> None:
    given = smart(web)
    rest.ok("deletePlaylist", id=given)
    assert rest.ok("getPlaylists")["playlists"] == {"playlist": []} and len(rest.ok("getRandomSongs", genre="folk")["randomSongs"]["song"]) == 2
