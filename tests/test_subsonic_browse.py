"""Looking through the library as a Subsonic client does: artists, albums, songs, lists, genres, starred, and search."""

from __future__ import annotations

import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import make_stub
from tests.subsonic_support import Rest, add_songs, album_named, scanned_web, songs_of

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
ALBUMS = ["Back Roads", "Low Tide", "Northern Lights", "Static Hearts"]


@pytest.fixture
def rest(web: SimpleNamespace) -> Rest:
    return Rest(web.client)


def names(items: list[dict]) -> list[str]:
    return [item["name"] for item in items]


def titles(items: list[dict]) -> list[str]:
    return [item["title"] for item in items]


# ----------------------------------------------------------------------------------------------------------------------------- the artists


def test_the_artists_are_in_sections_by_the_first_letter_with_the_article_left_off(rest: Rest) -> None:
    answer = rest.ok("getArtists")["artists"]
    assert answer["ignoredArticles"] == "The El La Los Las Le Les"
    assert [section["name"] for section in answer["index"]] == ["A", "F", "M"]
    by_name = {artist["name"]: artist for section in answer["index"] for artist in section["artist"]}
    assert set(by_name) == {"Aurora Vale", "The Fernwoods", "Mara Quinn"}
    assert [by_name[name]["albumCount"] for name in ("Aurora Vale", "The Fernwoods", "Mara Quinn")] == [2, 1, 1]
    for artist in by_name.values():
        assert artist["id"].startswith("ar-") and artist["coverArt"] == artist["id"]


def test_the_older_call_for_the_same_artists_has_no_album_counts_and_says_when_the_list_was_last_changed(rest: Rest) -> None:
    answer = rest.ok("getIndexes")["indexes"]
    assert [section["name"] for section in answer["index"]] == ["A", "F", "M"]
    assert answer["ignoredArticles"] == "The El La Los Las Le Les" and isinstance(answer["lastModified"], int) and abs(answer["lastModified"] - time.time() * 1000) < 60_000
    assert all(set(artist) == {"id", "name"} for section in answer["index"] for artist in section["artist"])


def test_an_artist_has_its_albums_by_year(rest: Rest) -> None:
    artist = rest.ok("getArtists")["artists"]["index"][0]["artist"][0]
    answer = rest.ok("getArtist", id=artist["id"])["artist"]
    assert (answer["id"], answer["name"], answer["albumCount"], answer["coverArt"]) == (artist["id"], "Aurora Vale", 2, artist["id"])
    assert [(album["name"], album["year"], album["songCount"], album["genre"]) for album in answer["album"]] == [
        ("Northern Lights", 2019, 3, "Electronic"), ("Low Tide", 2022, 1, "Ambient")]
    first = answer["album"][0]
    assert first["artist"] == "Aurora Vale" and first["artistId"] == artist["id"] and first["id"].startswith("al-") and first["coverArt"] == first["id"]
    assert first["duration"] >= 0 and first["playCount"] == 0 and ISO.match(first["created"]) and first["mediaType"] == "album" and first["displayArtist"] == "Aurora Vale"
    assert "starred" not in first and "played" not in first


@pytest.mark.parametrize("given,code", [("ar-" + "QQ", 70), ("12", 70), ("al-xx", 70), ("pl-1", 70), ("zzz", 70), ("ar-!!", 70)])
def test_an_artist_that_is_not_there_or_an_id_that_is_not_an_artists_is_not_found(rest: Rest, given: str, code: int) -> None:
    rest.fails("getArtist", code, id=given)


def test_what_is_not_found_is_said_with_what_it_is(rest: Rest) -> None:
    assert rest.fails("getArtist", 70, id="ar-QQ") == "The requested artist was not found"
    assert rest.fails("getArtist", 70, id="12") == "The requested artist was not found"
    assert rest.fails("getAlbum", 70, id="al-WyJ4IiwgInkiXQ") == "The requested album was not found"
    assert rest.fails("getAlbum", 70, id="12") == "The requested album was not found"
    assert rest.fails("getAlbum", 70, id="al-!!!") == "The requested album was not found"  # (not a key of an album at all)
    assert rest.fails("getSong", 70, id="999999") == "The requested song was not found"
    assert rest.fails("getSong", 70, id="al-x") == "The requested song was not found"
    assert rest.fails("getMusicDirectory", 70, id="ar-QQ") == "The requested artist was not found"
    assert rest.fails("getMusicDirectory", 70, id="al-WyJ4IiwgInkiXQ") == "The requested album was not found"


