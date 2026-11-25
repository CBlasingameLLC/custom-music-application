"""Writing FLAC and MP3 from the audio of a disc."""

from __future__ import annotations

import errno
import io
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC
from mutagen.mp3 import MP3, BitrateMode

from musictoolkit.discs import encode
from musictoolkit.discs.encode import NO_ROOM, Encoder, EncodeError, NoRoomError, check_choice, encode_pcm
from tests.fake_disc_helper import audio
from tests.full_drive import PLENTY

SECTORS = 300  # four seconds of the stand-in's audio
PCM = audio(4, 0, SECTORS)
SECONDS = SECTORS / 75


def decoded(path: Path) -> np.ndarray:
    data, rate = sf.read(str(path), dtype="int16", always_2d=True)
    assert rate == 44100 and data.shape[1] == 2
    return data


# ---------------------------------------------------------------------------------------- FLAC: lossless


def test_flac_decodes_to_exactly_the_audio_that_went_in(tmp_path: Path) -> None:
    path = tmp_path / "song.flac"
    size = encode_pcm(io.BytesIO(PCM), path, "flac")
    assert size == path.stat().st_size and 0 < size < len(PCM)
    assert decoded(path).tobytes() == PCM
    info = FLAC(path).info
    assert (info.sample_rate, info.channels, info.bits_per_sample) == (44100, 2, 16)
    assert info.length == pytest.approx(SECONDS, abs=0.001)


def test_the_audio_may_arrive_in_pieces_of_any_size(tmp_path: Path) -> None:
    path = tmp_path / "pieces.flac"
    with Encoder(path, "flac") as encoder:
        for start in range(0, len(PCM), 4 * 777):  # not a multiple of a sector
            encoder.write(PCM[start:start + 4 * 777])
    assert decoded(path).tobytes() == PCM


# ---------------------------------------------------------------------------------------- MP3: constant bitrates, with a header that gives the right length


@pytest.mark.parametrize("quality, kbps", [("320", 320), ("256", 256), ("192", 192), ("128", 128)])
def test_each_mp3_quality_is_the_constant_bitrate_it_says(tmp_path: Path, quality: str, kbps: int) -> None:
    path = tmp_path / "song.mp3"
    encode_pcm(io.BytesIO(PCM), path, "mp3", quality)
    info = MP3(path).info
    assert info.bitrate_mode == BitrateMode.CBR and abs(info.bitrate / 1000 - kbps) <= 1
    assert info.length == pytest.approx(SECONDS, abs=0.05), "the header gives players and this app the right length"
    assert (info.sample_rate, info.channels) == (44100, 2)
    assert abs(len(decoded(path)) / 44100 - SECONDS) < 0.1


def test_a_better_mp3_quality_is_a_bigger_file(tmp_path: Path) -> None:
    sizes = []
    for quality in ("128", "192", "256", "320"):
        path = tmp_path / f"{quality}.mp3"
        encode_pcm(io.BytesIO(PCM), path, "mp3", quality)
        sizes.append(path.stat().st_size)
    assert sizes == sorted(sizes) and len(set(sizes)) == 4


@pytest.mark.parametrize("quality, least", [("320", 0.99995), ("128", 0.9995)])
def test_the_mp3_sounds_like_what_went_in(tmp_path: Path, quality: str, least: float) -> None:
    """Guards against libsndfile's MP3 encoder distorting stereo 16-bit integers (a plain sine came back with a correlation of 0.47 and peaks past
    full scale): the Encoder hands it floating point instead. The file is also sample-aligned with the original (no delay)."""
    path = tmp_path / "song.mp3"
    encode_pcm(io.BytesIO(PCM), path, "mp3", quality)
    original = np.frombuffer(PCM, dtype="<i2").reshape(-1, 2).astype(float)
    heard = decoded(path).astype(float)
    assert len(heard) == len(original), "exactly as long as what went in"
    for channel in (0, 1):
        assert float(np.corrcoef(original[6000:-6000, channel], heard[6000:-6000, channel])[0, 1]) > least
    assert np.abs(heard).max() <= np.abs(original).max() * 1.05, "no peaks past what the music had"


# ---------------------------------------------------------------------------------------- the rules


def test_qualities_belong_to_their_format() -> None:
    check_choice("flac", "standard")
    check_choice("mp3", "320")
    for fmt, quality in (("mp3", "standard"), ("flac", "320"), ("wav", "standard"), ("mp3", "v0")):
        with pytest.raises(ValueError):
            check_choice(fmt, quality)
    assert encode.DEFAULT_QUALITY["mp3"] in encode.QUALITIES["mp3"] and encode.DEFAULT_QUALITY["flac"] in encode.QUALITIES["flac"]


