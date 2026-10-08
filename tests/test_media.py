from pathlib import Path

import pytest

from musictoolkit.ingest.organizer import validate_scheme
from musictoolkit.media import art, lyrics
from tests.conftest import add_cover, jpeg_bytes, make_track


class TestLrc:
    def test_basic_lines_are_timed_and_sorted(self) -> None:
        parsed = lyrics.parse_lrc("[00:10.00]B\n[00:02.50]A\n")
        assert parsed == [{"t": 2.5, "text": "A"}, {"t": 10.0, "text": "B"}]

    def test_one_line_with_several_timestamps_repeats(self) -> None:
        parsed = lyrics.parse_lrc("[00:01.00][00:09.00]Chorus")
        assert [(p["t"], p["text"]) for p in parsed] == [(1.0, "Chorus"), (9.0, "Chorus")]

    def test_offset_tag_shifts_every_line(self) -> None:
        assert lyrics.parse_lrc("[offset:+500]\n[00:01.00]x")[0]["t"] == 1.5
        assert lyrics.parse_lrc("[offset:-500]\n[00:01.00]x")[0]["t"] == 0.5
        assert lyrics.parse_lrc("[offset:-5000]\n[00:01.00]x")[0]["t"] == 0.0  # never negative

    def test_minutes_over_an_hour_and_centiseconds_vs_milliseconds(self) -> None:
        assert lyrics.parse_lrc("[75:00.00]long")[0]["t"] == 4500.0
        assert lyrics.parse_lrc("[00:01.5]x")[0]["t"] == 1.5
        assert lyrics.parse_lrc("[00:01.500]x")[0]["t"] == 1.5

    def test_plain_text_is_not_lrc(self) -> None:
        assert lyrics.parse_lrc("Just words\nno timestamps") == []

    def test_metadata_tags_are_ignored(self) -> None:
        assert lyrics.parse_lrc("[ti:Title]\n[ar:Artist]\n[00:03.00]line") == [{"t": 3.0, "text": "line"}]


class TestArt:
    def test_nothing_found_returns_none_without_raising(self, tmp_path: Path) -> None:
        path = make_track(tmp_path, "a.mp3", title="x")
        assert art.find_art(path) is None
        assert art.embedded_art(tmp_path / "does-not-exist.mp3") is None

    def test_corrupt_files_do_not_raise(self, tmp_path: Path) -> None:
        bad = tmp_path / "broken.mp3"
        bad.write_bytes(b"not audio at all")
        assert art.embedded_art(bad) is None
        bad_flac = tmp_path / "broken.flac"
        bad_flac.write_bytes(b"fLaC garbage")
        assert art.embedded_art(bad_flac) is None

    def test_embedded_art_beats_folder_art(self, tmp_path: Path) -> None:
        path = make_track(tmp_path, "a.mp3", title="x")
        add_cover(path, (255, 0, 0))
        (tmp_path / "cover.jpg").write_bytes(jpeg_bytes((0, 0, 255)))
        assert art.find_art(path) == art.embedded_art(path)

    @pytest.mark.parametrize("name", ["cover.jpg", "Folder.PNG", "front.jpeg", "ALBUM.webp"])
    def test_folder_art_names(self, tmp_path: Path, name: str) -> None:
        path = make_track(tmp_path, "a.mp3", title="x")
        (tmp_path / name).write_bytes(b"\x89PNG\r\n\x1a\nxxxx")
        assert art.folder_art(path) is not None

    def test_thumbnail_is_cached_and_invalidated_when_the_file_changes(self, tmp_path: Path) -> None:
        path = make_track(tmp_path, "a.mp3", title="x")
        add_cover(path, (255, 0, 0))
        cache = tmp_path / "cache"
        first = art.cached_art(path, 128, cache)
        assert first and len(list(cache.rglob("*.jpg"))) == 1
        assert art.cached_art(path, 128, cache) == first and len(list(cache.rglob("*.jpg"))) == 1
        add_cover(path, (0, 255, 0))  # editing the file's art changes its identity...
        art.cached_art(path, 128, cache)
        assert len(list(cache.rglob("*.jpg"))) == 2  # ...so a fresh thumbnail is made

    def test_mime_sniffing(self) -> None:
        assert art.sniff_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"
        assert art.sniff_mime(b"\x89PNG\r\n\x1a\n") == "image/png"
        assert art.sniff_mime(b"RIFFxxxxWEBPyy") == "image/webp"
        assert art.sniff_mime(b"zz") == "application/octet-stream"


class TestFolderTemplates:
    @pytest.mark.parametrize("scheme", [
        "{album_artist}/{album}/{track:02d} - {title}.{ext}",
        "{artist}/{year} - {album}/{title}.{ext}",
        "{album_artist} - {album} - {track:02d} - {title}.{ext}",
    ])
    def test_valid(self, scheme: str) -> None:
        validate_scheme(scheme)

    @pytest.mark.parametrize("scheme", [
        "{genre}/{title}.{ext}", "{title}", "/{title}.{ext}", "../{title}.{ext}", "{track:zz}/{title}.{ext}",
        "{0}/{title}.{ext}", "", "   ",
        "\\{title}.{ext}", "C:\\music\\{title}.{ext}", "D:/{title}.{ext}", "D:{title}.{ext}", "{album}/../../{title}.{ext}",
    ])
    def test_invalid(self, scheme: str) -> None:
        with pytest.raises(ValueError):
            validate_scheme(scheme)
