"""A real client of the protocol (the py-sonic library, which phone and desktop apps are cousins of) using the server over a real connection: signing in the two ways it
knows, looking through the library, playing a song, a cover, starring, rating, listening, making a playlist, and what it is told when something is wrong."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import libsonic
import pytest
from libsonic import errors

from tests.subsonic_support import live, scanned_web

TOKEN = "k" * 8 + "T0ken" + "z" * 30
GLACIER = Path("Aurora Vale") / "Northern Lights" / "01 - Glacier.mp3"


@pytest.fixture
def served(tmp_path: Path, music_dir: Path) -> Iterator[SimpleNamespace]:
    web = scanned_web(tmp_path, music_dir, token=TOKEN)
    with live(web.app) as port:
        yield SimpleNamespace(web=web, port=port, music=music_dir)


def connect(served: SimpleNamespace, password: str = TOKEN, *, legacy: bool = False, user: str = "me", version: str = "1.16.1") -> libsonic.Connection:
    return libsonic.Connection("http://127.0.0.1", user, password, port=served.port, appName="tests", apiVersion=version, legacyAuth=legacy)


@pytest.fixture
def app(served: SimpleNamespace) -> libsonic.Connection:
    return connect(served)


def album_of(app: libsonic.Connection, name: str) -> dict:
    return next(album for album in app.getAlbumList2("alphabeticalByName", size=50)["albumList2"]["album"] if album["name"] == name)


def song_titled(app: libsonic.Connection, album: str, title: str) -> dict:
    return next(song for song in app.getAlbum(album_of(app, album)["id"])["album"]["song"] if song["title"] == title)


# ------------------------------------------------------------------------------------------------------------------------------ signing in


@pytest.mark.parametrize("legacy", [False, True], ids=["salted token", "password"])
def test_a_client_signs_in_with_the_token_either_way_it_knows(served: SimpleNamespace, legacy: bool) -> None:
    app = connect(served, legacy=legacy)
    assert app.ping() is True
    assert app.getLicense()["license"]["valid"] is True and app.getUser("me")["user"]["username"] == "me"


@pytest.mark.parametrize("legacy", [False, True], ids=["salted token", "password"])
def test_a_client_with_a_wrong_password_is_told_so_in_the_way_the_library_knows(served: SimpleNamespace, legacy: bool) -> None:
    with pytest.raises(errors.CredentialError):
        connect(served, "wrong", legacy=legacy).ping()


def test_a_client_that_speaks_a_newer_version_is_told_the_server_must_upgrade(served: SimpleNamespace) -> None:
    with pytest.raises((errors.VersionError, errors.ArgumentError)):
        connect(served, version="2.0.0").ping()


# ------------------------------------------------------------------------------------------------------------------------- looking through


def test_the_library_is_browsed_from_the_artists_to_a_song(app: libsonic.Connection) -> None:
    sections = app.getArtists()["artists"]["index"]
    assert [section["name"] for section in sections] == ["A", "F", "M"]
    artist = app.getArtist(sections[0]["artist"][0]["id"])["artist"]
    assert artist["name"] == "Aurora Vale" and [album["name"] for album in artist["album"]] == ["Northern Lights", "Low Tide"]
    album = app.getAlbum(artist["album"][0]["id"])["album"]
    assert [song["title"] for song in album["song"]] == ["Glacier", "Polar Night", "Drift"]
    assert app.getSong(album["song"][0]["id"])["song"]["path"] == "Aurora Vale/Northern Lights/01 - Glacier.mp3"
    assert [genre["value"] for genre in app.getGenres()["genres"]["genre"]] == ["Ambient", "Electronic", "Folk", "Indie Rock"]
    assert app.getMusicDirectory(artist["id"])["directory"]["name"] == "Aurora Vale"


def test_the_library_is_searched_and_asked_for_songs_at_random(app: libsonic.Connection) -> None:
    found = app.search3("neon")["searchResult3"]
    assert [song["title"] for song in found["song"]] == ["Neon Rain"] and [artist["name"] for artist in found["artist"]] == ["Mara Quinn"]
    assert len(app.getRandomSongs(size=4)["randomSongs"]["song"]) == 4
    assert [album["name"] for album in app.getAlbumList2("byYear", fromYear=2019, toYear=2022)["albumList2"]["album"]] == ["Northern Lights", "Static Hearts", "Low Tide"]


# ---------------------------------------------------------------------------------------------------------------------------- the file and cover


def test_a_song_is_streamed_downloaded_and_a_part_of_it_asked_for(served: SimpleNamespace, app: libsonic.Connection) -> None:
    data = (served.music / GLACIER).read_bytes()
    song = song_titled(app, "Northern Lights", "Glacier")
    assert app.stream(song["id"]).read() == data and app.download(song["id"]).read() == data
    assert app.stream(song["id"], maxBitRate=64, tformat="mp3").read() == data  # (not converted: it is the file)


def test_a_cover_is_given_in_the_size_asked_for_and_a_song_without_one_is_not_found(app: libsonic.Connection) -> None:
    cover = app.getCoverArt(album_of(app, "Northern Lights")["id"], size=64).read()
    assert cover[:3] == b"\xff\xd8\xff" and 0 < len(cover) < 5000
    with pytest.raises(errors.DataNotFoundError):
        app.getCoverArt(album_of(app, "Back Roads")["id"])


def test_a_song_that_is_not_there_is_not_found_whichever_way_it_is_asked_for(app: libsonic.Connection) -> None:
    with pytest.raises(errors.DataNotFoundError):
        app.getSong("999999")
    with pytest.raises(errors.DataNotFoundError):
        app.stream("999999")
    with pytest.raises(errors.DataNotFoundError):
        app.download("999999")
    with pytest.raises(errors.ParameterError):
        app.getMusicDirectory("")


# ------------------------------------------------------------------------------------------------------------------------ stars, ratings, listens


def test_a_client_stars_and_rates_and_listens_and_the_library_remembers(served: SimpleNamespace, app: libsonic.Connection) -> None:
    glacier, drift = song_titled(app, "Northern Lights", "Glacier"), song_titled(app, "Northern Lights", "Drift")
    app.star(sids=[glacier["id"]], albumIds=[album_of(app, "Back Roads")["id"]])
    assert sorted(song["title"] for song in app.getStarred2()["starred2"]["song"]) == ["Glacier", "Gravel Dust", "Porch Light"]
    assert [album["name"] for album in app.getStarred2()["starred2"]["album"]] == ["Back Roads"]
    app.unstar(sids=[glacier["id"]])
    assert sorted(song["title"] for song in app.getStarred2()["starred2"]["song"]) == ["Gravel Dust", "Porch Light"]
    app.setRating(drift["id"], 4)
    assert app.getSong(drift["id"])["song"]["userRating"] == 4
    app.setRating(drift["id"], 0)
    assert "userRating" not in app.getSong(drift["id"])["song"]
    app.scrobble(drift["id"], submission=False)  # (playing now)
    app.scrobble(drift["id"], listenTime=1_700_000_000)
    app.scrobble(drift["id"], listenTime=1_700_000_000)  # (sent again: the same listen)
    assert app.getSong(drift["id"])["song"]["playCount"] == 1
    assert [album["name"] for album in app.getAlbumList2("frequent")["albumList2"]["album"]] == ["Northern Lights"]
    with served.web.ctx.db() as conn:
        assert conn.execute("SELECT played_at_epoch FROM play_history WHERE track_id = ?", (int(drift["id"]),)).fetchall()[0][0] == 1_700_000_000


# --------------------------------------------------------------------------------------------------------------------------------- playlists


def test_a_playlist_is_made_changed_looked_at_and_deleted(app: libsonic.Connection) -> None:
    glacier, drift, wires = (song_titled(app, "Northern Lights", "Glacier"), song_titled(app, "Northern Lights", "Drift"), song_titled(app, "Static Hearts", "Wires"))
    made = app.createPlaylist(name="Road trip", songIds=[glacier["id"], drift["id"]])["playlist"]
    assert made["name"] == "Road trip" and [song["title"] for song in made["entry"]] == ["Glacier", "Drift"]
    app.updatePlaylist(made["id"], name="Road trip 2", songIdsToAdd=[wires["id"]], songIndexesToRemove=[0])
    again = app.getPlaylist(made["id"])["playlist"]
    assert again["name"] == "Road trip 2" and [song["title"] for song in again["entry"]] == ["Drift", "Wires"] and again["songCount"] == 2
    assert [playlist["name"] for playlist in app.getPlaylists()["playlists"]["playlist"]] == ["Road trip 2"]
    replaced = app.createPlaylist(playlistId=made["id"], songIds=[wires["id"]])["playlist"]
    assert [song["title"] for song in replaced["entry"]] == ["Wires"]
    app.deletePlaylist(made["id"])
    assert app.getPlaylists()["playlists"] == {} or app.getPlaylists()["playlists"].get("playlist") in (None, [])
    with pytest.raises(errors.DataNotFoundError):
        app.getPlaylist(made["id"])


# ----------------------------------------------------------------------------------------------------------------------- lyrics and the scan


def test_the_lyrics_of_a_song_are_asked_for_by_its_artist_and_title(served: SimpleNamespace, app: libsonic.Connection) -> None:
    (served.music / GLACIER).with_suffix(".lrc").write_text("[00:01.00]Line one\n[00:03.00]Line two\n", encoding="utf-8")
    assert app.getLyrics("Aurora Vale", "Glacier")["lyrics"]["value"] == "Line one\nLine two"


def test_a_scan_is_asked_for_and_its_state_is_asked_after(served: SimpleNamespace, app: libsonic.Connection) -> None:
    assert app.getScanStatus()["scanStatus"]["scanning"] is False
    app.startScan()
    served.web.ctx.jobs.wait(served.web.ctx.jobs.list()[-1].id, timeout=60)
    assert app.getScanStatus()["scanStatus"] == {"scanning": False, "count": 9}
