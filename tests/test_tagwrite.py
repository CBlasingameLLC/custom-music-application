import shutil
from pathlib import Path

import mutagen
import pytest

from musictoolkit.ingest import tagwrite
from tests.conftest import FIXTURES_DIR

FORMATS = ["complete_tags.mp3", "tiny.flac", "tiny.m4a", "tiny.ogg", "tiny.opus"]


def read(path: Path) -> dict[str, str]:
    tags = mutagen.File(path, easy=True).tags or {}
    return {key: tags[key][0] for key in tags.keys()}


@pytest.fixture(params=FORMATS)
def audio_file(request, tmp_path: Path) -> Path:
    path = tmp_path / request.param
    shutil.copy(FIXTURES_DIR / request.param, path)
    return path


class TestCleanChanges:
    def test_text_is_trimmed_and_blank_clears(self) -> None:
        assert tagwrite.clean_changes({"title": "  Hi  ", "genre": "   ", "album": None}) == {
            "title": "Hi", "genre": None, "album": None,
        }

    def test_numbers_are_parsed(self) -> None:
        assert tagwrite.clean_changes({"year": "2019", "track_number": 4, "disc_number": ""}) == {
            "year": 2019, "track_number": 4, "disc_number": None,
        }

    def test_control_characters_are_dropped(self) -> None:
        assert tagwrite.clean_changes({"title": "a\x00b\nc"})["title"] == "abc"

    @pytest.mark.parametrize(
        "bad", [{"rating": 5}, {"year": "abc"}, {"year": 0}, {"year": 10000}, {"track_number": -1}, {"title": "x" * 501}]
    )
    def test_bad_values_are_refused(self, bad) -> None:
        with pytest.raises(ValueError):
            tagwrite.clean_changes(bad)


class TestEveryFormat:
    def test_write_then_restore_round_trips(self, audio_file: Path) -> None:
        original = read(audio_file)
        before, after = tagwrite.apply_changes(
            audio_file,
            tagwrite.clean_changes({"title": "New Title", "artist": "New Artist", "album_artist": "AA", "genre": "Jazz", "year": 1999, "track_number": 7, "disc_number": 2}),
        )
        now = read(audio_file)
        assert (now["title"], now["artist"], now["albumartist"], now["genre"]) == ("New Title", "New Artist", "AA", "Jazz")
        assert now["date"].startswith("1999") and now["tracknumber"].startswith("7") and now["discnumber"].startswith("2")
        assert set(before) == set(after)

        tagwrite.restore(audio_file, before)
        restored = read(audio_file)
        for key in ("title", "artist", "albumartist", "genre", "date", "tracknumber", "discnumber"):
            assert restored.get(key) == original.get(key), key

    def test_clearing_removes_the_tag(self, audio_file: Path) -> None:
        tagwrite.apply_changes(audio_file, {"genre": "Rock"})
        assert read(audio_file)["genre"] == "Rock"
        before, after = tagwrite.apply_changes(audio_file, {"genre": None})
        assert "genre" not in read(audio_file)
        assert before == {"genre": "Rock"} and after == {"genre": None}

    def test_other_tags_are_left_alone(self, audio_file: Path) -> None:
        tagwrite.apply_changes(audio_file, {"album": "Keep Me"})
        tagwrite.apply_changes(audio_file, {"genre": "Folk"})
        assert read(audio_file)["album"] == "Keep Me"

    def test_a_file_that_already_matches_is_not_touched(self, audio_file: Path) -> None:
        tagwrite.apply_changes(audio_file, {"title": "Same"})
        stamp = audio_file.stat().st_mtime_ns
        assert tagwrite.apply_changes(audio_file, {"title": "Same"}) == ({}, {})
        assert audio_file.stat().st_mtime_ns == stamp


class TestPreservation:
    def test_track_total_survives_a_track_number_edit(self, tmp_path: Path) -> None:
        path = tmp_path / "a.flac"
        shutil.copy(FIXTURES_DIR / "tiny.flac", path)
        tagwrite.restore(path, {"tracknumber": "3/12", "date": "2019-05-03"})
        tagwrite.apply_changes(path, {"track_number": 4})
        assert read(path)["tracknumber"] == "4/12"

    def test_same_track_number_keeps_the_total(self, tmp_path: Path) -> None:
        path = tmp_path / "a.flac"
        shutil.copy(FIXTURES_DIR / "tiny.flac", path)
        tagwrite.restore(path, {"tracknumber": "3/12"})
        assert tagwrite.apply_changes(path, {"track_number": 3}) == ({}, {})

    def test_full_date_is_kept_when_the_year_is_unchanged(self, tmp_path: Path) -> None:
        path = tmp_path / "a.flac"
        shutil.copy(FIXTURES_DIR / "tiny.flac", path)
        tagwrite.restore(path, {"date": "2019-05-03"})
        assert tagwrite.apply_changes(path, {"year": 2019}) == ({}, {})
        assert read(path)["date"] == "2019-05-03"
        tagwrite.apply_changes(path, {"year": 2020})
        assert read(path)["date"] == "2020"


class TestMp3Details:
    def test_new_tags_are_written_as_id3v23_for_older_players(self, tmp_path: Path) -> None:
        from mutagen.id3 import ID3

        path = tmp_path / "plain.mp3"
        shutil.copy(FIXTURES_DIR / "sparse_tags.mp3", path)
        assert mutagen.File(path, easy=True).tags is None  # a file with no tags at all
        tagwrite.apply_changes(path, {"title": "Fresh"})
        assert ID3(path).version == (2, 3, 0)
        assert read(path)["title"] == "Fresh"

    def test_existing_v24_tags_stay_v24(self, tmp_path: Path) -> None:
        from mutagen.id3 import ID3

        path = tmp_path / "a.mp3"
        shutil.copy(FIXTURES_DIR / "complete_tags.mp3", path)
        id3 = ID3(path)
        id3.save(path, v2_version=4)
        tagwrite.apply_changes(path, {"genre": "Folk"})
        assert ID3(path).version == (2, 4, 0)


class TestRefusals:
    def test_unsupported_format_is_explained(self, tmp_path: Path) -> None:
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF....WAVE")
        with pytest.raises(tagwrite.TagWriteError, match="can't be edited yet"):
            tagwrite.apply_changes(wav, {"title": "x"})

    def test_missing_file_is_explained(self, tmp_path: Path) -> None:
        with pytest.raises(tagwrite.TagWriteError, match="missing"):
            tagwrite.apply_changes(tmp_path / "gone.mp3", {"title": "x"})

    def test_read_only_file_is_explained(self, tmp_path: Path) -> None:
        import os
        import stat

        path = tmp_path / "ro.mp3"
        shutil.copy(FIXTURES_DIR / "complete_tags.mp3", path)
        path.chmod(stat.S_IREAD)
        if os.access(path, os.W_OK):  # running as root: permission bits are not enforced
            pytest.skip("cannot make a file unwritable for this user")
        with pytest.raises(tagwrite.TagWriteError, match="read-only"):
            tagwrite.apply_changes(path, {"title": "x"})
