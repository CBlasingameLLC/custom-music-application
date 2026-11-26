"""The calls that give a file or a picture: the song, its cover, and its lyrics."""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from musictoolkit.media import lrclib
from tests.conftest import FIXTURES_DIR, make_stub
from tests.subsonic_support import Rest, album_named, scanned_web, songs_of

GLACIER = Path("Aurora Vale") / "Northern Lights" / "01 - Glacier.mp3"


@pytest.fixture
def rest(web: SimpleNamespace) -> Rest:
    return Rest(web.client)


def glacier(rest: Rest) -> dict:
    return songs_of(rest, album_named(rest, "Northern Lights")["id"])[0]


def dimensions(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


# ---------------------------------------------------------------------------------------------------------------------------------- the file


def test_a_song_is_the_file_as_it_is_with_the_type_of_audio_it_is(rest: Rest, music_dir: Path) -> None:
    response = rest.get("stream", id=glacier(rest)["id"])
    assert response.status_code == 200 and response.headers["content-type"] == "audio/mpeg" and response.headers["accept-ranges"] == "bytes"
    assert response.content == (music_dir / GLACIER).read_bytes()


def test_a_client_that_seeks_gets_the_part_it_asks_for(rest: Rest, music_dir: Path) -> None:
    data = (music_dir / GLACIER).read_bytes()
    response = rest.client.get("/rest/stream", params=rest.params(id=glacier(rest)["id"]), headers={"Range": "bytes=10-19"})
    assert response.status_code == 206 and response.headers["content-range"] == f"bytes 10-19/{len(data)}" and response.content == data[10:20]
    response = rest.client.get("/rest/stream", params=rest.params(id=glacier(rest)["id"]), headers={"Range": "bytes=100-"})
    assert response.status_code == 206 and response.content == data[100:]
    response = rest.client.get("/rest/stream", params=rest.params(id=glacier(rest)["id"]), headers={"Range": "bytes=-50"})
    assert response.status_code == 206 and response.content == data[-50:]


def test_download_is_the_same_file_and_so_is_stream_when_it_is_asked_to_convert(rest: Rest, music_dir: Path) -> None:
    data = (music_dir / GLACIER).read_bytes()
    number = glacier(rest)["id"]
    assert rest.get("download", id=number).content == data
    assert rest.client.get("/rest/download.view", params=rest.params(id=number)).content == data
    assert rest.get("stream", id=number, format="mp3", maxBitRate=64, timeOffset=30, estimateContentLength="true").content == data


def test_the_headers_of_a_song_are_answered_without_the_song(rest: Rest, music_dir: Path) -> None:
    response = rest.client.head("/rest/stream", params=rest.params(id=glacier(rest)["id"]))
    assert response.status_code == 200 and response.content == b"" and int(response.headers["content-length"]) == (music_dir / GLACIER).stat().st_size


@pytest.mark.parametrize("fixture,suffix,media_type", [
    ("tiny.flac", "flac", "audio/flac"), ("tiny.m4a", "m4a", "audio/mp4"), ("tiny.ogg", "ogg", "audio/ogg"), ("tiny.opus", "opus", "audio/ogg"),
])
def test_each_kind_of_file_is_given_with_its_own_type_and_the_song_says_the_same(tmp_path: Path, music_dir: Path, fixture: str, suffix: str, media_type: str) -> None:
    shutil.copy(FIXTURES_DIR / fixture, music_dir / f"loose.{suffix}")
    rest = Rest(scanned_web(tmp_path, music_dir).client)
    song = next(song for song in rest.ok("search3", query="", songCount=500)["searchResult3"]["song"] if song["suffix"] == suffix)
    assert song["contentType"] == media_type and song["path"] == f"loose.{suffix}"
    assert rest.get("stream", id=song["id"]).headers["content-type"] == media_type


def test_what_is_asked_for_without_saying_which_song_or_one_that_is_not_there_is_an_error(rest: Rest) -> None:
    assert rest.fails("stream", 10) == "Required parameter is missing: id"
    for given in ("999999", "al-WyJ4IiwgInkiXQ", "nope", "pl-1"):
        rest.fails("stream", 70, id=given)
        rest.fails("download", 70, id=given)
    assert rest.get("stream", id="999999").status_code == 200  # (an error is an answer, as everywhere in this protocol)


def test_a_song_whose_file_has_gone_is_not_found_and_is_forgotten(rest: Rest, music_dir: Path) -> None:
    song = glacier(rest)
    (music_dir / GLACIER).unlink()
    assert rest.fails("stream", 70, id=song["id"]) == "the file is missing from disk"
    rest.fails("getSong", 70, id=song["id"])  # (it is no longer in the library)
    assert [s["title"] for s in songs_of(rest, album_named(rest, "Northern Lights")["id"])] == ["Polar Night", "Drift"]


def test_a_song_on_a_drive_that_is_not_connected_is_an_error_that_says_so_and_is_not_forgotten(rest: Rest, music_dir: Path, web: SimpleNamespace) -> None:
    song = glacier(rest)
    shutil.rmtree(music_dir)
    assert rest.fails("stream", 0, id=song["id"]) == "The library's drive is not available right now. Is it connected?"
    with web.ctx.db() as conn:
        assert conn.execute("SELECT is_missing FROM tracks WHERE id = ?", (int(song["id"]),)).fetchone()["is_missing"] == 0


def test_a_file_that_is_only_tags_has_nothing_to_play(tmp_path: Path, music_dir: Path) -> None:
    make_stub(music_dir / "Some Band" / "Some Album", title="Hidden Song")
    web = scanned_web(tmp_path, music_dir)
    with web.ctx.db() as conn:
        number = conn.execute("SELECT id FROM tracks WHERE title = 'Hidden Song'").fetchone()["id"]
    assert Rest(web.client).fails("stream", 70, id=str(number)) == "This file has tags but no audio in it, so there is nothing to play."


# ---------------------------------------------------------------------------------------------------------------------------------- the cover


def test_a_cover_is_a_jpeg_for_a_song_an_album_and_an_artist_that_has_one(rest: Rest) -> None:
    album = album_named(rest, "Northern Lights")
    for given in (glacier(rest)["id"], album["id"], album["artistId"]):
        response = rest.get("getCoverArt", id=given)
        assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg" and response.content[:3] == b"\xff\xd8\xff", given
        assert response.headers["cache-control"] == "private, max-age=86400"


def test_the_artist_has_the_cover_of_the_first_of_its_albums_that_has_one(rest: Rest) -> None:
    low_tide = album_named(rest, "Low Tide")
    rest.fails("getCoverArt", 70, id=low_tide["id"])  # (this one has none)
    assert rest.get("getCoverArt", id=low_tide["artistId"]).headers["content-type"] == "image/jpeg"  # (Northern Lights, by the same artist, has)


def test_a_playlist_has_the_cover_of_the_first_of_its_songs_that_has_one(rest: Rest) -> None:
    plain = songs_of(rest, album_named(rest, "Back Roads")["id"])[0]
    covered = glacier(rest)
    made = rest.ok("createPlaylist", name="Mixed", songId=[plain["id"], covered["id"]])["playlist"]
    assert rest.get("getCoverArt", id=made["id"]).headers["content-type"] == "image/jpeg"
    bare = rest.ok("createPlaylist", name="Bare", songId=[plain["id"]])["playlist"]
    rest.fails("getCoverArt", 70, id=bare["id"])
    rest.fails("getCoverArt", 70, id="pl-9999")


@pytest.mark.parametrize("asked,made", [(None, 300), (1, 64), (64, 64), (65, 128), (128, 128), (129, 256), (256, 256), (257, 300), (5000, 300)])
def test_a_cover_is_made_in_the_smallest_size_the_app_keeps_that_is_as_big_as_the_client_asked_for(rest: Rest, asked: int | None, made: int) -> None:
    response = rest.get("getCoverArt", id=album_named(rest, "Northern Lights")["id"], size=asked)
    assert dimensions(response.content) == (made, made)  # (the picture itself is 300 pixels across, and is not made bigger)


def test_a_song_whose_album_has_no_cover_has_none_and_that_is_an_answer_not_a_picture(rest: Rest) -> None:
    response = rest.get("getCoverArt", id=album_named(rest, "Back Roads")["id"])
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/xml") and 'code="70"' in response.text
    assert rest.fails("getCoverArt", 70, id=songs_of(rest, album_named(rest, "Back Roads")["id"])[0]["id"]) == "The requested cover art was not found"


@pytest.mark.parametrize("given", ["999999", "nope", "al-!!!", "al-WyJ4IiwgInkiXQ", "ar-!!", "ar-QQ"])
def test_a_cover_of_something_that_is_not_there_is_not_found(rest: Rest, given: str) -> None:
    rest.fails("getCoverArt", 70, id=given)


def test_a_cover_needs_to_be_told_which(rest: Rest) -> None:
    assert rest.fails("getCoverArt", 10) == "Required parameter is missing: id"
    rest.fails("getCoverArt", 0, id="1", size="big")


def test_the_cover_of_a_song_on_a_drive_that_is_not_connected_is_an_error_not_a_missing_cover(rest: Rest, music_dir: Path) -> None:
    song = glacier(rest)
    shutil.rmtree(music_dir)
    assert "not available" in rest.fails("getCoverArt", 0, id=song["id"])


# ----------------------------------------------------------------------------------------------------------------------------------- lyrics


SYNCED = "[00:01.00]First line\n[00:05.50]Second line\n"


def lines(answer: dict) -> list[dict]:
    return answer["lyricsList"]["structuredLyrics"]


def test_a_song_with_an_lrc_file_beside_it_has_timed_lyrics_in_milliseconds(rest: Rest, music_dir: Path) -> None:
    (music_dir / GLACIER).with_suffix(".lrc").write_text(SYNCED, encoding="utf-8")
    song = glacier(rest)
    only = lines(rest.ok("getLyricsBySongId", id=song["id"]))
    assert only == [{
        "displayArtist": "Aurora Vale", "displayTitle": "Glacier", "lang": "xxx", "offset": 0, "synced": True,
        "line": [{"start": 1000, "value": "First line"}, {"start": 5500, "value": "Second line"}],
    }]


def test_lyrics_that_are_only_words_are_lines_without_times(rest: Rest, music_dir: Path) -> None:
    (music_dir / GLACIER).with_suffix(".lrc").write_text("one\n\ntwo\nthree\n", encoding="utf-8")
    only = lines(rest.ok("getLyricsBySongId", id=glacier(rest)["id"]))
    assert only == [{"displayArtist": "Aurora Vale", "displayTitle": "Glacier", "lang": "xxx", "synced": False, "line": [{"value": "one"}, {"value": ""}, {"value": "two"}, {"value": "three"}]}]


def test_a_song_with_no_lyrics_has_a_list_with_nothing_in_it(rest: Rest) -> None:
    assert rest.ok("getLyricsBySongId", id=glacier(rest)["id"])["lyricsList"] == {"structuredLyrics": []}


@pytest.mark.parametrize("given", ["999999", "al-x", "nope"])
def test_the_lyrics_of_a_song_that_is_not_there_are_not_found(rest: Rest, given: str) -> None:
    rest.fails("getLyricsBySongId", 70, id=given)
    assert rest.fails("getLyricsBySongId", 10) == "Required parameter is missing: id"


def test_lrclib_is_asked_only_when_the_person_turned_that_on_and_what_it_said_is_kept(rest: Rest, web: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple] = []

    def lookup(artist, title, album=None, duration=None):
        asked.append((artist, title, album))
        return {"synced": [{"t": 2.0, "text": "From the net"}], "plain": None, "instrumental": False, "id": 5}

    monkeypatch.setattr(lrclib, "lookup", lookup)
    number = glacier(rest)["id"]
    assert lines(rest.ok("getLyricsBySongId", id=number)) == [] and asked == []  # (off: nothing leaves the server)
    web.ctx.config.app.lyrics_lrclib = True
    only = lines(rest.ok("getLyricsBySongId", id=number))
    assert asked == [("Aurora Vale", "Glacier", "Northern Lights")] and only[0]["line"] == [{"start": 2000, "value": "From the net"}]
    web.ctx.config.app.lyrics_lrclib = False
    assert lines(rest.ok("getLyricsBySongId", id=number))[0]["line"] == [{"start": 2000, "value": "From the net"}] and len(asked) == 1  # (kept)


def test_the_older_call_gives_the_words_of_a_song_by_its_artist_and_title(rest: Rest, music_dir: Path) -> None:
    (music_dir / GLACIER).with_suffix(".lrc").write_text(SYNCED, encoding="utf-8")
    answer = rest.ok("getLyrics", artist="aurora vale", title="GLACIER")["lyrics"]
    assert answer == {"artist": "aurora vale", "title": "GLACIER", "value": "First line\nSecond line"}
    assert rest.ok("getLyrics", title="Glacier")["lyrics"] == {"title": "Glacier", "value": "First line\nSecond line"}
    (music_dir / GLACIER).with_suffix(".lrc").write_text("only words", encoding="utf-8")
    assert rest.ok("getLyrics", artist="Aurora Vale", title="Glacier")["lyrics"]["value"] == "only words"


def test_the_older_call_for_a_song_that_has_no_lyrics_or_is_not_in_the_library_gives_an_empty_answer(rest: Rest) -> None:
    assert rest.ok("getLyrics", artist="Aurora Vale", title="Glacier")["lyrics"] == {"artist": "Aurora Vale", "title": "Glacier", "value": ""}
    assert rest.ok("getLyrics", artist="Nobody", title="Nothing")["lyrics"]["value"] == ""
    assert rest.ok("getLyrics", artist="Mara Quinn", title="Glacier")["lyrics"]["value"] == ""  # (another artist's song of that name)
    assert rest.fails("getLyrics", 10, artist="Aurora Vale") == "Required parameter is missing: title"