def test_audio_must_come_in_whole_stereo_frames(tmp_path: Path) -> None:
    with Encoder(tmp_path / "x.flac", "flac") as encoder, pytest.raises(ValueError, match="whole stereo frames"):
        encoder.write(b"\0\0\0")


def test_a_failure_leaves_nothing_behind(tmp_path: Path) -> None:
    path = tmp_path / "song.flac.part"

    class Stop(Exception):
        pass

    def check() -> None:
        raise Stop

    with pytest.raises(Stop):
        encode_pcm(io.BytesIO(PCM), path, "flac", check=check)
    assert not path.exists()


def test_a_file_that_cannot_be_created_is_explained(tmp_path: Path) -> None:
    with pytest.raises(EncodeError, match="could not be created"):
        Encoder(tmp_path / "no" / "such" / "folder" / "song.flac", "flac")


def test_a_missing_audio_library_is_explained(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import builtins

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "soundfile":
            raise OSError("sndfile library not found")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    with pytest.raises(EncodeError, match="could not be loaded"):
        Encoder(tmp_path / "x.flac", "flac")


def test_the_mp3_is_as_loud_as_what_went_in(tmp_path: Path) -> None:
    path = tmp_path / "song.mp3"
    encode_pcm(io.BytesIO(PCM), path, "mp3", "320")
    original = np.frombuffer(PCM, dtype="<i2").reshape(-1, 2).astype(float)[6000:-6000]
    heard = decoded(path).astype(float)[6000:-6000]
    assert float(np.sqrt((heard**2).mean()) / np.sqrt((original**2).mean())) == pytest.approx(1.0, abs=0.01)


def test_a_full_disk_is_explained_in_words_and_any_other_failure_says_what_it_was(tmp_path: Path) -> None:
    from types import SimpleNamespace

    def full(_samples) -> None:
        raise OSError(28, "No space left on device")

    def other(_samples) -> None:
        raise RuntimeError("the encoder broke")

    with Encoder(tmp_path / "a.flac", "flac") as encoder:
        encoder._file, real = SimpleNamespace(write=full, close=lambda: None), encoder._file
        with pytest.raises(EncodeError, match="not enough room"):
            encoder.write(PCM[:400])
        encoder._file = SimpleNamespace(write=other, close=lambda: None)
        with pytest.raises(EncodeError, match="the encoder broke"):
            encoder.write(PCM[:400])
        encoder._file = real


# ---------------------------------------------------------------------------------------- a drive that has no room left


def room_left(monkeypatch: pytest.MonkeyPatch, free: int, folder: Path | None = None) -> None:
    """The drive has `free` bytes left. With `folder`, it is that folder (the one the song is being written to) that the drive is asked about."""

    def disk_usage(asked: str | Path) -> SimpleNamespace:
        assert folder is None or Path(asked) == folder, f"the room was looked at in {asked}, not where the song is written ({folder})"
        return SimpleNamespace(total=10**12, used=10**12 - free, free=free)

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)


def refusing_library(monkeypatch: pytest.MonkeyPatch, error: BaseException, *, at: str) -> None:
    """The audio library with a file that fails at `at` ("create", "write" or "close") with `error`, as libsndfile's does on a drive that has run out of room
    (found on one): it has taken the first bytes of the song, so there is a half-written file when it refuses."""

    class File:
        def __init__(self, path: str, *args: object, **kwargs: object) -> None:
            if at == "create":
                raise error
            Path(path).write_bytes(b"half")

        def write(self, samples: np.ndarray) -> None:
            if at == "write":
                raise error

        def close(self) -> None:
            if at == "close":
                raise error

    monkeypatch.setattr(encode, "_library", lambda: SimpleNamespace(SoundFile=File))


REFUSALS = [
    pytest.param(sf.LibsndfileError(2), id="system error"),  # (all that libsndfile says of a write that the system refused)
    pytest.param(RuntimeError("Error : problem with initialization of the flac decoder."), id="flac header"),  # (what FLAC says when not even its header fits)
]


@pytest.mark.parametrize("fmt", ["flac", "mp3"])
@pytest.mark.parametrize("at,what", [("create", "created"), ("write", "written"), ("close", "finished")])
@pytest.mark.parametrize("refusal", REFUSALS)
@pytest.mark.parametrize("full", [True, False])
def test_a_write_the_audio_library_refuses_is_a_full_drive_only_if_the_drive_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str, at: str, what: str, refusal: BaseException, full: bool
) -> None:
    refusing_library(monkeypatch, refusal, at=at)
    room_left(monkeypatch, 0 if full else PLENTY, tmp_path)
    part = tmp_path / f"song.{fmt}.part"
    with pytest.raises(EncodeError) as raised:
        encode_pcm(io.BytesIO(PCM), part, fmt)
    if full:
        assert type(raised.value) is NoRoomError and str(raised.value) == NO_ROOM == "There is not enough room on the drive for the music."
    else:
        assert type(raised.value) is EncodeError and str(raised.value) == f"The file could not be {what} ({refusal})."
    assert not part.exists(), "nothing half-written is left"


