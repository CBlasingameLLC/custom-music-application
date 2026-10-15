"""Writing FLAC and MP3 from the audio of a disc."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC
from mutagen.mp3 import MP3, BitrateMode

from musictoolkit.discs import encode
from musictoolkit.discs.encode import Encoder, EncodeError, check_choice, encode_pcm
from tests.fake_disc_helper import audio

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
