"""Carrying tags and a cover from one kind of file to another: written with `discs.tagging`, read with `convert.tags`, in every direction."""

from __future__ import annotations

import base64
import io
import shutil
from pathlib import Path

import pytest
import soundfile as sf
from mutagen.asf import ASF
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE
from PIL import Image

from musictoolkit.convert.tags import read_tags
from musictoolkit.discs.tagging import ENCODER, FORMATS, SongTags, TagError, write_tags
from musictoolkit.media import art
from musictoolkit.media.info import replaygain
from tests import convert_audio as audio

FIXTURES = Path(__file__).parent / "fixtures"
RELEASE, RECORDING, GROUP, ARTIST = "07090529-0fbf-4bd3-adc4-fe627343976d", "95042c0a-5b64-4f61-ba75-2a1be26466d3", "9a3641db-6324-3db1-a406-075a2336e8bb", "66da25f9-1534-4dd1-b88c-718bc24e1ccd"
FILE_FORMATS = {"mp3": ("MP3", "MPEG_LAYER_III"), "ogg": ("OGG", "VORBIS"), "opus": ("OGG", "OPUS"), "flac": ("FLAC", "PCM_16"), "wav": ("WAV", "PCM_16")}
EXTENSIONS = {"mp3": "mp3", "ogg": "ogg", "opus": "opus", "flac": "flac", "wav": "wav"}