def test_the_room_is_looked_at_before_what_was_half_written_is_taken_away(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a drive of 1 MiB the half-written file is the whole drive: once it is taken away the drive looks as if it had room, and a check made then says so."""
    part = tmp_path / "song.flac.part"
    refusing_library(monkeypatch, sf.LibsndfileError(2), at="write")

    def disk_usage(folder: Path) -> SimpleNamespace:
        free = 0 if part.exists() else PLENTY
        return SimpleNamespace(total=10**12, used=10**12 - free, free=free)

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    with pytest.raises(NoRoomError, match="^There is not enough room on the drive for the music.$"):
        encode_pcm(io.BytesIO(PCM), part, "flac")
    assert not part.exists()


@pytest.mark.parametrize("number,said", [
    (errno.ENOSPC, NO_ROOM),  # (the error number of a plain write that found no room)
    (errno.EIO, "The file could not be written ([Errno 5] Input/output error)."),
])
def test_an_error_number_decides_for_a_write_that_has_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, number: int, said: str) -> None:
    refusing_library(monkeypatch, OSError(number, os.strerror(number)), at="write")
    room_left(monkeypatch, PLENTY)  # (whatever the drive says: the number is the system's own word for what happened)
    with pytest.raises(EncodeError) as raised:
        encode_pcm(io.BytesIO(PCM), tmp_path / "song.flac.part", "flac")
    assert str(raised.value) == said


def test_a_folder_called_outer_space_is_not_taken_for_a_full_drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """libsndfile puts the file's name in what it says when it cannot open one, so a check of the words would take this folder for a full drive."""
    song = tmp_path / "Outer Space" / "Album" / "01 - Song.flac.part"  # (the folders are not there: the real library cannot open the file)
    with pytest.raises(EncodeError) as raised:
        Encoder(song, "flac")
    assert type(raised.value) is EncodeError and "could not be created" in str(raised.value)
    assert "Outer Space" in str(raised.value), "the premise: the folder's name is in the library's words"
    refusing_library(monkeypatch, RuntimeError("Error writing '/music/Outer Space/01 - Song.flac.part': something else went wrong"), at="write")
    room_left(monkeypatch, PLENTY)
    with pytest.raises(EncodeError) as raised:
        encode_pcm(io.BytesIO(PCM), tmp_path / "song.flac.part", "flac")
    assert type(raised.value) is EncodeError and "something else went wrong" in str(raised.value)


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="Linux's /dev/full refuses every write for want of room, as a full drive does")
@pytest.mark.parametrize("fmt", ["flac", "mp3"])
def test_the_real_audio_library_refusing_a_write_is_a_full_drive_if_the_drive_says_it_is_full(monkeypatch: pytest.MonkeyPatch, fmt: str) -> None:
    """The real library, not a stand-in: what it raises when the system refuses a write is what the check looks for. (/dev itself has room, so the room
    the drive reports is the stand-in here; and the Encoder is used without `encode_pcm`, which would remove the file it was asked to write.)"""
    with pytest.raises(EncodeError) as raised, Encoder(Path("/dev/full"), fmt) as encoder:
        encoder.write(PCM)
    assert type(raised.value) is EncodeError and "could not be written" in str(raised.value), "the drive /dev is on has room: a refusal alone is not a full drive"
    room_left(monkeypatch, 0)
    with pytest.raises(NoRoomError, match="^There is not enough room on the drive for the music.$"), Encoder(Path("/dev/full"), fmt) as encoder:
        encoder.write(PCM)


def test_a_stream_that_is_not_at_its_start_is_encoded_from_its_start(tmp_path: Path) -> None:
    source = io.BytesIO(PCM)
    source.seek(0, 2)
    path = tmp_path / "song.flac"
    encode_pcm(source, path, "flac")
    assert decoded(path).tobytes() == PCM


def test_mp3_is_256_kbps_unless_another_is_chosen(tmp_path: Path) -> None:
    path = tmp_path / "song.mp3"
    with Encoder(path, "mp3") as encoder:
        encoder.write(PCM)
    assert abs(MP3(path).info.bitrate / 1000 - 256) <= 1
