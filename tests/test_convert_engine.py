"""Converting one song file into another: what a file is, what is to be done with it, and doing it, measured on real audio."""

from __future__ import annotations

import errno
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import numpy as np
import pytest
import soundfile as sf

from musictoolkit.convert import engine, formats
from musictoolkit.convert.engine import ConvertError, Spec
from musictoolkit.convert.source import SourceError, SourceInfo, inspect_source
from tests import convert_audio as audio

# ---------------------------------------------------------------------------------------- what a song file is


@pytest.mark.parametrize("file_format,subtype,family,lossy,bits", [
    ("WAV", "PCM_16", "wav", False, 16), ("WAV", "PCM_24", "wav", False, 24), ("FLAC", "PCM_16", "flac", False, 16), ("FLAC", "PCM_24", "flac", False, 24),
    ("OGG", "VORBIS", "ogg", True, 0), ("OGG", "OPUS", "opus", True, 0), ("MP3", "MPEG_LAYER_III", "mp3", True, 0), ("AIFF", "PCM_16", "other", False, 16),
])
def test_what_a_song_file_is(tmp_path: Path, file_format: str, subtype: str, family: str, lossy: bool, bits: int) -> None:
    path = tmp_path / "song"
    sf.write(str(path), audio.music(48000, 4), 48000, format=file_format, subtype=subtype)
    info = inspect_source(path)
    assert (info.family, info.rate, info.channels, info.bits, info.lossy, info.via) == (family, 48000, 2, bits, lossy, "")
    assert info.seconds == pytest.approx(4.0, abs=0.05) and info.frames == pytest.approx(4 * 48000, abs=2400)
    assert (info.kbps > 0) is lossy and info.size == path.stat().st_size


def test_a_lossy_songs_bitrate_comes_from_its_size_and_length(tmp_path: Path) -> None:
    path = tmp_path / "a.mp3"
    sf.write(str(path), audio.music(44100, 6), 44100, format="MP3", subtype="MPEG_LAYER_III", bitrate_mode="CONSTANT", compression_level=0.19)
    assert 250 <= inspect_source(path).kbps <= 265


def test_a_song_that_is_missing_unreadable_or_silent_is_refused_in_words(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="The file is not there any more"):
        inspect_source(tmp_path / "gone.flac")
    broken = tmp_path / "broken.mp3"
    broken.write_bytes(b"\0" * 3000)
    with pytest.raises(SourceError, match=r"^The song cannot be read \("):
        inspect_source(broken)
    empty = audio.write_song(tmp_path / "empty.wav", np.zeros((0, 2), dtype=np.int16), 44100, "PCM_16")
    with pytest.raises(SourceError, match="There is no sound in this file"):
        inspect_source(empty)


# ---------------------------------------------------------------------------------------- what is to be done with it


def song(family: str = "flac", rate: int = 44100, channels: int = 2, bits: int = 16, kbps: int = 0, via: str = "") -> SourceInfo:
    lossy = family in ("mp3", "ogg", "opus", "m4a", "wma", "aac", "mp4")
    return SourceInfo(f"/music/a.{family}", family, rate, channels, 0 if lossy else bits, 200.0, lossy, kbps, 1000, via)