def test_a_call_that_needs_an_id_says_so_when_it_has_none(rest: Rest) -> None:
    for name in ("getArtist", "getAlbum", "getSong", "getMusicDirectory"):
        assert rest.fails(name, 10) == "Required parameter is missing: id"


# ------------------------------------------------------------------------------------------------------------------------ albums and songs


def test_an_album_has_its_songs_in_the_order_of_the_disc_with_everything_a_client_shows_of_them(rest: Rest, music_dir: Path) -> None:
    album = album_named(rest, "Northern Lights")
    answer = rest.ok("getAlbum", id=album["id"])["album"]
    assert answer["name"] == "Northern Lights" and answer["songCount"] == 3 and answer["year"] == 2019
    assert [(song["track"], song["title"]) for song in answer["song"]] == [(1, "Glacier"), (2, "Polar Night"), (3, "Drift")]
    song = answer["song"][0]
    assert song["id"].isdigit() and song["parent"] == answer["id"] and song["albumId"] == answer["id"] and song["coverArt"] == answer["id"]
    assert song["isDir"] is False and song["isVideo"] is False and song["type"] == "music" and song["mediaType"] == "song"
    assert (song["album"], song["artist"], song["displayArtist"], song["displayAlbumArtist"], song["genre"], song["year"]) == (
        "Northern Lights", "Aurora Vale", "Aurora Vale", "Aurora Vale", "Electronic", 2019)
    assert song["artistId"] == album["artistId"] and song["contentType"] == "audio/mpeg" and song["suffix"] == "mp3"
    assert song["path"] == "Aurora Vale/Northern Lights/01 - Glacier.mp3"
    assert song["size"] == (music_dir / "Aurora Vale" / "Northern Lights" / "01 - Glacier.mp3").stat().st_size
    assert isinstance(song["duration"], int) and ISO.match(song["created"]) and song["playCount"] == 0
    assert "starred" not in song and "userRating" not in song and "played" not in song


def test_a_song_by_itself_is_the_same_song_as_in_its_album(rest: Rest) -> None:
    song = songs_of(rest, album_named(rest, "Back Roads")["id"])[1]
    assert rest.ok("getSong", id=song["id"])["song"] == song and song["title"] == "Porch Light"


@pytest.mark.parametrize("given", ["999999", "al-WyJ4IiwgInkiXQ", "al-!!!", "ar-QQ", "pl-1", "abc"])
def test_an_album_or_a_song_that_is_not_there_is_not_found(rest: Rest, given: str) -> None:
    rest.fails("getAlbum", 70, id=given)
    rest.fails("getSong", 70, id=given)


def test_the_folders_of_the_older_clients_are_the_artist_with_its_albums_and_the_album_with_its_songs(rest: Rest) -> None:
    artist = next(a for section in rest.ok("getIndexes")["indexes"]["index"] for a in section["artist"] if a["name"] == "Aurora Vale")
    folder = rest.ok("getMusicDirectory", id=artist["id"])["directory"]
    assert (folder["id"], folder["name"]) == (artist["id"], "Aurora Vale") and "parent" not in folder
    assert [(child["title"], child["isDir"], child["parent"]) for child in folder["child"]] == [("Northern Lights", True, artist["id"]), ("Low Tide", True, artist["id"])]
    inside = rest.ok("getMusicDirectory", id=folder["child"][0]["id"])["directory"]
    assert (inside["name"], inside["parent"]) == ("Northern Lights", artist["id"]) and titles(inside["child"]) == ["Glacier", "Polar Night", "Drift"]
    assert all(child["isDir"] is False for child in inside["child"])


