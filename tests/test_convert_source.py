"""What a song file is, as far as converting goes: how the codec family is told, what a refusal says, and what Windows says of the songs only it reads."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from musictoolkit.convert import source
from musictoolkit.convert.source import SourceError, from_windows, inspect_source


@pytest.mark.parametrize("file_format,subtype,expected", [
    ("MP3", "MPEG_LAYER_III", ("mp3", True)), ("OGG", "VORBIS", ("ogg", True)), ("OGG", "OPUS", ("opus", True)), ("FLAC", "PCM_16", ("flac", False)),
    ("WAV", "PCM_16", ("wav", False)), ("WAVEX", "PCM_24", ("wav", False)), ("WAV", "ULAW", ("wav", False)),
    ("WAV", "MPEG_LAYER_III", ("mp3", True)),  # (an MP3 stream inside a WAV file: rare, and still lossy)
    ("AIFF", "PCM_16", ("other", False)), ("AIFF", "FLOAT", ("other", False)), ("CAF", "ALAC_16", ("other", True)), ("W64", "GSM610", ("other", True)),
])
def test_the_family_of_a_file_and_whether_it_is_lossy(file_format: str, subtype: str, expected: tuple[str, bool]) -> None:
    assert source._family(file_format, subtype) == expected


def test_a_file_that_cannot_be_opened_is_refused_in_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "a.flac"
    path.write_bytes(b"x")

    def refuse(name: str) -> None:
        raise OSError("the drive went away")

    monkeypatch.setattr(sf, "info", refuse)
    with pytest.raises(SourceError, match=r"^The song cannot be read \(the drive went away\)\.$"):
        inspect_source(path)


def test_a_file_that_says_it_has_no_sample_rate_is_refused_as_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "a.wav"
    path.write_bytes(b"x")
    monkeypatch.setattr(sf, "info", lambda name: SimpleNamespace(frames=1000, samplerate=0, channels=2, format="WAV", subtype="PCM_16"))
    with pytest.raises(SourceError, match="There is no sound in this file"):
        inspect_source(path)


def test_a_wav_file_of_another_kind_of_sound_is_taken_to_have_16_bits(tmp_path: Path) -> None:
    path = tmp_path / "a.wav"
    sf.write(str(path), (0.3 * np.sin(np.arange(8000) / 20)).astype("float32"), 8000, format="WAV", subtype="ULAW")
    info = inspect_source(path)
    assert (info.family, info.lossy, info.bits, info.kbps) == ("wav", False, 16, 0)


def test_what_windows_says_of_a_song_only_it_reads(tmp_path: Path) -> None:
    path = tmp_path / "a.m4a"
    path.write_bytes(b"x" * 8000)
    got = from_windows(path, {"rate": 44100, "channels": 2, "seconds": 2.0})
    assert (got.family, got.rate, got.channels, got.bits, got.lossy, got.via, got.size, got.kbps) == ("m4a", 44100, 2, 0, True, "windows", 8000, 32)
    assert got.seconds == 2.0 and got.path == str(path)


def test_the_size_windows_is_given_is_used_and_the_file_is_not_looked_at(tmp_path: Path) -> None:
    got = from_windows(tmp_path / "not there.wma", {"rate": 44100, "channels": 2, "seconds": 2.0}, size=16000)
    assert (got.size, got.kbps, got.family) == (16000, 64, "wma")


def test_without_a_length_or_a_size_there_is_no_bitrate_and_no_slip(tmp_path: Path) -> None:
    path = tmp_path / "a.aac"
    path.write_bytes(b"x" * 8000)
    assert from_windows(path, {"rate": 44100, "channels": 2, "seconds": 0}).kbps == 0  # (no length: nothing to divide by)
    assert from_windows(tmp_path / "gone.aac", {"rate": 44100, "channels": 2, "seconds": 2.0}).kbps == 0  # (no size: nothing was measured)


def test_numbers_windows_does_not_give_are_nothing() -> None:
    got = from_windows(Path("/music/a.mp4"), {})
    assert (got.rate, got.channels, got.seconds, got.kbps, got.family) == (0, 0, 0.0, 0, "mp4")