@pytest.mark.parametrize("info,spec,action,notes", [
    # already in the format and as good as asked for: copied, or left out, or converted when the person says so
    (song("flac"), Spec("flac"), "copy", ["Already in this format: copied as it is."]),
    (song("flac"), Spec("flac", existing="skip"), "skip", ["Already in this format: left out."]),
    (song("flac"), Spec("flac", existing="convert"), "convert", []),
    (song("wav", bits=24), Spec("wav", depth="keep"), "copy", ["Already in this format: copied as it is."]),
    (song("mp3", kbps=192), Spec("mp3", "256"), "copy", ["Already in this format: copied as it is."]),
    (song("mp3", kbps=268), Spec("mp3", "256"), "copy", ["Already in this format: copied as it is."]),  # (5% over still counts as at the bitrate)
    (song("mp3", kbps=270), Spec("mp3", "256"), "convert", ["A lossy song converted to another lossy format loses a little more quality."]),
    (song("mp3", kbps=320), Spec("mp3", "128"), "convert", ["A lossy song converted to another lossy format loses a little more quality."]),
    (song("mp3", rate=48000, kbps=256), Spec("mp3", "320"), "copy", ["Already in this format: copied as it is."]),
    (song("opus", rate=48000, kbps=100), Spec("opus", "128"), "copy", ["Already in this format: copied as it is."]),
    (song("opus", rate=48000, kbps=168), Spec("opus", "160"), "copy", ["Already in this format: copied as it is."]),  # (160 kbps and a twentieth: exactly 168)
    (song("opus", rate=48000, kbps=169), Spec("opus", "160"), "convert", ["A lossy song converted to another lossy format loses a little more quality."]),
    (song("mp3", kbps=336), Spec("mp3", "320"), "copy", ["Already in this format: copied as it is."]),
    (song("mp3", kbps=337), Spec("mp3", "320"), "convert", ["A lossy song converted to another lossy format loses a little more quality."]),
    # a song in the format is only as it is if it needs no change of rate: what MP3 is written at here, and a cap
    (song("mp3", rate=22050, kbps=64), Spec("mp3", "256"), "convert", ["Resampled from 22.05 kHz to 44.1 kHz.", "A lossy song converted to another lossy format loses a little more quality."]),
    (song("mp3", rate=48000, kbps=128), Spec("mp3", "256", max_rate=44100), "convert", ["Resampled from 48 kHz to 44.1 kHz.", "A lossy song converted to another lossy format loses a little more quality."]),
    (song("flac", rate=96000), Spec("flac", max_rate=48000, existing="copy"), "convert", ["Resampled from 96 kHz to 48 kHz."]),
    # a song that is not as asked for is converted, whatever is to be done with those that are
    (song("flac", bits=24), Spec("flac", depth="16", existing="skip"), "convert", ["Reduced from 24 to 16 bits (with a little dither)."]),
    (song("mp3", kbps=320), Spec("mp3", "128", existing="skip"), "convert", ["A lossy song converted to another lossy format loses a little more quality."]),
    (song("flac", rate=96000), Spec("flac", max_rate=48000, existing="skip"), "convert", ["Resampled from 96 kHz to 48 kHz."]),
    # not in the format, or not as asked for
    (song("flac", bits=24), Spec("flac", depth="16"), "convert", ["Reduced from 24 to 16 bits (with a little dither)."]),
    (song("flac", bits=24), Spec("flac", depth="keep"), "copy", ["Already in this format: copied as it is."]),
    (song("flac", rate=96000), Spec("flac", max_rate=48000), "convert", ["Resampled from 96 kHz to 48 kHz."]),
    (song("flac", rate=88200, bits=24), Spec("wav", max_rate=48000, depth="16"), "convert", ["Resampled from 88.2 kHz to 44.1 kHz.", "Reduced from 24 to 16 bits (with a little dither)."]),
    (song("flac"), Spec("mp3", "256"), "convert", []),
    (song("flac", rate=96000), Spec("mp3", "320"), "convert", ["Resampled from 96 kHz to 48 kHz."]),
    (song("wav"), Spec("flac"), "convert", []),
    (song("mp3", kbps=256), Spec("opus", "128"), "convert", ["Resampled from 44.1 kHz to 48 kHz.", "A lossy song converted to another lossy format loses a little more quality."]),
    (song("mp3", kbps=256), Spec("flac"), "convert", ["A lossy song does not get its quality back: the file only gets bigger."]),
    (song("ogg", kbps=160), Spec("wav"), "convert", ["A lossy song does not get its quality back: the file only gets bigger."]),
    # a song only Windows decodes is never copied, whatever it is going to
    (song("m4a", kbps=256, via="windows"), Spec("mp3", "256"), "convert", ["Windows decodes this M4A song first.", "A lossy song converted to another lossy format loses a little more quality."]),
    (song("wma", kbps=128, via="windows"), Spec("flac"), "convert", ["Windows decodes this WMA song first.", "A lossy song does not get its quality back: the file only gets bigger."]),
    (song("mp3", kbps=128, via="windows"), Spec("mp3", "256"), "convert", ["Windows decodes this MP3 song first.", "A lossy song converted to another lossy format loses a little more quality."]),
    (song("flac", via="windows"), Spec("flac", existing="skip"), "convert", ["Windows decodes this FLAC song first."]),
])
def test_what_becomes_of_a_song(info: SourceInfo, spec: Spec, action: str, notes: list[str]) -> None:
    treatment = engine.decide(info, spec)
    assert (treatment.action, treatment.notes) == (action, notes)


def test_what_a_converted_song_will_be_like() -> None:
    wanted = engine.decide(song("flac", rate=88200, bits=24), Spec("flac", max_rate=48000, depth="keep"))
    assert (wanted.rate, wanted.channels, wanted.bits) == (44100, 2, 24)
    lossy = engine.decide(song("flac", rate=44100), Spec("opus", "96"))
    assert (lossy.rate, lossy.bits) == (48000, 0)  # (a lossy format has no depth)
    mono = engine.decide(song("flac", rate=22050, channels=1), Spec("mp3", "128"))
    assert (mono.rate, mono.channels) == (44100, 1)
    copied = engine.decide(song("mp3", rate=48000, kbps=128), Spec("mp3", "256"))
    assert (copied.rate, copied.channels, copied.bits) == (48000, 2, 0)


