"""The real ffmpeg: what it makes of real songs, that what comes out is MP3 of the bitrate asked for, as long as the song, without tags or a picture, and
whatever the song was (a FLAC at 24 bits, six channels, a file that is not audio). They are skipped where there is no ffmpeg with an MP3 encoder."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from musictoolkit.transcode import Transcoder
from tests.conftest import add_cover, make_track

FIXTURES = Path(__file__).parent / "fixtures"
HAVE = Transcoder().available()
pytestmark = pytest.mark.skipif(not HAVE, reason="no ffmpeg with an MP3 encoder (sudo apt install ffmpeg)")


@pytest.fixture
def real(tmp_path: Path):
    made = Transcoder(cache_dir=tmp_path / "cache", cache_mb=50, jobs=2)
    yield made
    made.close()


def make(real: Transcoder, track_id: int, path: Path, kbps: int) -> bytes:
    job = real.job(real.key(track_id, path, kbps), path, kbps)
    assert job.done.wait(60), "ffmpeg did not finish"
    assert job.ok, job.error
    data = b"".join(real.follow(job))
    assert real.cached(real.key(track_id, path, kbps)).read_bytes() == data  # (what was handed out is what is kept)
    return data


def tone(path: Path, seconds: float, rate: int, channels: int, subtype: str = "PCM_16", fmt: str = "FLAC") -> Path:
    times = np.arange(int(seconds * rate)) / rate
    wave = (0.3 * np.sin(2 * np.pi * 440 * times)).astype("float32")
    sf.write(path, np.column_stack([wave] * channels) if channels > 1 else wave, rate, subtype=subtype, format=fmt)
    return path


def info(path: Path):
    return sf.info(path)


@pytest.mark.parametrize("kbps", [64, 128, 320])
def test_a_song_comes_out_as_mp3_at_the_bitrate_asked_for_and_as_long_as_it_was(real: Transcoder, tmp_path: Path, kbps: int) -> None:
    data = make(real, 1, FIXTURES / "tone_12s.mp3", kbps)
    assert abs(len(data) - 12 * kbps * 125) < 0.02 * 12 * kbps * 125  # (constant bitrate: its seconds times its bytes a second, to the frame)
    assert data[:3] != b"ID3" and data[0] == 0xFF and data[1] & 0xE0 == 0xE0  # (no tag in front: it begins with a frame)
    kept = tmp_path / "out.mp3"
    kept.write_bytes(data)
    heard = info(kept)
    assert heard.samplerate == 44100 and heard.channels == 2 and abs(heard.duration - 12.0) < 0.2


def test_a_song_without_a_xing_header_and_without_the_tags_and_the_picture_of_the_file(real: Transcoder, tmp_path: Path) -> None:
    path = make_track(tmp_path, "with-a-cover.mp3", title="Tagged", artist="Someone")
    add_cover(path)
    assert b"APIC" in path.read_bytes() and path.read_bytes()[:3] == b"ID3"  # (the file has both)
    data = make(real, 2, path, 128)
    assert b"Xing" not in data[:200] and b"Info" not in data[:200] and b"ID3" not in data and b"APIC" not in data and b"Tagged" not in data
    assert data[0] == 0xFF


def test_a_flac_of_24_bits_at_a_high_rate_and_a_mono_one_become_stereo_mp3_at_44100(real: Transcoder, tmp_path: Path) -> None:
    for number, (rate, channels, subtype) in enumerate([(96000, 2, "PCM_24"), (48000, 1, "PCM_16"), (44100, 1, "PCM_24")]):
        path = tone(tmp_path / f"s{number}.flac", 3.0, rate, channels, subtype)
        data = make(real, 10 + number, path, 96)
        out = tmp_path / f"o{number}.mp3"
        out.write_bytes(data)
        heard = info(out)
        assert heard.samplerate == 44100 and heard.channels == 2 and abs(heard.duration - 3.0) < 0.15
        assert abs(len(data) - 3 * 96 * 125) < 0.03 * 3 * 96 * 125


def test_a_song_of_six_channels_is_mixed_down_and_not_refused(real: Transcoder, tmp_path: Path) -> None:
    path = tone(tmp_path / "surround.wav", 2.0, 48000, 6, "PCM_16", "WAV")
    data = make(real, 20, path, 128)
    out = tmp_path / "o.mp3"
    out.write_bytes(data)
    assert info(out).channels == 2 and abs(info(out).duration - 2.0) < 0.15


def test_a_second_request_is_a_plain_file_and_ffmpeg_is_not_run_again(real: Transcoder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = FIXTURES / "tone_12s.mp3"
    make(real, 3, path, 64)
    key = real.key(3, path, 64)
    monkeypatch.setattr(real, "_arguments", lambda *a, **k: pytest.fail("ffmpeg was asked again for a song that is kept"))
    assert real.cached(key) is not None


def test_a_song_made_from_a_time_is_as_long_as_what_is_left(real: Transcoder) -> None:
    data = b"".join(real.direct(FIXTURES / "tone_12s.mp3", 64, 6.0))
    assert abs(len(data) - 6 * 64 * 125) < 0.05 * 6 * 64 * 125


def test_a_file_that_is_not_audio_is_an_error_with_what_ffmpeg_said_and_nothing_is_kept(real: Transcoder, tmp_path: Path) -> None:
    path = tmp_path / "notes.flac"
    path.write_text("this is not audio at all", encoding="utf-8")
    job = real.job(real.key(4, path, 128), path, 128)
    assert job.done.wait(60) and not job.ok and job.error
    assert list((tmp_path / "cache").iterdir()) == []


def test_ffmpeg_reports_itself(real: Transcoder) -> None:
    status = real.status()
    assert status["available"] is True and status["reason"] is None and status["ffmpeg"] and shutil.which("ffmpeg")
