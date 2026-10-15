"""Tags and cover for ripped songs: read back with mutagen's full interface, and with the library scanner that will meet the files next."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from mutagen import MutagenError
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from PIL import Image

from musictoolkit.discs.encode import encode_pcm
from musictoolkit.discs.tagging import ENCODER, SongTags, TagError, write_tags
from musictoolkit.ingest import scanner
from tests.fake_disc_helper import audio

PCM = audio(2, 0, 150)  # two seconds
RELEASE, RECORDING, GROUP, ARTIST = "07090529-0fbf-4bd3-adc4-fe627343976d", "95042c0a-5b64-4f61-ba75-2a1be26466d3", "9a3641db-6324-3db1-a406-075a2336e8bb", "66da25f9-1534-4dd1-b88c-718bc24e1ccd"


def cover() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (320, 240), (180, 40, 40)).save(out, "JPEG")
    return out.getvalue()


def tags(**changes) -> SongTags:
    base = dict(
        title="Big in Japan", artist="Guano Apes", album="Don’t Give Me Names", album_artist="The Album Artist", track_number=3, track_total=15,
        disc_number=2, disc_total=2, year=2000, date="2000-05-02", genre="Rock", recording_id=RECORDING, release_id=RELEASE,
        release_group_id=GROUP, artist_ids=[ARTIST], barcode="743217522421", label="BMG",
    )
    return SongTags(**{**base, **changes})


def song(tmp_path: Path, fmt: str, name: str = "song") -> Path:
    path = tmp_path / f"{name}.{fmt}"
    encode_pcm(io.BytesIO(PCM), path, fmt, "320" if fmt == "mp3" else None)
    return path


# ---------------------------------------------------------------------------------------- FLAC


def test_flac_gets_vorbis_comments_musicbrainz_ids_and_a_front_cover(tmp_path: Path) -> None:
    path = song(tmp_path, "flac")
    write_tags(path, "flac", tags(), cover())
    audio_file = FLAC(path)
    got = {k.lower(): v for k, v in audio_file.tags.items()}
    assert got["title"] == ["Big in Japan"] and got["artist"] == ["Guano Apes"] and got["albumartist"] == ["The Album Artist"]
    assert got["album"] == ["Don’t Give Me Names"] and got["date"] == ["2000-05-02"] and got["genre"] == ["Rock"]
    assert (got["tracknumber"], got["tracktotal"], got["discnumber"], got["disctotal"]) == (["3"], ["15"], ["2"], ["2"])
    assert got["musicbrainz_trackid"] == [RECORDING] and got["musicbrainz_albumid"] == [RELEASE] and got["musicbrainz_releasegroupid"] == [GROUP]
    assert got["musicbrainz_artistid"] == [ARTIST] and got["musicbrainz_albumartistid"] == [ARTIST]
    assert got["barcode"] == ["743217522421"] and got["label"] == ["BMG"] and got["encoder"] == [ENCODER]
    assert len(audio_file.pictures) == 1
    picture = audio_file.pictures[0]
    assert (picture.type, picture.mime, picture.width, picture.height) == (3, "image/jpeg", 320, 240) and picture.data == cover()
    assert audio_file.info.length == pytest.approx(2.0, abs=0.01), "tagging leaves the audio alone"


def test_tagging_a_file_twice_replaces_the_tags_instead_of_piling_them_up(tmp_path: Path) -> None:
    path = song(tmp_path, "flac")
    write_tags(path, "flac", tags(title="First"), cover())
    write_tags(path, "flac", tags(title="Second"), cover())
    audio_file = FLAC(path)
    assert audio_file["title"] == ["Second"] and len(audio_file.pictures) == 1


def test_empty_fields_are_left_out_not_written_as_nothing(tmp_path: Path) -> None:
    path = song(tmp_path, "flac")
    write_tags(path, "flac", tags(genre="", barcode="", label="", recording_id=None, artist_ids=[], year=None, date=""))
    got = {k.lower() for k in FLAC(path).tags.keys()}
    assert not got & {"genre", "barcode", "label", "musicbrainz_trackid", "musicbrainz_artistid", "date"}
    assert FLAC(path).pictures == []


# ---------------------------------------------------------------------------------------- MP3


def test_mp3_gets_id3v23_with_musicbrainz_ids_and_a_front_cover(tmp_path: Path) -> None:
    path = song(tmp_path, "mp3")
    write_tags(path, "mp3", tags(), cover())
    frames = ID3(path)
    assert frames.version[:2] == (2, 3), "v2.3: every player and device reads it"
    assert str(frames["TIT2"]) == "Big in Japan" and str(frames["TPE1"]) == "Guano Apes" and str(frames["TPE2"]) == "The Album Artist"
    assert str(frames["TALB"]) == "Don’t Give Me Names" and str(frames["TCON"]) == "Rock"
    assert str(frames["TRCK"]) == "3/15" and str(frames["TPOS"]) == "2/2" and str(frames["TDRC"]) == "2000-05-02"  # (read back as v2.4's one date frame)
    stored = ID3(path, translate=False)  # as it is in the file: v2.3 has the year and the day and month apart
    assert (str(stored["TYER"]), str(stored["TDAT"])) == ("2000", "0205")
    assert frames["UFID:http://musicbrainz.org"].data == RECORDING.encode()
    assert str(frames["TXXX:MusicBrainz Album Id"]) == RELEASE and str(frames["TXXX:MusicBrainz Artist Id"]) == ARTIST
    assert str(frames["TXXX:BARCODE"]) == "743217522421" and str(frames["TSSE"]) == ENCODER
    picture = frames["APIC:Front cover"]
    assert (picture.type, picture.mime, picture.data) == (3, "image/jpeg", cover())


def test_a_disc_that_is_not_part_of_a_set_is_numbered_plainly(tmp_path: Path) -> None:
    path = song(tmp_path, "mp3")
    write_tags(path, "mp3", tags(disc_number=1, disc_total=1, track_total=0))
    frames = ID3(path)
    assert str(frames["TPOS"]) == "1" and str(frames["TRCK"]) == "3"


def test_the_tags_an_mp3_had_are_replaced(tmp_path: Path) -> None:
    path = song(tmp_path, "mp3")
    write_tags(path, "mp3", tags(title="First"), cover())
    write_tags(path, "mp3", tags(title="Second"))
    frames = ID3(path)
    assert str(frames["TIT2"]) == "Second" and not frames.getall("APIC")


# ---------------------------------------------------------------------------------------- the library meets them next


@pytest.mark.parametrize("fmt", ["flac", "mp3"])
def test_the_library_scanner_reads_what_was_written(tmp_path: Path, fmt: str) -> None:
    path = song(tmp_path, fmt)
    write_tags(path, fmt, tags(), cover())
    found = scanner.read_basic_tags(path)
    assert (found["title"], found["artist"], found["album"], found["album_artist"]) == ("Big in Japan", "Guano Apes", "Don’t Give Me Names", "The Album Artist")
    assert (found["track_number"], found["disc_number"], found["year"], found["genre"]) == (3, 2, 2000, "Rock")
    assert found["duration_seconds"] == pytest.approx(2.0, abs=0.1) and found["format"] == fmt


def test_control_characters_do_not_get_into_a_tag(tmp_path: Path) -> None:
    path = song(tmp_path, "flac")
    write_tags(path, "flac", tags(title="Line\x00one\x1b[0m\ntwo"))
    assert FLAC(path)["title"] == ["Lineone[0mtwo"]


def test_an_unknown_format_or_an_unwritable_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_tags(tmp_path / "x.wav", "wav", tags())
    with pytest.raises(TagError):
        write_tags(tmp_path / "missing.flac", "flac", tags())


def test_what_the_file_was_tagged_with_before_is_gone_when_it_is_tagged_again(tmp_path: Path) -> None:
    path = song(tmp_path, "flac")
    write_tags(path, "flac", tags(genre="Rock", label="BMG"), cover())
    write_tags(path, "flac", tags(genre="", label=""), None)
    audio_file = FLAC(path)
    assert "genre" not in audio_file and "label" not in audio_file and audio_file.pictures == []


def test_a_file_that_cannot_be_written_to_is_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from musictoolkit.discs import tagging

    def refused(*_args: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tagging, "_flac", refused)
    with pytest.raises(TagError, match="read-only or in use") as caught:
        write_tags(tmp_path / "x.flac", "flac", tags(), None)
    assert caught.value.busy is True, "another program may let go of the file in a moment"

    def wrapped(*_args: object) -> None:
        raise MutagenError(PermissionError(13, "Permission denied"))

    monkeypatch.setattr(tagging, "_flac", wrapped)
    with pytest.raises(TagError, match="read-only or in use") as caught:
        write_tags(tmp_path / "x.flac", "flac", tags(), None)
    assert caught.value.busy is True

    def full(*_args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tagging, "_flac", full)
    with pytest.raises(TagError, match="No space left") as caught:
        write_tags(tmp_path / "x.flac", "flac", tags(), None)
    assert caught.value.busy is False, "trying again would not help"
