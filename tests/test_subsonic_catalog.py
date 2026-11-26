"""How the library is told in the words of the protocol: times, the names that a record may lack, the index of artists, and what a song is called when its tags do not say."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from musictoolkit.web.queries import UNKNOWN_ALBUM, UNKNOWN_ARTIST
from musictoolkit.web.subsonic import catalog
from tests.subsonic_support import Rest, add_songs, album_named, songs_of

# ----------------------------------------------------------------------------------------------------------------------------------- times


@pytest.mark.parametrize("given,written", [
    (None, None), ("", None), ("yesterday", None), ("2024-13-45T00:00:00", None), (10**20, None), (-(10**20), None),
    ("2024-01-01T10:00:00", "2024-01-01T10:00:00.000Z"),  # (no zone: it was written in UTC)
    ("2024-01-01T10:00:00+00:00", "2024-01-01T10:00:00.000Z"), ("2024-01-01T10:00:00Z", "2024-01-01T10:00:00.000Z"),
    ("2024-01-01T10:00:00+02:00", "2024-01-01T08:00:00.000Z"), ("2024-01-01T01:00:00-05:30", "2024-01-01T06:30:00.000Z"),
    ("2024-01-01T10:00:00.123456+00:00", "2024-01-01T10:00:00.123Z"), ("2024-01-01 10:00:00", "2024-01-01T10:00:00.000Z"),
    (0, "1970-01-01T00:00:00.000Z"), (1_700_000_000, "2023-11-14T22:13:20.000Z"), (1_700_000_000.5, "2023-11-14T22:13:20.500Z"), (-1, "1969-12-31T23:59:59.000Z"),
])
def test_a_time_is_written_in_utc_to_the_millisecond_with_a_z_and_what_is_no_time_is_none(given: object, written: str | None) -> None:
    assert catalog.iso(given) == written


def test_the_songs_and_albums_say_when_they_were_added_and_the_newest_album_comes_first(web: SimpleNamespace) -> None:
    rest = Rest(web.client)
    add_songs(web, [
        {"file_path": "/old/1.mp3", "title": "Old 1", "artist": "Dated", "album_artist": "Dated", "album": "Old", "date_added": "2020-02-03T04:05:06+00:00"},
        {"file_path": "/old/2.mp3", "title": "Old 2", "artist": "Dated", "album_artist": "Dated", "album": "Old", "date_added": "2020-03-04T05:06:07+00:00"},
        {"file_path": "/new/1.mp3", "title": "New 1", "artist": "Dated", "album_artist": "Dated", "album": "New", "date_added": "2030-01-01T00:00:00+00:00"},
    ])
    old = album_named(rest, "Old")
    assert old["created"] == "2020-02-03T04:05:06.000Z" and [song["created"] for song in songs_of(rest, old["id"])] == ["2020-02-03T04:05:06.000Z", "2020-03-04T05:06:07.000Z"]
    assert album_named(rest, "New")["created"] == "2030-01-01T00:00:00.000Z"
    newest = [album["name"] for album in rest.ok("getAlbumList2", type="newest", size=10)["albumList2"]["album"]]
    assert newest[0] == "New" and newest[-1] == "Old" and len(newest) == 6  # (the four of the library were added today: between the two)


# -------------------------------------------------------------------------------------------------------------------------------- the paths


def test_a_path_is_given_from_the_library_folder_it_is_in_or_by_its_name() -> None:
    assert catalog._relative("/music/a/b.mp3", ["/other", "/music"]) == "a/b.mp3"
    assert catalog._relative("/music/a/b.mp3", ["/music/a", "/music"]) == "b.mp3"  # (the first folder it is in)
    assert catalog._relative("/elsewhere/a/b.mp3", ["/music"]) == "b.mp3" and catalog._relative("/x/y.mp3", []) == "y.mp3"


# --------------------------------------------------------------------------------------------------------------------------- what a record lacks


def test_a_song_whose_tags_do_not_say_who_or_what_is_given_the_names_that_stand_in(web: SimpleNamespace) -> None:
    rest = Rest(web.client)
    add_songs(web, [
        {"file_path": "/odd/only-artist.mp3", "title": "Only artist", "artist": "Solo", "album": "Odd"},
        {"file_path": "/odd/only-album-artist.mp3", "title": "Only album artist", "album_artist": "Band", "album": "Odd 2"},
        {"file_path": "/odd/nothing.mp3", "title": "Nothing"},
        {"file_path": "/odd/No Title.xyz", "artist": "Solo", "album": "Odd"},
        {"file_path": "/odd/no-extension", "title": "No extension", "artist": "Solo", "album": "Odd"},
    ])
    found = {song["path"]: song for song in rest.ok("search3", query="", songCount=500)["searchResult3"]["song"]}
    assert (found["only-artist.mp3"]["artist"], found["only-artist.mp3"]["displayAlbumArtist"], found["only-artist.mp3"]["album"]) == ("Solo", "Solo", "Odd")
    assert (found["only-album-artist.mp3"]["artist"], found["only-album-artist.mp3"]["displayAlbumArtist"]) == ("Band", "Band")
    assert (found["nothing.mp3"]["artist"], found["nothing.mp3"]["displayAlbumArtist"], found["nothing.mp3"]["album"]) == (UNKNOWN_ARTIST, UNKNOWN_ARTIST, UNKNOWN_ALBUM)
    assert found["No Title.xyz"]["title"] == "No Title" and found["No Title.xyz"]["suffix"] == "xyz" and found["No Title.xyz"]["contentType"] == "application/octet-stream"
    assert "suffix" not in found["no-extension"] and found["no-extension"]["contentType"] == "application/octet-stream"
    assert found["only-artist.mp3"]["artistId"] == found["only-artist.mp3"]["artistId"] and found["nothing.mp3"]["parent"].startswith("al-")


def test_the_rows_of_the_library_that_have_no_length_are_zero_long(web: SimpleNamespace) -> None:
    rest = Rest(web.client)
    add_songs(web, [
        {"file_path": "/odd/long.mp3", "title": "Long", "artist": "L", "album_artist": "L", "album": "L", "duration_seconds": None, "bitrate": 320000},
        {"file_path": "/odd/fast.mp3", "title": "Fast", "artist": "F", "album_artist": "F", "album": "F", "bitrate": 1_000_000},
        {"file_path": "/odd/unknown.mp3", "title": "Unknown", "artist": "U", "album_artist": "U", "album": "U"},
    ])
    song = songs_of(rest, album_named(rest, "L")["id"])[0]
    assert (song["duration"], song["bitRate"]) == (0, 320)
    assert album_named(rest, "L")["duration"] == 0
    assert songs_of(rest, album_named(rest, "F")["id"])[0]["bitRate"] == 1000 and "bitRate" not in songs_of(rest, album_named(rest, "U")["id"])[0]


# ----------------------------------------------------------------------------------------------------------------------------- the index


@pytest.mark.parametrize("name,letter", [
    ("The Beatles", "B"), ("the beatles", "B"), ("Los Lobos", "L"), ("El Cuarteto", "C"), ("La Roux", "R"), ("Le Tigre", "T"), ("Las Ketchup", "K"), ("Les Rita Mitsouko", "R"),
    ("A Tribe Called Quest", "A"), ("The", "T"), ("The ", "T"), ("Theory", "T"), ("Elbow", "E"), ("2Pac", "#"), ("!!!", "#"), ("", "#"), ("  ", "#"), ("élan", "É"), ("名前", "名"),
])
def test_an_artist_is_under_the_letter_it_starts_with_once_a_leading_article_is_left_off(name: str, letter: str) -> None:
    assert catalog.index_letter(name) == letter


def test_the_artists_are_in_sections_by_letter_with_the_others_last_and_in_order_inside_each() -> None:
    entries = [{"name": name} for name in ("Zed", "The Band", "2Pac", "alpha", "Bravo", "!!!", "The Alpha", "El Dorado")]
    assert [(section["name"], [entry["name"] for entry in section["artist"]]) for section in catalog.indexed(entries)] == [
        ("A", ["alpha", "The Alpha"]), ("B", ["The Band", "Bravo"]), ("D", ["El Dorado"]), ("Z", ["Zed"]), ("#", ["!!!", "2Pac"])]
    assert catalog.indexed([], "child") == [] and catalog.indexed([{"name": "x"}], "child") == [{"name": "X", "child": [{"name": "x"}]}]


def test_a_library_with_an_artist_that_starts_with_a_digit_has_a_section_for_it_after_the_letters(web: SimpleNamespace) -> None:
    add_songs(web, [("/odd/1.mp3", "Digit", "2Pac", "All Eyez", 1)])
    assert [section["name"] for section in Rest(web.client).ok("getArtists")["artists"]["index"]] == ["A", "F", "M", "#"]