@pytest.mark.parametrize("given", ["1", "pl-1", "ar-QQ", "al-WyJ4IiwgInkiXQ", "nope"])
def test_a_folder_that_is_not_an_artist_or_an_album_that_is_there_is_not_found(rest: Rest, given: str) -> None:
    rest.fails("getMusicDirectory", 70, id=given)


# ------------------------------------------------------------------------------------------------------------------------------------ genres


def test_the_genres_with_how_many_songs_and_albums_each_has(rest: Rest) -> None:
    genres = rest.ok("getGenres")["genres"]["genre"]
    assert [(g["value"], g["songCount"], g["albumCount"]) for g in genres] == [("Ambient", 1, 1), ("Electronic", 3, 1), ("Folk", 2, 1), ("Indie Rock", 3, 1)]


def test_a_genre_that_is_spelt_two_ways_is_one_genre(tmp_path: Path, music_dir: Path) -> None:
    from tests.conftest import make_track

    make_track(music_dir / "Zed" / "Z", "01.mp3", title="Z1", artist="Zed", albumartist="Zed", album="Z", tracknumber=1, genre="FOLK")
    rest = Rest(scanned_web(tmp_path, music_dir).client)
    folk = [g for g in rest.ok("getGenres")["genres"]["genre"] if g["value"].lower() == "folk"]
    assert len(folk) == 1 and folk[0]["songCount"] == 3 and folk[0]["albumCount"] == 2


# ----------------------------------------------------------------------------------------------------------------------------------- lists


def lists(rest: Rest, kind: str, **extra: object) -> list[str]:
    return names(rest.ok("getAlbumList2", type=kind, **extra)["albumList2"]["album"])


def test_the_albums_by_name_and_by_artist(rest: Rest) -> None:
    assert lists(rest, "alphabeticalByName") == ALBUMS
    assert lists(rest, "alphabeticalByArtist") == ["Low Tide", "Northern Lights", "Static Hearts", "Back Roads"]  # (Aurora Vale, Mara Quinn, The Fernwoods)


def test_a_list_is_paged_by_size_and_offset_and_a_size_beyond_the_limits_is_brought_back_to_them(rest: Rest) -> None:
    assert lists(rest, "alphabeticalByName", size=2) == ALBUMS[:2]
    assert lists(rest, "alphabeticalByName", size=2, offset=2) == ALBUMS[2:]
    assert lists(rest, "alphabeticalByName", size=2, offset=3) == ALBUMS[3:]
    assert lists(rest, "alphabeticalByName", size=0) == ALBUMS[:1]  # (at least one)
    assert lists(rest, "alphabeticalByName", size=100000) == ALBUMS  # (at most 500)
    assert lists(rest, "alphabeticalByName", offset=-5, size=1) == ALBUMS[:1]
    assert len(lists(rest, "alphabeticalByName")) == 4  # (ten, when it is not said: there are four)


def test_the_newest_and_the_random_albums_are_all_of_them_in_some_order(rest: Rest) -> None:
    assert sorted(lists(rest, "newest")) == ALBUMS and sorted(lists(rest, "random")) == ALBUMS


def test_albums_by_year_go_from_the_first_year_to_the_last_and_back_when_the_years_are_given_the_other_way_round(rest: Rest) -> None:
    assert lists(rest, "byYear", fromYear=2019, toYear=2022) == ["Northern Lights", "Static Hearts", "Low Tide"]
    assert lists(rest, "byYear", fromYear=2022, toYear=2019) == ["Low Tide", "Static Hearts", "Northern Lights"]
    assert lists(rest, "byYear", fromYear=2015, toYear=2015) == ["Back Roads"]
    assert lists(rest, "byYear", fromYear=2016, toYear=2018) == []
    assert rest.fails("getAlbumList2", 10, type="byYear", fromYear=2000) == "Required parameter is missing: fromYear and toYear"
    assert rest.fails("getAlbumList2", 10, type="byYear", toYear=2000) == "Required parameter is missing: fromYear and toYear"