@pytest.mark.parametrize("channels", [0, 3, 6])
def test_a_song_with_more_than_two_channels_cannot_be_converted(channels: int) -> None:
    treatment = engine.decide(song("flac", channels=channels), Spec("mp3", "256"))
    assert treatment.action == "cannot" and treatment.notes == [f"This song has {channels} channels; Convert handles mono and stereo."]


def test_a_choice_that_is_not_offered_is_refused_in_words() -> None:
    for spec, words in [(Spec("aac"), "format must be one of"), (Spec("mp3", "100"), "quality for mp3 must be one of"), (Spec("flac", max_rate=96000), "the highest rate"),
                        (Spec("flac", depth="32"), "depth must be one of"), (Spec("flac", existing="overwrite"), "existing songs must be one of")]:
        with pytest.raises(ValueError, match=words):
            spec.checked()


# ---------------------------------------------------------------------------------------- doing it: lossless


def convert(src: Path, out: Path, spec: Spec, **kwargs) -> engine.Converted:
    return engine.convert_file(src, out, inspect_source(src), spec, **kwargs)


def test_a_16_bit_song_passes_between_flac_and_wav_sample_for_sample(tmp_path: Path) -> None:
    noise = audio.noise16(44100, 3)
    flac = audio.write_song(tmp_path / "a.flac", noise, 44100, "PCM_16")
    as_wav = tmp_path / "a.wav"
    result = convert(flac, as_wav, Spec("wav"))
    assert (result.rate, result.channels, result.frames) == (44100, 2, len(noise)) and result.size == as_wav.stat().st_size
    back = tmp_path / "b.flac"
    convert(as_wav, back, Spec("flac"))
    for path in (as_wav, back):
        assert np.array_equal(audio.read(path, "int16")[0], noise)
    again = tmp_path / "c.flac"
    convert(back, again, Spec("flac", existing="convert"))  # (a song converted to its own format anyway)
    assert np.array_equal(audio.read(again, "int16")[0], noise)


def test_a_24_bit_song_keeps_its_depth_exactly_or_comes_down_to_16_bits(tmp_path: Path) -> None:
    exact = audio.left_justified_24(48000, 2)
    flac = audio.write_song(tmp_path / "a.flac", exact, 48000, "PCM_24")
    assert inspect_source(flac).bits == 24
    kept = tmp_path / "kept.wav"
    convert(flac, kept, Spec("wav", depth="keep"))
    assert sf.info(str(kept)).subtype == "PCM_24" and np.array_equal(audio.read(kept, "int32")[0], exact)
    lowered, again = tmp_path / "low.wav", tmp_path / "low2.wav"
    convert(flac, lowered, Spec("wav", depth="16"))
    convert(flac, again, Spec("wav", depth="16"))
    assert sf.info(str(lowered)).subtype == "PCM_16"
    difference = audio.read(lowered, "int16")[0].astype(np.int32) - (exact >> 16)
    assert np.abs(difference).max() <= 2  # (rounded, with one least-significant bit of dither)
    assert np.count_nonzero(difference) > difference.size // 4  # (not just cut off at 16 bits, which would leave every sample where `>> 16` puts it)
    assert lowered.read_bytes() == again.read_bytes()  # (the same song is always converted to the same bytes)


def test_a_lossy_song_is_written_at_16_bits_with_dither_not_passed_through(tmp_path: Path) -> None:
    """A lossless song at the same rate and depth is copied sample for sample, but what a lossy song decodes to is floating point: it is
    brought down to 16 bits with the same dither as any other song that ends up there."""
    mp3 = audio.write_song(tmp_path / "a.mp3", audio.music(44100, 4), 44100, "MPEG_LAYER_III", bitrate_mode="CONSTANT", compression_level=0.19)
    assert inspect_source(mp3).lossy
    out, again = tmp_path / "a.flac", tmp_path / "b.flac"
    convert(mp3, out, Spec("flac", existing="convert"))
    convert(mp3, again, Spec("flac", existing="convert"))
    assert sf.info(str(out)).subtype == "PCM_16" and out.read_bytes() == again.read_bytes()
    plain = audio.read(mp3, "int16")[0]  # (the decoder's own 16-bit numbers, rounded and with no dither)
    made = audio.read(out, "int16")[0]
    difference = made.astype(np.int32) - plain[: len(made)].astype(np.int32)
    assert np.abs(difference).max() <= 2  # (the song itself, within a bit or two)
    assert np.count_nonzero(difference) > len(made) // 8  # (and not those numbers: a good part of them were moved by the dither)


