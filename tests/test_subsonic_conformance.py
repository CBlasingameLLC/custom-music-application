"""That what the server answers is what the protocol says: every call checked against the schemas of the OpenSubsonic project, in JSON, and the XML checked to say what the
JSON says (the XML is what most clients ask for; its own schema is not part of the tests, so it is held to the JSON's)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from musictoolkit.web.subsonic.dispatch import PUBLIC, REGISTRY
from tests.conftest import build_music_dir
from tests.subsonic_schema import FAILURE, problems, schema_of
from tests.subsonic_support import Rest, album_named, canonical_json, canonical_xml, scanned_web, songs_of

BINARY = {"stream", "download", "getCoverArt"}  # (these answer with the file; they answer with the JSON of a failure when they cannot)


@pytest.fixture(scope="module")
def library(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """A library with a little of everything in it: lyrics (timed and not), stars, a rating, listens, playlists (made and smart), a station."""
    music = build_music_dir(tmp_path_factory.mktemp("music"))
    web = scanned_web(tmp_path_factory.mktemp("app"), music)
    rest = Rest(web.client)
    northern = album_named(rest, "Northern Lights")
    glacier, polar, drift = songs_of(rest, northern["id"])
    (music / "Aurora Vale" / "Northern Lights" / "01 - Glacier.lrc").write_text("[00:01.00]First\n[00:05.00]Second\n", encoding="utf-8")
    (music / "Aurora Vale" / "Northern Lights" / "03 - Drift.lrc").write_text("just words\nand more\n", encoding="utf-8")
    rest.ok("star", id=glacier["id"])
    rest.ok("setRating", id=polar["id"], rating=4)
    rest.ok("scrobble", id=drift["id"], time=1_700_000_000_000)
    made = rest.ok("createPlaylist", name="Made", songId=[glacier["id"], drift["id"]])["playlist"]
    smart = web.client.post("/api/playlists", json={"name": "Folk", "kind": "smart", "rules": {"match": "all", "rules": [{"field": "genre", "op": "is", "value": "Folk"}]}}).json()
    web.client.post("/api/radio/stations/save", json={"name": "Jazz FM", "stream_url": "https://stream.example.com/jazz", "source": "manual", "homepage": "https://jazz.example.com/"})
    return SimpleNamespace(
        web=web, rest=rest, song=glacier["id"], plain=drift["id"], bare=polar["id"], album=northern["id"], artist=northern["artistId"], made=made["id"], smart=f"pl-{smart['id']}", music=music,
    )


KINDS = ("random", "newest", "frequent", "recent", "starred", "alphabeticalByName", "alphabeticalByArtist")
# Every call that only looks, asked in the ways a client asks it. A value in braces is filled in from the library (its ids are known only once it is scanned).
READS: list[tuple[str, dict[str, Any]]] = [
    ("ping", {}), ("getLicense", {}), ("getOpenSubsonicExtensions", {}), ("tokenInfo", {}), ("getMusicFolders", {}), ("getUser", {"username": "tester"}),
    ("getArtists", {}), ("getIndexes", {}), ("getArtist", {"id": "{artist}"}), ("getAlbum", {"id": "{album}"}), ("getSong", {"id": "{song}"}),
    ("getMusicDirectory", {"id": "{artist}"}), ("getMusicDirectory", {"id": "{album}"}), ("getGenres", {}),
    *[("getAlbumList2", {"type": kind, "size": 5}) for kind in KINDS], *[("getAlbumList", {"type": kind, "size": 5}) for kind in KINDS],
    ("getAlbumList2", {"type": "byYear", "fromYear": 2000, "toYear": 2030}), ("getAlbumList2", {"type": "byGenre", "genre": "Folk"}),
    ("getAlbumList", {"type": "byYear", "fromYear": 2030, "toYear": 2000}),
    ("getRandomSongs", {"size": 3}), ("getRandomSongs", {"genre": "Folk"}), ("getSongsByGenre", {"genre": "Folk"}),
    ("getStarred2", {}), ("getStarred", {}), ("getNowPlaying", {}), ("getTopSongs", {"artist": "Aurora Vale"}), ("getTopSongs", {"artist": "Nobody"}),
    ("getArtistInfo", {"id": "{artist}"}), ("getArtistInfo2", {"id": "{artist}"}), ("getAlbumInfo", {"id": "{album}"}), ("getAlbumInfo2", {"id": "{album}"}),
    ("getSimilarSongs", {"id": "{song}"}), ("getSimilarSongs2", {"id": "{song}"}),
    ("search3", {"query": "a"}), ("search3", {"query": ""}), ("search3", {"query": "zzz"}), ("search2", {"query": "a"}), ("search2", {"query": ""}),
    ("getLyricsBySongId", {"id": "{song}"}), ("getLyricsBySongId", {"id": "{plain}"}), ("getLyricsBySongId", {"id": "{bare}"}),
    ("getLyrics", {"artist": "Aurora Vale", "title": "Glacier"}), ("getLyrics", {"artist": "Aurora Vale", "title": "Drift"}), ("getLyrics", {"title": "Nothing"}),
    ("getPlaylists", {}), ("getPlaylist", {"id": "{made}"}), ("getPlaylist", {"id": "{smart}"}),
    ("getScanStatus", {}), ("getInternetRadioStations", {}), ("getBookmarks", {}), ("getShares", {}), ("getChatMessages", {}),
]
CHANGES = ("star", "unstar", "setRating", "scrobble", "createPlaylist", "updatePlaylist", "deletePlaylist", "startScan")  # (asked in the test below)
CHANGES_EVERY_TIME = {"lastModified"}  # (what is true of the moment it is asked: the JSON and the XML of a call are two calls)


def filled(params: dict[str, Any], lib: SimpleNamespace) -> dict[str, Any]:
    return {key: value.format(**vars(lib)) if isinstance(value, str) else value for key, value in params.items()}


def without(value: Any, names: set[str]) -> Any:
    """The plain data of an answer, without what is true only of the moment it was asked."""
    if isinstance(value, dict):
        return {key: without(item, names) for key, item in value.items() if key not in names}
    if isinstance(value, list):
        return [without(item, names) for item in value]
    return value


def label(case: tuple[str, dict[str, Any]]) -> str:
    return case[0] + "?" + "&".join(f"{key}={value}" for key, value in case[1].items())


def test_every_call_the_server_does_is_asked_here(library: SimpleNamespace) -> None:
    asked = {name for name, _ in READS} | set(CHANGES) | BINARY
    assert asked >= set(REGISTRY), sorted(set(REGISTRY) - asked)


@pytest.mark.parametrize("case", READS, ids=label)
def test_the_json_of_each_call_is_what_the_schema_says_and_the_xml_says_the_same(library: SimpleNamespace, case: tuple[str, dict[str, Any]]) -> None:
    name, params = case[0], filled(case[1], library)
    answer = library.rest.call(name, **params)
    assert answer["status"] == "ok", answer
    reference = schema_of(name)
    assert reference is not None, f"the schemas have nothing for {name}"
    assert problems(reference, {"subsonic-response": answer}) == []
    if "random" not in (name.lower(), str(params.get("type"))) and "Random" not in name:  # (a random answer is not the same twice)
        in_xml = canonical_xml(library.rest.xml(name, **params))
        assert without(canonical_json(answer), CHANGES_EVERY_TIME) == without(in_xml, CHANGES_EVERY_TIME)


# What changes something is asked in turn, as a client does, each answer checked the same way.
def test_the_json_of_each_call_that_changes_something_is_what_the_schema_says(library: SimpleNamespace) -> None:
    rest, song, other = library.rest, library.song, library.plain
    steps: list[tuple[str, dict[str, Any]]] = [
        ("star", {"id": song}), ("star", {"albumId": library.album, "artistId": library.artist}), ("unstar", {"id": [song, other]}), ("unstar", {"albumId": library.album}),
        ("setRating", {"id": song, "rating": 5}), ("setRating", {"id": song, "rating": 0}),
        ("scrobble", {"id": song, "time": 1_700_000_001_000}), ("scrobble", {"id": [song, other], "submission": "false"}),
        ("createPlaylist", {"name": "Conformance", "songId": [song, other]}),
        ("updatePlaylist", {"playlistId": library.made, "name": "Renamed", "songIdToAdd": other, "songIndexToRemove": 0}),
        ("startScan", {}),
    ]
    for name, params in steps:
        answer = rest.call(name, **params)
        assert answer["status"] == "ok", (name, answer)
        reference = schema_of(name)
        assert reference is not None
        assert problems(reference, {"subsonic-response": answer}) == [], name
    created = rest.ok("getPlaylists")["playlists"]["playlist"]
    for item in created:
        if item["name"] == "Conformance":
            answer = rest.call("deletePlaylist", id=item["id"])
            assert answer["status"] == "ok" and problems(schema_of("deletePlaylist"), {"subsonic-response": answer}) == []
    library.web.ctx.jobs.wait(library.web.ctx.jobs.list()[-1].id, timeout=60)


@pytest.mark.parametrize("name,params", [
    ("getArtist", {"id": "ar-QQ"}), ("getAlbum", {"id": "al-x"}), ("getSong", {"id": "999999"}), ("getMusicDirectory", {"id": "12345"}), ("getPlaylist", {"id": "pl-9999"}),
    ("stream", {"id": "999999"}), ("download", {"id": "nope"}), ("getCoverArt", {"id": "999999"}), ("getLyricsBySongId", {"id": "999999"}), ("getLyrics", {}),
    ("star", {"id": "999999"}), ("star", {}), ("setRating", {"id": "1", "rating": 9}), ("scrobble", {"id": "999999"}), ("deletePlaylist", {"id": "pl-9999"}),
    ("updatePlaylist", {}), ("createPlaylist", {}), ("getAlbumList2", {"type": "bogus"}), ("getAlbumList2", {}), ("getTopSongs", {}), ("search3", {"songCount": "many"}),
    ("nonexistent", {}),
])
def test_a_failure_is_what_the_protocol_says_one_is_for_every_call(library: SimpleNamespace, name: str, params: dict) -> None:
    answer = library.rest.call(name, **params)
    assert answer["status"] == "failed" and isinstance(answer["error"]["code"], int) and answer["error"]["message"]
    assert problems(FAILURE, answer) == []
    reference = schema_of(name)
    if reference is not None:
        assert problems(reference, {"subsonic-response": answer}) == []
    xml = canonical_xml(library.rest.xml(name, **params))
    assert xml["status"] == "failed" and xml["error"] == [{"code": str(answer["error"]["code"]), "message": answer["error"]["message"]}]


def test_the_failures_of_who_is_asking_are_what_the_protocol_says_too(tmp_path: Path, music_dir: Path) -> None:
    web = scanned_web(tmp_path, music_dir, token="t" * 40)
    for kind, rest in (("plain", Rest(web.client, password="wrong")), ("key", Rest(web.client, password="wrong", auth="key")), ("none", Rest(web.client)), ("old", Rest(web.client, password="t" * 40, version="0.5.0"))):
        answer = rest.call("getSong", id="1")
        assert answer["status"] == "failed", kind
        assert problems(FAILURE, answer) == [], kind
    assert problems(schema_of("getOpenSubsonicExtensions"), {"subsonic-response": Rest(web.client).call("getOpenSubsonicExtensions")}) == []
    assert PUBLIC == {"getOpenSubsonicExtensions"}