def test_albums_by_genre_do_not_mind_the_case(rest: Rest) -> None:
    assert lists(rest, "byGenre", genre="folk") == ["Back Roads"] and lists(rest, "byGenre", genre="INDIE ROCK") == ["Static Hearts"] and lists(rest, "byGenre", genre="jazz") == []
    assert rest.fails("getAlbumList2", 10, type="byGenre") == "Required parameter is missing: genre"


def test_the_frequent_and_recent_and_starred_albums_are_none_until_something_is_played_or_starred(rest: Rest) -> None:
    assert lists(rest, "frequent") == [] and lists(rest, "recent") == [] and lists(rest, "starred") == []
    song = songs_of(rest, album_named(rest, "Static Hearts")["id"])[0]
    rest.ok("scrobble", id=song["id"])
    assert lists(rest, "frequent") == ["Static Hearts"] and lists(rest, "recent") == ["Static Hearts"]
    other = album_named(rest, "Back Roads")
    rest.ok("star", albumId=other["id"])
    assert lists(rest, "starred") == ["Back Roads"]
    rest.ok("unstar", id=songs_of(rest, other["id"])[0]["id"])
    assert lists(rest, "starred") == []  # (an album is starred when every one of its songs is)


@pytest.mark.parametrize("place", [0, 1, 2])
def test_an_album_is_frequent_and_recent_whichever_of_its_songs_was_listened_to(rest: Rest, place: int) -> None:
    """(The total of an album is a total, not the figure of whichever of its songs the database happens to look at.)"""
    song = songs_of(rest, album_named(rest, "Northern Lights")["id"])[place]
    rest.ok("scrobble", id=song["id"])
    assert lists(rest, "frequent") == ["Northern Lights"] and lists(rest, "recent") == ["Northern Lights"]
    assert rest.ok("getAlbumList2", type="frequent")["albumList2"]["album"][0]["playCount"] == 1


def test_the_most_listened_to_album_comes_first_and_the_one_listened_to_last_comes_first_among_the_recent(rest: Rest) -> None:
    northern = songs_of(rest, album_named(rest, "Northern Lights")["id"])
    static = songs_of(rest, album_named(rest, "Static Hearts")["id"])
    rest.ok("scrobble", id=[northern[0]["id"], northern[1]["id"]], time=[1_700_000_000_000, 1_700_000_100_000])
    rest.ok("scrobble", id=static[2]["id"], time=1_700_000_200_000)
    assert lists(rest, "frequent") == ["Northern Lights", "Static Hearts"] and lists(rest, "recent") == ["Static Hearts", "Northern Lights"]
    assert [album["playCount"] for album in rest.ok("getAlbumList2", type="frequent")["albumList2"]["album"]] == [2, 1]
    assert rest.ok("getAlbumList2", type="recent")["albumList2"]["album"][0]["played"] == "2023-11-14T22:16:40.000Z"


def test_a_kind_of_list_that_is_not_one_or_is_not_said_is_an_error(rest: Rest) -> None:
    assert rest.fails("getAlbumList2", 0, type="bogus") == "'bogus' is not a kind of list of albums"
    rest.fails("getAlbumList2", 10)
    rest.fails("getAlbumList2", 0, type="newest", size="many")


def test_the_older_list_gives_folders_of_songs_where_the_newer_one_gives_albums(rest: Rest) -> None:
    older = rest.ok("getAlbumList", type="alphabeticalByName")["albumList"]["album"]
    assert titles(older) == ALBUMS and all(item["isDir"] is True and item["parent"].startswith("ar-") for item in older)
    assert "songCount" not in older[0]


def test_random_songs_are_as_many_as_asked_for_and_can_be_kept_to_a_genre_and_to_years(rest: Rest) -> None:
    assert len(rest.ok("getRandomSongs", size=3)["randomSongs"]["song"]) == 3 and len(rest.ok("getRandomSongs", size=0)["randomSongs"]["song"]) == 1
    assert len(rest.ok("getRandomSongs", size=500)["randomSongs"]["song"]) == 9
    assert sorted(titles(rest.ok("getRandomSongs", genre="folk")["randomSongs"]["song"])) == ["Gravel Dust", "Porch Light"]
    assert sorted(titles(rest.ok("getRandomSongs", fromYear=2021, toYear=2021)["randomSongs"]["song"])) == ["Neon Rain", "Signal Fade", "Wires"]
    assert sorted(titles(rest.ok("getRandomSongs", fromYear=2022)["randomSongs"]["song"])) == ["Salt and Stone"]
    assert sorted(titles(rest.ok("getRandomSongs", toYear=2015)["randomSongs"]["song"])) == ["Gravel Dust", "Porch Light"]
    assert rest.ok("getRandomSongs", genre="jazz")["randomSongs"] == {"song": []}