def test_a_sound_quieter_than_the_last_bit_is_still_there_on_average_because_of_the_dither(tmp_path: Path) -> None:
    """What dither is for: without it, a sound smaller than one least-significant bit is rounded away to silence."""
    quiet = np.full((44100 * 3, 2), 0.3 / 32768, dtype=np.float32)  # (three tenths of a bit)
    src = audio.write_song(tmp_path / "quiet.wav", quiet, 44100, "FLOAT")
    out = tmp_path / "quiet.flac"
    convert(src, out, Spec("flac", depth="16"))
    made = audio.read(out, "int16")[0].astype(np.int64)
    assert set(np.unique(made)) <= {-1, 0, 1, 2} and made.mean() == pytest.approx(0.3, abs=0.02)


def test_a_32_bit_float_song_comes_down_to_24_bits_when_the_depth_is_kept(tmp_path: Path) -> None:
    samples = audio.tones(44100, 2, hertz=(440.0,))
    path = audio.write_song(tmp_path / "a.wav", samples, 44100, "FLOAT")
    assert inspect_source(path).bits == 32
    out = tmp_path / "a.flac"
    convert(path, out, Spec("flac", depth="keep"))
    assert sf.info(str(out)).subtype == "PCM_24"
    assert np.abs(audio.read(out)[0] - samples).max() < 2e-7


# ---------------------------------------------------------------------------------------- doing it: lossy, and other rates


