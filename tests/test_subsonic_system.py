"""The calls about the server itself: that it is there, what it adds to the protocol, the scan, the stations, and what it does not do."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from musictoolkit import __version__
from musictoolkit.ingest import scanner
from musictoolkit.web.subsonic.dispatch import PUBLIC, REGISTRY
from tests.conftest import make_track
from tests.subsonic_support import Rest, album_named, scanned_web, songs_of


@pytest.fixture
def rest(web: SimpleNamespace) -> Rest:
    return Rest(web.client)


# ------------------------------------------------------------------------------------------------------------------------ who is there


def test_the_music_folder_is_the_one_library(rest: Rest) -> None:
    assert rest.ok("getMusicFolders")["musicFolders"] == {"musicFolder": [{"id": 1, "name": "Music"}]}


def test_the_user_is_the_person_who_may_do_everything_there_is_but_run_the_server(rest: Rest) -> None:
    assert rest.ok("getUser", username="tester")["user"] == {
        "username": "tester", "scrobblingEnabled": True, "adminRole": False, "settingsRole": False, "downloadRole": True, "uploadRole": False, "playlistRole": True,
        "coverArtRole": True, "commentRole": False, "podcastRole": False, "streamRole": True, "jukeboxRole": False, "shareRole": False, "videoConversionRole": False, "folder": [1],
    }


def test_the_user_asked_about_is_the_user_asked_about_and_no_name_is_the_one_signed_in_as(rest: Rest) -> None:
    assert rest.ok("getUser", username="somebody else")["user"]["username"] == "somebody else"  # (there is one person: whoever is asked after is her)
    assert rest.ok("getUser")["user"]["username"] == "tester"


def test_the_server_says_what_it_is_in_every_answer(rest: Rest) -> None:
    answer = rest.ok("ping")
    assert (answer["version"], answer["type"], answer["serverVersion"], answer["openSubsonic"]) == ("1.16.1", "musictoolkit", __version__, True)


def test_the_license_is_valid_because_some_clients_will_not_go_on_without_one(rest: Rest) -> None:
    assert rest.ok("getLicense")["license"] == {"valid": True, "email": "", "licenseExpires": "2099-12-31T23:59:59.000Z"}


def test_the_extensions_are_the_ones_that_are_there_to_use_each_in_its_first_version(rest: Rest) -> None:
    listed = rest.ok("getOpenSubsonicExtensions")["openSubsonicExtensions"]
    assert {item["name"] for item in listed} == {"apiKeyAuthentication", "formPost", "songLyrics"} and all(item["versions"] == [1] for item in listed)


def test_an_api_key_says_whose_it_is(web: SimpleNamespace) -> None:
    assert Rest(web.client).ok("tokenInfo")["tokenInfo"] == {"username": "tester"}


def test_the_key_of_a_server_with_a_token_is_that_token_and_it_says_the_name_the_client_gave_or_one_of_ours(tmp_path: Path, music_dir: Path) -> None:
    secure = scanned_web(tmp_path, music_dir, token="t" * 40)
    assert Rest(secure.client, password="t" * 40, auth="key").ok("tokenInfo")["tokenInfo"] == {"username": "mtk"}
    assert Rest(secure.client, password="t" * 40, user="phone").ok("tokenInfo")["tokenInfo"] == {"username": "phone"}


def test_no_call_that_needs_no_password_is_there_but_the_extensions() -> None:
    assert PUBLIC == {"getOpenSubsonicExtensions"} and PUBLIC <= set(REGISTRY)


# --------------------------------------------------------------------------------------------------------------------------------- the scan


class Blocked:
    """A scan that does not finish until it is let: to look at the server while it is scanning."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.started, self.release, self.calls = threading.Event(), threading.Event(), 0
        original = scanner.scan_library

        def scan(conn, root, on_progress=None, **kwargs):
            self.calls += 1
            on_progress(3, 9)
            self.started.set()
            assert self.release.wait(30)
            return original(conn, root, on_progress=on_progress, **kwargs)

        monkeypatch.setattr(scanner, "scan_library", scan)


def test_when_nothing_is_being_scanned_the_status_says_so_and_how_many_files_there_are(rest: Rest) -> None:
    assert rest.ok("getScanStatus")["scanStatus"] == {"scanning": False, "count": 9}