def test_the_songs_of_a_genre_are_paged(rest: Rest) -> None:
    every = titles(rest.ok("getSongsByGenre", genre="Electronic", count=50)["songsByGenre"]["song"])
    assert sorted(every) == ["Drift", "Glacier", "Polar Night"]
    assert titles(rest.ok("getSongsByGenre", genre="electronic", count=2, offset=1)["songsByGenre"]["song"]) == every[1:]
    assert titles(rest.ok("getSongsByGenre", genre="electronic", count=1, offset=1)["songsByGenre"]["song"]) == every[1:2]
    assert titles(rest.ok("getSongsByGenre", genre="electronic", count=0)["songsByGenre"]["song"]) == every[:1]  # (at least one)
    assert titles(rest.ok("getSongsByGenre", genre="electronic", count=2, offset=-4)["songsByGenre"]["song"]) == every[:2]  # (from the first)
    rest.fails("getSongsByGenre", 10)


def test_nothing_is_starred_and_nobody_is_listening_until_something_is(rest: Rest) -> None:
    assert rest.ok("getStarred2")["starred2"] == {"album": [], "song": []}
    assert rest.ok("getStarred")["starred"] == {"album": [], "song": []}
    assert rest.ok("getNowPlaying")["nowPlaying"] == {"entry": []}


def test_what_is_starred_is_listed_with_the_time_it_was_given_and_the_albums_that_are_whole(rest: Rest) -> None:
    album = album_named(rest, "Northern Lights")
    songs = songs_of(rest, album["id"])
    rest.ok("star", id=songs[0]["id"])
    answer = rest.ok("getStarred2")["starred2"]
    assert titles(answer["song"]) == ["Glacier"] and ISO.match(answer["song"][0]["starred"]) and answer["album"] == []
    rest.ok("star", id=songs[1]["id"], albumId=album["id"])
    answer = rest.ok("getStarred2")["starred2"]
    assert names(answer["album"]) == ["Northern Lights"] and ISO.match(answer["album"][0]["starred"]) and len(answer["song"]) == 3
    older = rest.ok("getStarred")["starred"]
    assert older["album"][0]["isDir"] is True and titles(older["album"]) == ["Northern Lights"]


def test_the_top_songs_of_an_artist_are_the_ones_listened_to_most(rest: Rest) -> None:
    songs = songs_of(rest, album_named(rest, "Northern Lights")["id"])
    for _ in range(2):
        rest.ok("scrobble", id=songs[2]["id"])
    rest.ok("scrobble", id=songs[1]["id"])
    top = rest.ok("getTopSongs", artist="Aurora Vale", count=2)["topSongs"]["song"]
    assert titles(top) == ["Drift", "Polar Night"] and [song["playCount"] for song in top] == [2, 1]
    assert rest.ok("getTopSongs", artist="Nobody")["topSongs"] == {"song": []}
    rest.fails("getTopSongs", 10)


def test_what_the_library_does_not_know_is_an_answer_with_nothing_in_it(rest: Rest) -> None:
    artist = rest.ok("getArtists")["artists"]["index"][0]["artist"][0]["id"]
    assert rest.ok("getArtistInfo", id=artist)["artistInfo"] == {} and rest.ok("getArtistInfo2", id=artist)["artistInfo2"] == {}
    assert rest.ok("getAlbumInfo", id=artist)["albumInfo"] == {} and rest.ok("getAlbumInfo2", id=artist)["albumInfo"] == {}
    assert rest.ok("getSimilarSongs", id=artist)["similarSongs"] == {} and rest.ok("getSimilarSongs2", id=artist)["similarSongs2"] == {}