def loud(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


@pytest.mark.parametrize("fmt,quality,rate", [("mp3", "256", 44100), ("mp3", "128", 44100), ("ogg", "160", 44100), ("opus", "128", 48000)])
def test_a_lossy_song_keeps_its_pitch_its_level_and_its_length(tmp_path: Path, fmt: str, quality: str, rate: int) -> None:
    src = audio.write_song(tmp_path / "a.flac", audio.tones(44100, 6), 44100, "PCM_16")
    out = tmp_path / f"a.{formats.FORMATS[fmt].extension}"
    result = convert(src, out, Spec(fmt, quality))
    assert (result.rate, result.channels) == (rate, 2)
    back, back_rate = audio.read(out)
    assert back_rate == rate and len(back) / back_rate == pytest.approx(6.0, abs=0.06)
    middle = back[back_rate : 5 * back_rate]
    assert audio.peak_hertz(middle[:, 0], back_rate) == pytest.approx(440, abs=4) and audio.peak_hertz(middle[:, 1], back_rate) == pytest.approx(660, abs=4)
    assert loud(middle[:, 0]) == pytest.approx(0.4 / np.sqrt(2), rel=0.07)


@pytest.mark.parametrize("fmt,quality", [("mp3", "256"), ("ogg", "160"), ("opus", "128")])
def test_a_song_that_goes_beyond_full_scale_is_clipped_before_a_lossy_encoder_is_given_it(tmp_path: Path, fmt: str, quality: str) -> None:
    t = np.arange(44100 * 3) / 44100
    hot = np.where(np.sin(2 * np.pi * 100 * t) >= 0, 1.5, -1.5).astype(np.float32)  # (a floating-point song can go beyond full scale)
    src = audio.write_song(tmp_path / "hot.wav", np.column_stack([hot, hot]), 44100, "FLOAT")
    out = tmp_path / f"hot.{formats.FORMATS[fmt].extension}"
    convert(src, out, Spec(fmt, quality))
    # (a square wave held at full scale comes back from an encoder at 1.15 to 1.25 times full scale at most; one handed 1.5 times full scale, at 1.7 to 2)
    assert np.abs(audio.read(out)[0]).max() < 1.4


def test_a_song_of_a_high_rate_is_brought_down_when_the_person_capped_it(tmp_path: Path) -> None:
    src = audio.write_song(tmp_path / "a.flac", audio.tones(96000, 5), 96000, "PCM_24")
    out = tmp_path / "a.flac.part"
    result = convert(src, out, Spec("flac", max_rate=48000))
    back, back_rate = audio.read(out)
    assert (result.rate, back_rate) == (48000, 48000) and abs(len(back) - 5 * 48000) <= 2 and result.frames == len(back)
    assert audio.peak_hertz(back[24000:-24000, 0], back_rate) == pytest.approx(440, abs=2)
    assert loud(back[24000:-24000, 0]) == pytest.approx(0.4 / np.sqrt(2), rel=0.01)


def level_near(samples: np.ndarray, rate: int, hertz: float, width: float = 100.0) -> float:
    """The strongest part of the spectrum within `width` hertz of `hertz` (one channel, Hann window)."""
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    frequencies = np.fft.rfftfreq(len(samples), 1 / rate)
    return float(spectrum[(frequencies > hertz - width) & (frequencies < hertz + width)].max())


def test_a_tone_the_new_rate_cannot_hold_is_filtered_out_and_not_folded_back_into_the_music(tmp_path: Path) -> None:
    rate = 96000
    t = np.arange(rate * 4) / rate
    tone = (0.3 * np.sin(2 * np.pi * 1000 * t) + 0.3 * np.sin(2 * np.pi * 30000 * t)).astype(np.float32)  # (at 48 kHz, 30 kHz would come back as 18 kHz)
    src = audio.write_song(tmp_path / "a.wav", np.column_stack([tone, tone]), rate, "PCM_24")
    out = tmp_path / "a.flac"
    convert(src, out, Spec("flac", max_rate=48000, depth="keep"))
    back, back_rate = audio.read(out)
    middle = back[back_rate // 2 : -back_rate // 2, 0].astype(np.float64)
    assert back_rate == 48000 and 20 * np.log10(level_near(middle, back_rate, 18000) / level_near(middle, back_rate, 1000)) < -120


def test_a_song_of_the_44_1_family_stays_in_it_under_a_48_khz_cap(tmp_path: Path) -> None:
    src = audio.write_song(tmp_path / "a.flac", audio.tones(88200, 3), 88200, "PCM_16")
    out = tmp_path / "a.wav"
    assert convert(src, out, Spec("wav", max_rate=48000)).rate == 44100 and sf.info(str(out)).samplerate == 44100


def test_a_song_a_format_cannot_hold_as_it_is_is_brought_to_a_rate_it_can(tmp_path: Path) -> None:
    for rate, fmt, expected in [(96000, "mp3", 48000), (22050, "mp3", 44100), (44100, "opus", 48000), (192000, "opus", 48000)]:
        src = audio.write_song(tmp_path / f"{rate}.flac", audio.tones(rate, 3), rate, "PCM_16")
        out = tmp_path / f"{rate}.{fmt}"
        result = convert(src, out, Spec(fmt))
        back, back_rate = audio.read(out)
        assert (result.rate, back_rate) == (expected, expected) and len(back) / back_rate == pytest.approx(3.0, abs=0.06), (rate, fmt)
        assert audio.peak_hertz(back[back_rate // 2 : -back_rate // 2, 0], back_rate) == pytest.approx(440, abs=4), (rate, fmt)


def test_a_mono_song_stays_mono(tmp_path: Path) -> None:
    src = audio.write_song(tmp_path / "a.flac", audio.tones(22050, 4, channels=1, hertz=(330.0,)), 22050, "PCM_16")
    for fmt, expected_rate in [("mp3", 44100), ("ogg", 22050), ("opus", 48000), ("flac", 22050), ("wav", 22050)]:
        out = tmp_path / f"converted.{formats.FORMATS[fmt].extension}"
        result = convert(src, out, Spec(fmt, existing="convert"))
        back, back_rate = audio.read(out)
        assert (result.channels, back.shape[1], back_rate) == (1, 1, expected_rate), fmt
        assert audio.peak_hertz(back[back_rate // 2 : -back_rate // 2, 0], back_rate) == pytest.approx(330, abs=4), fmt


def test_samples_beyond_full_scale_are_clipped_not_wrapped() -> None:
    rng = np.random.default_rng(1)
    quantised = engine._quantise16(np.array([[1.5, -1.5], [0.5, -0.5], [1.0, -1.0]], dtype=np.float32), rng)
    assert quantised[0].tolist() == [32767, -32768]  # (a wrap would turn 1.5 into -0.5)
    assert quantised[1].tolist() == pytest.approx([16384, -16384], abs=2) and quantised[2].tolist() == [32767, -32768]


def test_a_square_wave_right_up_to_full_scale_is_not_wrapped_when_it_is_brought_to_another_rate(tmp_path: Path) -> None:
    square = (0.999 * np.sign(audio.tones(48000, 2, hertz=(1000.0,)))).astype(np.float32)  # (resampling overshoots at every edge)
    src = audio.write_song(tmp_path / "a.wav", square, 48000, "PCM_16")
    out = tmp_path / "converted.flac"
    convert(src, out, Spec("flac", max_rate=44100))
    back, rate = audio.read(out)
    middle = back[rate // 10 : -rate // 10, 0]
    assert rate == 44100 and np.abs(middle).max() <= 1.0 and loud(middle) == pytest.approx(0.999, rel=0.05)  # (a little under: the resampler's filter takes off the highest harmonics)
    # a wrapped sample would be the wrong way up: a clean square wave is positive or negative for whole stretches, never for a sample or two
    signs = np.sign(middle[np.abs(middle) > 0.5])
    flips = np.count_nonzero(np.diff(signs))
    assert flips <= 2 * 2 * len(middle) / 44.1 / 1.0 + 4  # (1000 hertz: two edges a millisecond, with a little room)


# ---------------------------------------------------------------------------------------- the quality asked for is the quality got


@pytest.mark.parametrize("fmt,quality,tolerance", [
    ("mp3", "320", 0.03), ("mp3", "256", 0.03), ("mp3", "192", 0.03), ("mp3", "128", 0.03),  # (constant bitrate)
    ("ogg", "320", 0.2), ("ogg", "256", 0.2), ("ogg", "192", 0.2), ("ogg", "160", 0.2), ("ogg", "128", 0.2), ("ogg", "96", 0.2),  # (variable: about)
    ("opus", "192", 0.12), ("opus", "160", 0.12), ("opus", "128", 0.12), ("opus", "96", 0.12), ("opus", "64", 0.12),
])
def test_each_quality_gives_the_bitrate_it_is_called_after(tmp_path: Path, fmt: str, quality: str, tolerance: float) -> None:
    seconds = 12
    src = audio.write_song(tmp_path / "a.flac", audio.music(44100, seconds), 44100, "PCM_16")
    out = tmp_path / f"a.{formats.FORMATS[fmt].extension}"
    result = convert(src, out, Spec(fmt, quality))
    kbps = result.size * 8 / seconds / 1000
    assert kbps == pytest.approx(int(quality), rel=tolerance)


@pytest.mark.parametrize("fmt", ["mp3", "ogg", "opus"])
def test_a_better_quality_is_a_bigger_file(tmp_path: Path, fmt: str) -> None:
    src = audio.write_song(tmp_path / "a.flac", audio.music(44100, 8), 44100, "PCM_16")
    sizes = []
    for quality in formats.FORMATS[fmt].qualities:
        out = tmp_path / f"{quality.id}.{formats.FORMATS[fmt].extension}"
        sizes.append(convert(src, out, Spec(fmt, quality.id)).size)
    assert sizes == sorted(sizes, reverse=True) and len(set(sizes)) == len(sizes)


def test_a_constant_bitrate_is_the_same_for_mono_and_stereo_but_opus_goes_by_the_channel(tmp_path: Path) -> None:
    stereo = audio.write_song(tmp_path / "s.flac", audio.music(44100, 10), 44100, "PCM_16")
    mono = audio.write_song(tmp_path / "m.flac", audio.music(44100, 10, channels=1), 44100, "PCM_16")
    size = {}
    for name, src in (("stereo", stereo), ("mono", mono)):
        for fmt, quality in (("mp3", "192"), ("opus", "128")):
            size[name, fmt] = convert(src, tmp_path / f"{name}.{fmt}", Spec(fmt, quality)).size
    assert size["mono", "mp3"] == pytest.approx(size["stereo", "mp3"], rel=0.02)
    assert size["mono", "opus"] == pytest.approx(size["stereo", "opus"] / 2, rel=0.15)


# ---------------------------------------------------------------------------------------- progress, stopping and failing


def test_progress_counts_the_frames_of_the_song_as_they_are_read(tmp_path: Path) -> None:
    src = audio.write_song(tmp_path / "a.flac", audio.tones(44100, 12), 44100, "PCM_16")
    seen: list[tuple[int, int]] = []
    convert(src, tmp_path / "a.mp3", Spec("mp3"), on_progress=lambda done, total: seen.append((done, total)))
    assert {total for _, total in seen} == {12 * 44100}
    done = [d for d, _ in seen]
    assert done == sorted(done) and done[0] > 0 and done[-1] == 12 * 44100 and len(done) >= 3


class Stop(Exception):
    pass


@pytest.mark.parametrize("fmt", ["mp3", "flac"])
def test_a_stop_leaves_no_file_behind(tmp_path: Path, fmt: str) -> None:
    src = audio.write_song(tmp_path / "a.flac", audio.tones(44100, 12), 44100, "PCM_16")
    out = tmp_path / f"a.{fmt}.part"
    calls = 0

    def check() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise Stop

    with pytest.raises(Stop):
        convert(src, out, Spec(fmt, existing="convert"), check=check)
    assert not out.exists()


def test_a_file_that_cannot_be_created_is_said_so_in_words_and_leaves_nothing(tmp_path: Path) -> None:
    src = audio.write_song(tmp_path / "a.flac", audio.tones(44100, 2), 44100, "PCM_16")
    with pytest.raises(ConvertError, match=r"^The converted file could not be created \("):
        convert(src, tmp_path / "no-such-folder" / "a.mp3", Spec("mp3"))


def test_running_out_of_room_is_said_in_words_and_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = audio.write_song(tmp_path / "a.flac", audio.tones(44100, 2), 44100, "PCM_16")
    out = tmp_path / "a.mp3.part"

    class Full:
        def __enter__(self):
            out.write_bytes(b"half")
            return self

        def __exit__(self, *exc):
            return False

        def write(self, block):
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(engine, "_open_writer", lambda *args, **kwargs: Full())
    with pytest.raises(ConvertError, match="^There is not enough room on the drive for the converted songs.$"):
        convert(src, out, Spec("mp3"))
    assert not out.exists()


@pytest.mark.parametrize("exc,full", [
    (OSError(errno.ENOSPC, "No space left on device"), True),
    (OSError(errno.ENOSPC, "Il n'y a plus de place sur le disque"), True),  # (the number, when the words are another language's)
    (RuntimeError("System error : No space left on device."), True),  # (the words, when only they come through)
    (RuntimeError("Error opening '/music/Outer Space/01.flac': Format not recognised."), False),  # (a folder called Space is not a full drive)
    (OSError(errno.ENOENT, "No such file or directory", "/music/Outer Space/01.flac"), False),
    (OSError(errno.EIO, "Input/output error"), False),
])
def test_what_counts_as_the_drive_running_full(exc: BaseException, full: bool) -> None:
    assert engine.no_room(exc) is full


@pytest.mark.parametrize("free,full", [(0, True), (300 * 1024, True), (engine.LOW_ROOM - 1, True), (engine.LOW_ROOM, False), (100 * 1024**2, False), (50 * 1024**3, False)])
def test_the_audio_librarys_bare_system_error_is_a_full_drive_when_the_drive_is_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free: int, full: bool) -> None:
    monkeypatch.setattr(engine.shutil, "disk_usage", lambda folder: SimpleNamespace(total=10**12, used=10**12 - free, free=free))
    refused = sf.LibsndfileError(2)  # (all that libsndfile says of a write that the system refused: found on a drive that was really full)
    assert str(refused) == "System error."
    assert engine.no_room(refused, tmp_path) is full
    assert engine.no_room(refused) is False  # (with no folder there is no drive to look at)
    assert engine.no_room(RuntimeError("Something else went wrong."), tmp_path) is False  # (and only that one message is taken for it)


def test_a_drive_that_cannot_be_looked_at_is_not_taken_for_a_full_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def gone(folder: Path) -> None:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr(engine.shutil, "disk_usage", gone)
    assert engine.no_room(sf.LibsndfileError(2), tmp_path) is False


@pytest.mark.parametrize("full,said", [
    (True, "^There is not enough room on the drive for the converted songs.$"),
    (False, r"^The song could not be converted \(System error\.\)\.$"),
])
def test_a_write_the_audio_library_refuses_is_a_full_drive_only_if_the_drive_is_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full: bool, said: str) -> None:
    src = audio.write_song(tmp_path / "a.flac", audio.tones(44100, 2), 44100, "PCM_16")
    out = tmp_path / "a.mp3.part"

    class Refusing:
        def __enter__(self) -> Self:
            out.write_bytes(b"half")
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def write(self, block: np.ndarray) -> None:
            raise sf.LibsndfileError(2)

    def disk_usage(folder: Path) -> SimpleNamespace:  # (a full drive is full while the half-written file is on it, and has room again when that is taken off)
        free = 0 if full and out.exists() else 50 * 1024**3
        return SimpleNamespace(total=10**12, used=10**12 - free, free=free)

    monkeypatch.setattr(engine, "_open_writer", lambda *args, **kwargs: Refusing())
    monkeypatch.setattr(engine.shutil, "disk_usage", disk_usage)
    with pytest.raises(ConvertError, match=said):
        convert(src, out, Spec("mp3"))
    assert not out.exists()


def test_a_song_with_six_channels_is_refused_in_words(tmp_path: Path) -> None:
    src = audio.write_song(tmp_path / "a.flac", np.zeros((44100, 6), dtype=np.int16) + 100, 44100, "PCM_16")
    with pytest.raises(ConvertError, match="^This song has 6 channels; Convert handles mono and stereo.$"):
        convert(src, tmp_path / "a.mp3", Spec("mp3"))
    assert not (tmp_path / "a.mp3").exists()


def test_a_file_that_turns_out_to_hold_no_sound_leaves_nothing(tmp_path: Path) -> None:
    src = audio.write_song(tmp_path / "a.flac", audio.tones(44100, 2), 44100, "PCM_16")
    info = inspect_source(src)
    src.write_bytes(b"fLaC" + b"\0" * 20)  # (it changed between the look and the conversion)
    out = tmp_path / "a.mp3"
    with pytest.raises(ConvertError, match=r"^The song could not be converted \(|^There is no sound"):
        engine.convert_file(src, out, info, Spec("mp3"))
    assert not out.exists()
    audio.write_song(src, np.zeros((0, 2), dtype=np.int16), 44100, "PCM_16", "WAV")  # (or it is a file that is whole, and has no sound in it)
    with pytest.raises(ConvertError, match="^There is no sound in this file.$"):
        engine.convert_file(src, out, info, Spec("mp3"))
    assert not out.exists()


@pytest.mark.parametrize("spec,said", [
    (Spec("mp3", "999"), "quality for mp3 must be one of"), (Spec("mp3", max_rate=12345), "the highest rate must be one of"), (Spec("flac", depth="32"), "depth must be one of"),
    (Spec("mp3", existing="overwrite"), "existing songs must be one of"), (Spec("nope"), "format must be one of"),
])
def test_a_choice_that_is_not_offered_is_refused_before_anything_is_written(tmp_path: Path, spec: Spec, said: str) -> None:
    src = audio.write_song(tmp_path / "a.flac", audio.tones(44100, 2), 44100, "PCM_16")
    out = tmp_path / "a.out.part"
    with pytest.raises(ValueError, match=said):
        engine.convert_file(src, out, inspect_source(src), spec)
    assert not out.exists()


def test_a_song_is_never_converted_or_copied_onto_itself(tmp_path: Path) -> None:
    src = audio.write_song(tmp_path / "a.flac", audio.tones(44100, 2), 44100, "PCM_16")
    before = src.read_bytes()
    with pytest.raises(ConvertError, match="^A song cannot be converted onto itself.$"):
        convert(src, src, Spec("flac", existing="convert"))
    with pytest.raises(ConvertError, match="^A song cannot be converted onto itself.$"):
        engine.copy_file(src, tmp_path / "." / "a.flac")  # (the same file by another way of writing its name)
    assert src.read_bytes() == before


# ---------------------------------------------------------------------------------------- copying


def test_a_copy_is_the_same_bytes_and_reports_its_progress(tmp_path: Path) -> None:
    src = tmp_path / "a.mp3"
    src.write_bytes(bytes(range(256)) * 20_000)
    seen: list[tuple[int, int]] = []
    size = engine.copy_file(src, tmp_path / "copy.part", on_progress=lambda d, t: seen.append((d, t)))
    assert size == src.stat().st_size and (tmp_path / "copy.part").read_bytes() == src.read_bytes()
    assert seen[-1] == (size, size) and [d for d, _ in seen] == sorted(d for d, _ in seen) and len(seen) >= 4


def test_a_stopped_copy_and_a_copy_of_nothing_leave_no_file_behind(tmp_path: Path) -> None:
    src = tmp_path / "a.mp3"
    src.write_bytes(b"x" * (engine.COPY_CHUNK * 3))
    calls = 0

    def check() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise Stop

    with pytest.raises(Stop):
        engine.copy_file(src, tmp_path / "copy.part", check=check)
    assert not (tmp_path / "copy.part").exists()
    with pytest.raises(ConvertError, match=r"^The song could not be copied \(No such file or directory\)\.$"):
        engine.copy_file(tmp_path / "Outer Space" / "gone.mp3", tmp_path / "copy2.part")  # (a folder with "space" in its name is not a full drive)
    assert not (tmp_path / "copy2.part").exists()


@pytest.mark.parametrize("number,reason,said", [
    (errno.ENOSPC, "No space left on device", "^There is not enough room on the drive for the converted songs.$"),
    (errno.EIO, "Input/output error", r"^The song could not be copied \(Input/output error\).$"),
])
def test_a_copy_that_fails_after_it_was_started_is_said_in_words_and_leaves_no_file_behind(tmp_path: Path, number: int, reason: str, said: str) -> None:
    src = tmp_path / "a.mp3"
    src.write_bytes(b"x" * (engine.COPY_CHUNK * 3))
    target = tmp_path / "copy.part"

    def fails_after_the_first_block(done: int, total: int) -> None:
        assert target.exists() and done == engine.COPY_CHUNK  # (what is half written is there to be left behind)
        raise OSError(number, reason)

    with pytest.raises(ConvertError, match=said):
        engine.copy_file(src, target, on_progress=fails_after_the_first_block)
    assert not target.exists()