def test_a_scan_that_is_asked_for_runs_and_the_status_says_so_with_how_far_it_has_come(rest: Rest, web: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = Blocked(monkeypatch)
    assert rest.ok("startScan")["scanStatus"]["scanning"] is True
    assert blocked.started.wait(30)
    assert rest.ok("getScanStatus")["scanStatus"] == {"scanning": True, "count": 3}
    blocked.release.set()
    web.ctx.jobs.wait(web.ctx.jobs.list()[-1].id, timeout=60)
    assert rest.ok("getScanStatus")["scanStatus"] == {"scanning": False, "count": 9}


def test_a_scan_that_is_asked_for_while_one_runs_is_the_one_that_runs(rest: Rest, web: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = Blocked(monkeypatch)
    rest.ok("startScan")
    assert blocked.started.wait(30)
    assert rest.ok("startScan")["scanStatus"]["scanning"] is True
    blocked.release.set()
    web.ctx.jobs.wait(web.ctx.jobs.list()[-1].id, timeout=60)
    assert blocked.calls == 1 and len([job for job in web.ctx.jobs.list() if job.kind == "scan"]) == 2  # (the one the fixture ran, and this one)


def test_a_scan_finds_the_songs_that_were_added_since(rest: Rest, web: SimpleNamespace, music_dir: Path) -> None:
    make_track(music_dir / "Newcomer" / "Fresh", "01 - First.mp3", title="First", artist="Newcomer", albumartist="Newcomer", album="Fresh", tracknumber=1, genre="Pop")
    rest.ok("startScan")
    web.ctx.jobs.wait(web.ctx.jobs.list()[-1].id, timeout=60)
    assert [song["title"] for song in songs_of(rest, album_named(rest, "Fresh")["id"])] == ["First"] and rest.ok("getScanStatus")["scanStatus"]["count"] == 10


def test_a_scan_cannot_be_asked_for_in_a_library_with_no_folder(empty_web: SimpleNamespace) -> None:
    rest = Rest(empty_web.client)
    assert rest.fails("startScan", 0) == "Add a music folder first"
    assert rest.ok("getScanStatus")["scanStatus"] == {"scanning": False, "count": 0}


# ------------------------------------------------------------------------------------------------------------------------------ the stations


def keep(web: SimpleNamespace, name: str, url: str, homepage: str | None = None) -> None:
    response = web.client.post("/api/radio/stations/save", json={"name": name, "stream_url": url, "source": "manual", "homepage": homepage})
    assert response.status_code == 201, response.text


def test_the_stations_the_person_keeps_are_given_with_their_own_addresses(rest: Rest, web: SimpleNamespace) -> None:
    assert rest.ok("getInternetRadioStations")["internetRadioStations"] == {"internetRadioStation": []}
    keep(web, "Jazz FM", "https://stream.example.com/jazz", "https://jazz.example.com/")
    keep(web, "Plain", "http://stream.example.com:8000/plain")
    stations = rest.ok("getInternetRadioStations")["internetRadioStations"]["internetRadioStation"]
    assert sorted((s["name"], s["streamUrl"], s.get("homePageUrl")) for s in stations) == [
        ("Jazz FM", "https://stream.example.com/jazz", "https://jazz.example.com/"), ("Plain", "http://stream.example.com:8000/plain", None)]
    assert all(s["id"].isdigit() for s in stations)


# --------------------------------------------------------------------------------------------------------------------- what there is none of


@pytest.mark.parametrize("name,key", [("getBookmarks", "bookmarks"), ("getShares", "shares"), ("getChatMessages", "chatMessages")])
def test_what_this_server_has_none_of_is_answered_with_none(rest: Rest, name: str, key: str) -> None:
    assert rest.ok(name)[key] == {}


@pytest.mark.parametrize("name", [
    "savePlayQueue", "getPlayQueue", "createBookmark", "deleteBookmark", "createShare", "updateShare", "deleteShare", "addChatMessage", "getPodcasts", "getNewestPodcasts",
    "createPodcastChannel", "jukeboxControl", "getVideos", "getVideoInfo", "createInternetRadioStation", "updateInternetRadioStation", "deleteInternetRadioStation",
    "getUsers", "createUser", "updateUser", "deleteUser", "changePassword", "getAvatar", "hls", "getCaptions",
])
def test_what_this_server_does_not_do_is_said_in_a_failure_that_names_it(rest: Rest, name: str) -> None:
    assert rest.fails(name, 0) == f"This server does not do {name}"


def test_the_calls_this_server_does_are_these() -> None:
    """A call is added or taken away on purpose: this list, and the conformance test that has to ask each of them, are changed with it."""
    assert sorted(REGISTRY) == [
        "createPlaylist", "deletePlaylist", "download", "getAlbum", "getAlbumInfo", "getAlbumInfo2", "getAlbumList", "getAlbumList2", "getArtist", "getArtistInfo",
        "getArtistInfo2", "getArtists", "getBookmarks", "getChatMessages", "getCoverArt", "getGenres", "getIndexes", "getInternetRadioStations", "getLicense", "getLyrics",
        "getLyricsBySongId", "getMusicDirectory", "getMusicFolders", "getNowPlaying", "getOpenSubsonicExtensions", "getPlaylist", "getPlaylists", "getRandomSongs",
        "getScanStatus", "getShares", "getSimilarSongs", "getSimilarSongs2", "getSong", "getSongsByGenre", "getStarred", "getStarred2", "getTopSongs", "getUser", "ping",
        "scrobble", "search2", "search3", "setRating", "star", "startScan", "stream", "tokenInfo", "unstar", "updatePlaylist",
    ]