# ---------------------------------------------------------------------------------------------------------------------------------- search


def test_a_search_finds_the_artists_albums_and_songs_that_have_every_word_somewhere(rest: Rest) -> None:
    answer = rest.ok("search3", query="aurora")["searchResult3"]
    assert names(answer["artist"]) == ["Aurora Vale"] and sorted(names(answer["album"])) == ["Low Tide", "Northern Lights"] and len(answer["song"]) == 4
    answer = rest.ok("search3", query="vale north")["searchResult3"]
    assert names(answer["album"]) == ["Northern Lights"] and len(answer["song"]) == 3
    answer = rest.ok("search3", query="NEON")["searchResult3"]
    assert titles(answer["song"]) == ["Neon Rain"] and names(answer["artist"]) == ["Mara Quinn"] and names(answer["album"]) == ["Static Hearts"]
    assert len(rest.ok("search3", query="indie rock 2021")["searchResult3"]["song"]) == 3
    assert rest.ok("search3", query="nothing like this")["searchResult3"] == {"artist": [], "album": [], "song": []}


def test_an_empty_search_is_everything_which_is_how_some_clients_read_the_whole_library(rest: Rest) -> None:
    answer = rest.ok("search3", query="", songCount=500, albumCount=500, artistCount=500)["searchResult3"]
    assert len(answer["song"]) == 9 and len(answer["album"]) == 4 and len(answer["artist"]) == 3
    assert len(rest.ok("search3", songCount=500)["searchResult3"]["song"]) == 9


def test_a_search_is_paged_for_each_kind_of_thing_on_its_own_and_a_kind_with_a_count_of_nothing_is_left_out(rest: Rest) -> None:
    answer = rest.ok("search3", query="", songCount=2, songOffset=1, albumCount=0, artistCount=1, artistOffset=1)["searchResult3"]
    assert len(answer["song"]) == 2 and "album" not in answer and names(answer["artist"]) == ["Mara Quinn"]  # (by name, whatever the article)
    answer = rest.ok("search3", query="", songCount=0, albumCount=1, albumOffset=3, artistCount=0)["searchResult3"]
    assert names(answer["album"]) == ["Static Hearts"] and set(answer) == {"album"}


def test_the_characters_of_a_pattern_are_only_characters_in_a_search(rest: Rest) -> None:
    for query in ("%", "_", "100%", "a_b", "\\", "'", '"', "a' OR '1'='1", "; DROP TABLE tracks; --"):
        assert rest.ok("search3", query=query)["searchResult3"] == {"artist": [], "album": [], "song": []}, query
    assert len(rest.ok("getRandomSongs", size=500)["randomSongs"]["song"]) == 9


def test_the_songs_that_are_found_are_the_most_listened_to_first(rest: Rest) -> None:
    found = rest.ok("search3", query="", songCount=500)["searchResult3"]["song"]
    by_title = {song["title"]: song["id"] for song in found}
    for title in ("Neon Rain", "Neon Rain", "Wires", "Neon Rain", "Wires", "Glacier"):
        rest.ok("scrobble", id=by_title[title])
    assert titles(rest.ok("search3", query="", songCount=500)["searchResult3"]["song"])[:3] == ["Neon Rain", "Wires", "Glacier"]


def test_the_older_search_gives_the_albums_as_folders_and_the_artists_as_names(rest: Rest) -> None:
    answer = rest.ok("search2", query="aurora")["searchResult2"]
    assert answer["artist"][0].keys() == {"id", "name"} and [item["isDir"] for item in answer["album"]] == [True, True] and len(answer["song"]) == 4


# ---------------------------------------------------------------------------------------------------------------- a file that is only tags


