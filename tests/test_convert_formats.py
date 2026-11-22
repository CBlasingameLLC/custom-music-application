"""What Convert writes, the sample rates and depths each format can hold, and how big the files will be."""

from __future__ import annotations

import pytest

from musictoolkit.convert import formats


def test_every_format_has_a_default_quality_that_it_lists_and_a_unique_extension() -> None:
    assert list(formats.FORMATS) == ["mp3", "ogg", "opus", "flac", "wav"]
    assert len({f.extension for f in formats.FORMATS.values()}) == len(formats.FORMATS)
    for fmt in formats.FORMATS.values():
        assert fmt.default in [q.id for q in fmt.qualities]
        assert formats.check_choice(fmt.id, None).id == fmt.default
        assert (fmt.lossy is False) == (fmt.id in ("flac", "wav"))
        assert all((q.kbps == 0) == (not fmt.lossy) for q in fmt.qualities)


def test_the_qualities_run_from_best_to_smallest() -> None:
    for fmt in ("mp3", "ogg", "opus"):
        kbps = [q.kbps for q in formats.FORMATS[fmt].qualities]
        assert kbps == sorted(kbps, reverse=True) and len(set(kbps)) == len(kbps)
        levels = [q.level for q in formats.FORMATS[fmt].qualities]
        assert levels == sorted(levels)  # (libsndfile: the higher the level, the smaller the file)


@pytest.mark.parametrize("fmt,quality", [("mp3", "100"), ("flac", "320"), ("opus", "256"), ("wav", "standard")])
def test_a_quality_the_format_does_not_have_is_refused_in_words(fmt: str, quality: str) -> None:
    with pytest.raises(ValueError, match=rf"quality for {fmt} must be one of"):
        formats.check_choice(fmt, quality)


def test_a_format_that_is_not_offered_is_refused_in_words() -> None:
    with pytest.raises(ValueError, match="format must be one of: mp3, ogg, opus, flac, wav"):
        formats.check_choice("aac", None)


@pytest.mark.parametrize("fmt,rate,cap,expected", [
    # the song's own rate stays unless a format cannot hold it or the person capped it
    ("flac", 44100, 0, 44100), ("flac", 96000, 0, 96000), ("wav", 22050, 0, 22050), ("ogg", 192000, 0, 192000),
    # MP3 has only 32, 44.1 and 48 kHz
    ("mp3", 44100, 0, 44100), ("mp3", 48000, 0, 48000), ("mp3", 32000, 0, 32000),
    ("mp3", 96000, 0, 48000), ("mp3", 192000, 0, 48000), ("mp3", 88200, 0, 44100), ("mp3", 176400, 0, 44100),
    ("mp3", 22050, 0, 44100), ("mp3", 11025, 0, 44100), ("mp3", 24000, 0, 48000), ("mp3", 16000, 0, 32000), ("mp3", 8000, 0, 32000), ("mp3", 12000, 0, 48000),
    # Opus has 8, 12, 16, 24 and 48 kHz
    ("opus", 48000, 0, 48000), ("opus", 24000, 0, 24000), ("opus", 8000, 0, 8000), ("opus", 44100, 0, 48000), ("opus", 96000, 0, 48000), ("opus", 22050, 0, 48000),
    # a cap: a song of the 44.1 kHz family stays in it under a 48 kHz cap
    ("flac", 96000, 48000, 48000), ("flac", 192000, 48000, 48000), ("flac", 88200, 48000, 44100), ("flac", 176400, 48000, 44100),
    ("flac", 96000, 44100, 44100), ("flac", 48000, 44100, 44100), ("flac", 44100, 44100, 44100), ("flac", 22050, 44100, 22050), ("wav", 64000, 48000, 48000),
    ("mp3", 96000, 44100, 44100), ("opus", 96000, 44100, 48000),
])
def test_the_rate_a_converted_song_has(fmt: str, rate: int, cap: int, expected: int) -> None:
    assert formats.target_rate(fmt, rate, cap) == expected


def test_a_cap_that_is_not_one_of_the_choices_is_refused() -> None:
    with pytest.raises(ValueError, match="the highest rate must be one of"):
        formats.target_rate("flac", 96000, 96000)


@pytest.mark.parametrize("fmt,depth,source,expected", [
    ("flac", "16", 16, 16), ("flac", "16", 24, 16), ("flac", "keep", 24, 24), ("flac", "keep", 16, 16), ("flac", "keep", 32, 24), ("flac", "keep", 8, 16),
    ("wav", "16", 24, 16), ("wav", "keep", 24, 24), ("wav", "keep", 32, 24),
    ("mp3", "keep", 24, 0), ("ogg", "16", 16, 0), ("opus", "keep", 24, 0),
])
def test_the_depth_a_converted_song_has(fmt: str, depth: str, source: int, expected: int) -> None:
    assert formats.target_bits(fmt, depth, source) == expected


def test_a_depth_that_is_not_one_of_the_choices_is_refused() -> None:
    with pytest.raises(ValueError, match="depth must be one of: 16, keep"):
        formats.target_bits("flac", "32", 16)


def test_how_big_the_files_will_be() -> None:
    assert formats.estimate_bytes("wav", None, 10, 2, 44100, 16) == 10 * 44100 * 4 + 44
    assert formats.estimate_bytes("wav", None, 10, 1, 48000, 24) == 10 * 48000 * 3 + 44
    assert formats.estimate_bytes("mp3", "256", 10, 2, 44100, 0) == 320_000  # (constant bitrate: the same for mono)
    assert formats.estimate_bytes("mp3", "256", 10, 1, 44100, 0) == 320_000
    assert formats.estimate_bytes("opus", "128", 10, 2, 48000, 0) == 160_000
    assert formats.estimate_bytes("opus", "128", 10, 1, 48000, 0) == 80_000  # (per channel)
    assert formats.estimate_bytes("ogg", "160", 10, 2, 44100, 0) == 200_000
    assert formats.estimate_bytes("flac", None, 10, 2, 44100, 16) == int(10 * 44100 * 4 * 0.62)
    assert formats.estimate_bytes("flac", None, -5, 2, 44100, 16) == 0  # (a length that is not known is nothing)