def cover(kind: str = "JPEG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (320, 240), (180, 40, 40)).save(out, kind)
    return out.getvalue()


def full(**changes) -> SongTags:
    base = dict(
        title="Big in Japan", artist="Guano Apes", album="Don’t Give Me Names", album_artist="The Album Artist", track_number=3, track_total=15,
        disc_number=2, disc_total=2, year=2000, date="2000-05-02", genre="Rock", recording_id=RECORDING, release_id=RELEASE, release_group_id=GROUP,
        artist_ids=[ARTIST], barcode="743217522421", label="BMG", composer="Henning Rümenapp", comment="Remastered\nfrom the tapes",
        lyrics="Line one\nLine two\n\nLine four", replaygain={"track_gain": "-7.50 dB", "track_peak": "0.988000", "album_gain": "-6.00 dB", "album_peak": "1.000000"},
    )
    return SongTags(**{**base, **changes})


def blank(tmp_path: Path, fmt: str, name: str = "blank") -> Path:
    file_format, subtype = FILE_FORMATS[fmt]
    path = tmp_path / f"{name}.{EXTENSIONS[fmt]}"
    sf.write(str(path), audio.tones(48000, 1), 48000, format=file_format, subtype=subtype)
    return path


def same(got: SongTags, wanted: SongTags) -> None:
    for field in ("title", "artist", "album", "album_artist", "track_number", "track_total", "disc_number", "disc_total", "year", "date", "genre", "recording_id",
                  "release_id", "release_group_id", "artist_ids", "barcode", "label", "composer", "comment", "lyrics", "replaygain"):
        assert getattr(got, field) == getattr(wanted, field), field


# ---------------------------------------------------------------------------------------- every kind of file the app writes, and back


@pytest.mark.parametrize("fmt", FORMATS)
def test_every_field_survives_being_written_and_read_back(tmp_path: Path, fmt: str) -> None:
    path = blank(tmp_path, fmt)
    write_tags(path, fmt, full(), cover())
    same(read_tags(path), full())


@pytest.mark.parametrize("fmt", ["mp3", "flac", "ogg", "opus"])
def test_the_cover_is_in_the_file_where_the_app_looks_for_it(tmp_path: Path, fmt: str) -> None:
    path = blank(tmp_path, fmt)
    picture = cover()
    write_tags(path, fmt, full(), picture)
    assert art.embedded_art(path) == picture


def test_a_wav_file_carries_the_cover_in_its_id3_chunk(tmp_path: Path) -> None:
    path = blank(tmp_path, "wav")
    picture = cover()
    write_tags(path, "wav", full(), picture)
    frames = WAVE(path).tags
    assert [f.data for f in frames.getall("APIC")] == [picture] and frames.getall("APIC")[0].type == 3


@pytest.mark.parametrize("kind,mime", [("JPEG", "image/jpeg"), ("PNG", "image/png")])
def test_the_picture_is_labelled_with_what_it_really_is(tmp_path: Path, kind: str, mime: str) -> None:
    flac, mp3, ogg = blank(tmp_path, "flac"), blank(tmp_path, "mp3"), blank(tmp_path, "ogg")
    for path, fmt in ((flac, "flac"), (mp3, "mp3"), (ogg, "ogg")):
        write_tags(path, fmt, full(), cover(kind))
    assert [p.mime for p in FLAC(flac).pictures] == [mime]
    from mutagen.id3 import ID3
    assert [f.mime for f in ID3(mp3).getall("APIC")] == [mime]
    packed = OggVorbis(ogg)["metadata_block_picture"]
    assert [Picture(base64.b64decode(p)).mime for p in packed] == [mime]


@pytest.mark.parametrize("fmt", FORMATS)
def test_what_a_file_was_tagged_with_before_is_gone_when_it_is_tagged_again(tmp_path: Path, fmt: str) -> None:
    path = blank(tmp_path, fmt)
    write_tags(path, fmt, full(), cover())
    write_tags(path, fmt, SongTags(title="New", artist="Someone", album="Else", album_artist="", track_number=1, track_total=0, disc_number=0, disc_total=0))
    got = read_tags(path)
    assert (got.title, got.artist, got.album, got.genre, got.lyrics, got.comment, got.composer, got.replaygain, got.artist_ids) == ("New", "Someone", "Else", "", "", "", "", {}, [])
    assert got.recording_id is None and got.release_id is None and (got.disc_number, got.track_total) == (0, 0)
    if fmt != "wav":
        assert art.embedded_art(path) is None


def test_lyrics_and_comments_keep_their_lines_whatever_the_line_endings(tmp_path: Path) -> None:
    for fmt in ("flac", "mp3", "ogg"):
        path = blank(tmp_path, fmt, f"lines-{fmt}")
        write_tags(path, fmt, full(lyrics="one\r\ntwo\rthree\n\n  four  ", comment="a\r\nb"), None)
        got = read_tags(path)
        assert (got.lyrics, got.comment) == ("one\ntwo\nthree\n\n  four", "a\nb"), fmt  # (a line keeps its indentation; trailing spaces go)


def test_a_file_the_encoder_names_itself_in(tmp_path: Path) -> None:
    flac = blank(tmp_path, "flac")
    write_tags(flac, "flac", full(), None)
    assert FLAC(flac)["ENCODER"] == [ENCODER]
    opus = blank(tmp_path, "opus")
    write_tags(opus, "opus", full(), None)
    assert OggOpus(opus)["ENCODER"] == [ENCODER]


@pytest.mark.parametrize("fmt", ["mp3", "flac", "ogg", "opus"])
def test_the_loudness_the_app_levels_by_is_carried_in_the_tags_it_reads(tmp_path: Path, fmt: str) -> None:
    path = blank(tmp_path, fmt)
    write_tags(path, fmt, full(), None)
    assert replaygain(path) == {"track_db": -7.5, "album_db": -6.0}


def test_a_missing_file_cannot_be_tagged(tmp_path: Path) -> None:
    for fmt in FORMATS:
        with pytest.raises(TagError):
            write_tags(tmp_path / f"nothing.{EXTENSIONS[fmt]}", fmt, full(), None)


# ---------------------------------------------------------------------------------------- reading the kinds the app does not write


def test_the_tags_of_an_m4a_file(tmp_path: Path) -> None:
    path = tmp_path / "a.m4a"
    shutil.copy(FIXTURES / "tones_aac.m4a", path)
    atoms = MP4(path)
    atoms.tags = None
    atoms.add_tags()
    atoms["\xa9nam"], atoms["\xa9ART"], atoms["\xa9alb"], atoms["aART"] = ["Song"], ["Artist One", "Artist Two"], ["Album"], ["Album Artist"]
    atoms["trkn"], atoms["disk"], atoms["\xa9day"], atoms["\xa9gen"], atoms["\xa9wrt"] = [(3, 12)], [(1, 2)], ["2001-02-03"], ["Jazz"], ["Composer"]
    atoms["\xa9cmt"], atoms["\xa9lyr"] = ["A comment"], ["Words\nof the song"]
    atoms["----:com.apple.iTunes:MusicBrainz Track Id"] = [MP4FreeForm(RECORDING.encode())]
    atoms["----:com.apple.iTunes:MusicBrainz Album Id"] = [MP4FreeForm(RELEASE.encode())]
    atoms["----:com.apple.iTunes:MusicBrainz Artist Id"] = [MP4FreeForm(ARTIST.encode())]
    atoms["----:com.apple.iTunes:replaygain_track_gain"] = [MP4FreeForm(b"-3.20 dB")]
    atoms.save()
    got = read_tags(path)
    wanted = SongTags("Song", "Artist One; Artist Two", "Album", "Album Artist", 3, 12, 1, 2, 2001, "2001-02-03", "Jazz", RECORDING, RELEASE, None, [ARTIST], "", "",
                      "Composer", "A comment", "Words\nof the song", {"track_gain": "-3.20 dB"})
    same(got, wanted)


def test_the_tags_of_a_wma_file(tmp_path: Path) -> None:
    path = tmp_path / "a.wma"
    shutil.copy(FIXTURES / "tones_wma.wma", path)
    attributes = ASF(path)
    for key, value in {"Title": "Song", "Author": "Artist", "WM/AlbumTitle": "Album", "WM/AlbumArtist": "Album Artist", "WM/TrackNumber": "3", "WM/PartOfSet": "1/2",
                       "WM/Year": "2001", "WM/Genre": "Jazz", "WM/Composer": "Composer", "Description": "A comment", "WM/Lyrics": "Words",
                       "MusicBrainz/Track Id": RECORDING, "MusicBrainz/Album Id": RELEASE}.items():
        attributes[key] = [value]
    attributes.save()
    got = read_tags(path)
    assert (got.title, got.artist, got.album, got.album_artist, got.track_number, got.disc_number, got.disc_total, got.year, got.genre, got.composer) == (
        "Song", "Artist", "Album", "Album Artist", 3, 1, 2, 2001, "Jazz", "Composer")
    assert (got.comment, got.lyrics, got.recording_id, got.release_id) == ("A comment", "Words", RECORDING, RELEASE)


@pytest.mark.parametrize("name", ["tiny.flac", "tiny.ogg", "tiny.opus", "tiny.m4a", "sparse_tags.mp3", "complete_tags.mp3", "tone_12s.mp3"])
def test_the_files_the_library_already_holds_are_read_without_trouble(name: str) -> None:
    got = read_tags(FIXTURES / name)
    assert isinstance(got, SongTags) and isinstance(got.track_number, int) and isinstance(got.artist_ids, list) and isinstance(got.replaygain, dict)


def test_a_file_with_tags_is_read_with_the_fields_it_has() -> None:
    got = read_tags(FIXTURES / "complete_tags.mp3")
    assert got.title and got.artist and got.album  # (the fixture is a song with everything the scanner reads)


def test_a_file_with_no_tags_a_damaged_one_and_a_missing_one_come_out_empty(tmp_path: Path) -> None:
    empty = blank(tmp_path, "wav")
    garbage = tmp_path / "garbage.mp3"
    garbage.write_bytes(b"not music " * 200)
    for path in (empty, garbage, tmp_path / "gone.flac"):
        got = read_tags(path)
        assert (got.title, got.artist, got.album, got.track_number, got.disc_number, got.lyrics, got.artist_ids, got.replaygain) == ("", "", "", 0, 0, "", [], {}), path.name


@pytest.mark.parametrize("raw,expected", [("3/12", (3, 12)), ("03", (3, 0)), (" 7 / 9 ", (7, 9)), ("x", (0, 0)), ("", (0, 0)), (None, (0, 0))])
def test_numbers_that_come_as_n_or_n_of_total(raw: object, expected: tuple[int, int]) -> None:
    from musictoolkit.convert.tags import _pair
    assert _pair(raw) == expected


def test_a_vorbis_file_that_keeps_its_totals_in_the_other_fields_and_its_artists_apart(tmp_path: Path) -> None:
    path = blank(tmp_path, "flac")
    audio_file = FLAC(path)
    audio_file.clear()
    audio_file["TITLE"], audio_file["ARTIST"] = ["Song"], ["One", "Two"]
    audio_file["TRACKNUMBER"], audio_file["TOTALTRACKS"], audio_file["DISCNUMBER"], audio_file["TOTALDISCS"] = ["4"], ["10"], ["1"], ["3"]
    audio_file["YEAR"], audio_file["ORGANIZATION"], audio_file["DESCRIPTION"], audio_file["UNSYNCEDLYRICS"] = ["1999"], ["A Label"], ["Said"], ["Sung"]
    audio_file.save()
    got = read_tags(path)
    assert (got.artist, got.track_number, got.track_total, got.disc_number, got.disc_total, got.year, got.label, got.comment, got.lyrics) == (
        "One; Two", 4, 10, 1, 3, 1999, "A Label", "Said", "Sung")