def test_a_file_that_is_only_tags_is_in_no_list_and_has_nothing_to_find(tmp_path: Path, music_dir: Path) -> None:
    stub = make_stub(music_dir / "Some Band" / "Some Album", title="Hidden Song", artist="Some Band", album="Some Album", genre="Hidden")
    web = scanned_web(tmp_path, music_dir)
    rest = Rest(web.client)
    assert stub.is_file()
    assert "Some Band" not in names([a for section in rest.ok("getArtists")["artists"]["index"] for a in section["artist"]])
    assert "Some Album" not in lists(rest, "alphabeticalByName", size=100) and "Hidden" not in [g["value"] for g in rest.ok("getGenres")["genres"]["genre"]]
    assert rest.ok("search3", query="hidden")["searchResult3"] == {"artist": [], "album": [], "song": []}
    assert len(rest.ok("search3", songCount=500)["searchResult3"]["song"]) == 9 and len(rest.ok("getRandomSongs", size=500)["randomSongs"]["song"]) == 9
    with web.ctx.db() as conn:
        number = conn.execute("SELECT id FROM tracks WHERE title = 'Hidden Song'").fetchone()["id"]
    rest.fails("getSong", 70, id=str(number))


# ------------------------------------------------------------------------------------------------------------------------------- limits


def test_a_page_of_anything_is_at_most_500_and_a_client_that_asks_for_more_is_given_that(rest: Rest, web: SimpleNamespace) -> None:
    add_songs(web, [(f"/crowd/{n}.mp3", f"Song {n}", "Crowd", f"Album {n:04d}", 1) for n in range(520)])
    assert len(lists(rest, "alphabeticalByName", size=100000)) == 500 and len(lists(rest, "alphabeticalByName", size=500)) == 500 and len(lists(rest, "alphabeticalByName", size=499)) == 499
    assert len(lists(rest, "alphabeticalByName", size=500, offset=500)) == 24  # (520 and the four of the library, from the 501st on)
    assert len(rest.ok("getRandomSongs", size=100000)["randomSongs"]["song"]) == 500
    assert len(rest.ok("getSongsByGenre", genre="folk", count=100000)["songsByGenre"]["song"]) == 2
    found = rest.ok("search3", query="", songCount=100000, albumCount=100000, artistCount=100000)["searchResult3"]
    assert (len(found["song"]), len(found["album"]), len(found["artist"])) == (500, 500, 4)
    crowd = rest.ok("getArtist", id=next(a["id"] for a in found["artist"] if a["name"] == "Crowd"))["artist"]
    assert len(crowd["album"]) == 500


def test_an_album_gives_at_most_5000_songs(rest: Rest, web: SimpleNamespace) -> None:
    add_songs(web, [(f"/huge/{n}.mp3", f"Song {n}", "Huge", "Huge", n) for n in range(5001)])
    album = album_named(rest, "Huge")
    assert album["songCount"] == 5001 and len(songs_of(rest, album["id"])) == 5000
    assert len(rest.ok("getMusicDirectory", id=album["id"])["directory"]["child"]) == 5000


# ---------------------------------------------------------------------------------------------------------------------------- odd records


def test_a_song_that_nobody_dated_is_as_old_as_there_is(rest: Rest, web: SimpleNamespace) -> None:
    with web.ctx.db() as conn:
        conn.execute("UPDATE tracks SET date_added = NULL")
        conn.commit()
    epoch = "1970-01-01T00:00:00.000Z"
    album = album_named(rest, "Northern Lights")
    assert album["created"] == epoch and all(song["created"] == epoch for song in songs_of(rest, album["id"]))
    assert rest.ok("getArtist", id=album["artistId"])["artist"]["album"][0]["created"] == epoch


def test_a_song_that_is_not_in_a_library_folder_is_known_by_its_file_name(rest: Rest, web: SimpleNamespace, tmp_path: Path) -> None:
    web.ctx.config.library.roots = [str(tmp_path / "somewhere else")]
    assert [song["path"] for song in songs_of(rest, album_named(rest, "Northern Lights")["id"])] == ["01 - Glacier.mp3", "02 - Polar Night.mp3", "03 - Drift.mp3"]


def test_a_song_may_be_in_the_second_of_several_library_folders(rest: Rest, web: SimpleNamespace, music_dir: Path, tmp_path: Path) -> None:
    web.ctx.config.library.roots = [str(tmp_path / "unrelated"), str(music_dir)]
    assert songs_of(rest, album_named(rest, "Northern Lights")["id"])[0]["path"] == "Aurora Vale/Northern Lights/01 - Glacier.mp3"


def test_a_song_says_its_disc_its_musicbrainz_id_and_its_genre_when_it_has_them_and_nothing_when_it_has_not(rest: Rest, web: SimpleNamespace) -> None:
    first, second = songs_of(rest, album_named(rest, "Back Roads")["id"])
    assert all(key not in first for key in ("discNumber", "musicBrainzId")) and first["genre"] == "Folk" and first["genres"] == [{"name": "Folk"}]
    with web.ctx.db() as conn:
        conn.execute("UPDATE tracks SET disc_number = 2, musicbrainz_recording_id = 'b1a9c0e9-d987-4042-ae91-78d6a3267d69' WHERE id = ?", (int(first["id"]),))
        conn.execute("UPDATE tracks SET genre = NULL WHERE id = ?", (int(second["id"]),))
        conn.commit()
    by_title = {song["title"]: song for song in songs_of(rest, album_named(rest, "Back Roads")["id"])}
    assert by_title["Gravel Dust"]["discNumber"] == 2 and by_title["Gravel Dust"]["musicBrainzId"] == "b1a9c0e9-d987-4042-ae91-78d6a3267d69"
    assert all(key not in by_title["Porch Light"] for key in ("genre", "genres", "discNumber", "musicBrainzId"))


def test_a_folder_that_is_a_playlist_or_a_song_is_said_not_to_be_a_directory(rest: Rest) -> None:
    assert rest.fails("getMusicDirectory", 70, id="pl-1") == "The requested directory was not found"
    assert rest.fails("getMusicDirectory", 70, id="12") == "The requested directory was not found"


def test_a_list_asked_for_without_a_size_has_the_size_the_protocol_says(rest: Rest, web: SimpleNamespace) -> None:
    add_songs(web, [(f"/many/{n}.mp3", f"Song {n:03d}", f"Artist {n:03d}", f"Album {n:03d}", 1) for n in range(60)])
    with web.ctx.db() as conn:
        conn.execute("UPDATE tracks SET genre = 'Many' WHERE file_path LIKE '/many/%'")
        conn.commit()
    assert len(lists(rest, "alphabeticalByName")) == 10  # (ten albums)
    assert len(rest.ok("getRandomSongs")["randomSongs"]["song"]) == 10
    assert len(rest.ok("getSongsByGenre", genre="many")["songsByGenre"]["song"]) == 10
    found = rest.ok("search3", query="")["searchResult3"]
    assert (len(found["artist"]), len(found["album"]), len(found["song"])) == (20, 20, 20)  # (twenty of each)
    assert len(rest.ok("search3", query="many", songCount=3)["searchResult3"]["song"]) == 3  # (the genre is one of the words a song is found by)
    again = rest.ok("search3", query="Song 0", artistCount=2, albumCount=2, songCount=2)["searchResult3"]
    assert (len(again["artist"]), len(again["album"]), len(again["song"])) == (2, 2, 2)
    assert names(rest.ok("search3", query="Song 0", artistCount=2, albumCount=0, songCount=0)["searchResult3"]["artist"]) == names(again["artist"])  # (the first page: no offset)


def test_the_top_songs_are_fifty_unless_asked_for_fewer(rest: Rest, web: SimpleNamespace) -> None:
    add_songs(web, [(f"/prolific/{n}.mp3", f"Hit {n:03d}", "Prolific", "Hits", n) for n in range(60)])
    assert len(rest.ok("getTopSongs", artist="Prolific")["topSongs"]["song"]) == 50
    assert len(rest.ok("getTopSongs", artist="Prolific", count=7)["topSongs"]["song"]) == 7
    assert len(rest.ok("getTopSongs", artist="Prolific", count=0)["topSongs"]["song"]) == 1  # (at least one)
    assert len(rest.ok("getTopSongs", artist="Prolific", count=100000)["topSongs"]["song"]) == 60
