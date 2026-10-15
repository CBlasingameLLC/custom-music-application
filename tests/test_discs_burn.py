"""Burning music: which songs can go on an audio CD, how they are turned into the plain audio a CD holds, and whether they fit.

The audio is made with tiny real files in tmp_path; what comes out is compared sample by sample with what went in."""

from __future__ import annotations

import errno
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from musictoolkit.discs import burn
from musictoolkit.discs.burn import (
    FRAMES_PER_SECTOR, GAP_SECTORS, MIN_SECTORS, RESERVE_SECTORS, SECTOR_BYTES, BurnError, SongCheck, SongInput, decode_to_pcm, inspect_song,
    plan_audio,
)


def noise(frames: int, channels: int = 2, seed: int = 1) -> np.ndarray:
    """Random 16-bit audio: any change to a sample shows."""
    return np.random.default_rng(seed).integers(-32768, 32768, (frames, channels), dtype=np.int16)


def tone(frames: int, rate: int, hertz: float = 1000.0, level: float = 0.5, channels: int = 2) -> np.ndarray:
    t = np.arange(frames) / rate
    wave = (level * np.sin(2 * np.pi * hertz * t)).astype(np.float64)
    return np.stack([wave] * channels, axis=1)


def song(tmp_path: Path, name: str, data: np.ndarray, rate: int = 44100, **kwargs) -> Path:
    path = tmp_path / name
    sf.write(str(path), data, rate, **kwargs)
    return path


def pcm_of(path: Path) -> np.ndarray:
    return np.frombuffer(path.read_bytes(), dtype="<i2").reshape(-1, 2)


def decoded(tmp_path: Path, source: Path, **kwargs) -> tuple[np.ndarray, int]:
    target = tmp_path / f"{source.stem}.pcm"
    sectors = decode_to_pcm(source, target, **kwargs)
    return pcm_of(target), sectors


FIVE_SECONDS = 5 * 44100


# ---------------------------------------------------------------------------------------- can this song go on a music CD?


@pytest.mark.parametrize("name,extra", [("a.wav", {}), ("a.flac", {})])
def test_cd_quality_songs_go_on_as_they_are(tmp_path: Path, name: str, extra: dict) -> None:
    check = inspect_song(song(tmp_path, name, noise(FIVE_SECONDS), subtype="PCM_16"))
    assert (check.verdict, check.notes, check.exact) == ("ok", [], True)
    assert check.sectors == -(-FIVE_SECONDS // FRAMES_PER_SECTOR) and check.seconds == pytest.approx(5.0)


def test_a_song_at_another_rate_is_converted_and_says_so(tmp_path: Path) -> None:
    check = inspect_song(song(tmp_path, "a.wav", noise(48000 * 6), rate=48000))
    assert check.verdict == "converted" and not check.exact
    assert check.notes == ["Converted from 48 kHz to 44.1 kHz."]
    assert check.sectors == -(-(44100 * 6) // FRAMES_PER_SECTOR)  # the length it will have on the disc, not in the file


@pytest.mark.parametrize(
    "kwargs,channels,said",
    [
        ({"subtype": "PCM_24"}, 2, "24-bit sound is reduced to 16 bits."),
        ({"subtype": "FLOAT"}, 2, "Floating-point sound is reduced to 16 bits."),
        ({"subtype": "PCM_16"}, 1, "Mono: both speakers get the same sound."),
    ],
)
def test_other_depths_and_mono_are_converted_and_say_so(tmp_path: Path, kwargs: dict, channels: int, said: str) -> None:
    data = tone(FIVE_SECONDS, 44100, channels=channels)
    check = inspect_song(song(tmp_path, "a.wav", data, **kwargs))
    assert check.verdict == "converted" and said in check.notes


@pytest.mark.parametrize(
    "name,kwargs,rate,verdict",
    [
        ("a.mp3", {"format": "MP3", "subtype": "MPEG_LAYER_III"}, 44100, "ok"),
        ("a.ogg", {"format": "OGG", "subtype": "VORBIS"}, 44100, "ok"),
        ("a.opus", {"format": "OGG", "subtype": "OPUS"}, 48000, "converted"),  # (Opus is always decoded at 48 kHz)
    ],
)
def test_the_compressed_formats_that_were_asked_for_are_read(tmp_path: Path, name: str, kwargs: dict, rate: int, verdict: str) -> None:
    check = inspect_song(song(tmp_path, name, tone(rate * 5, rate).astype(np.float32), rate=rate, **kwargs))
    assert check.verdict == verdict, check.notes
    assert check.seconds == pytest.approx(5.0, abs=0.2) and check.sectors > 5 * 75 - 20


@pytest.mark.parametrize("channels", [3, 6])
def test_more_than_two_channels_is_refused_in_words(tmp_path: Path, channels: int) -> None:
    check = inspect_song(song(tmp_path, "a.wav", noise(FIVE_SECONDS, channels=channels), subtype="PCM_16"))
    assert (check.verdict, check.sectors) == ("cannot", 0)
    assert check.notes == [f"This song has {channels} channels; a music CD holds two (stereo)."]


@pytest.mark.parametrize("extension,named", [("m4a", "M4A"), ("wma", "WMA"), ("aac", "AAC"), ("ape", "APE")])
def test_formats_that_are_not_supported_yet_are_named_not_silently_skipped(tmp_path: Path, extension: str, named: str) -> None:
    path = tmp_path / f"a.{extension}"
    path.write_bytes(b"not audio libsndfile knows" * 100)
    check = inspect_song(path)
    assert check.verdict == "cannot" and check.notes == [f"{named} songs cannot be burned to a music CD yet. MP3, FLAC, WAV, Ogg and Opus can."]


def test_a_file_that_is_missing_unreadable_or_silent_is_refused_in_words(tmp_path: Path) -> None:
    missing = inspect_song(tmp_path / "gone.flac")
    assert (missing.verdict, missing.notes) == ("cannot", ["The file is not there any more."])
    broken = tmp_path / "broken.mp3"
    broken.write_bytes(b"\0" * 3000)
    unreadable = inspect_song(broken)
    assert unreadable.verdict == "cannot" and unreadable.notes[0].startswith("The song cannot be read (")
    empty = inspect_song(song(tmp_path, "empty.wav", np.zeros((0, 2), dtype=np.int16), subtype="PCM_16"))
    assert (empty.verdict, empty.notes) == ("cannot", ["There is no sound in this file."])


def test_a_song_under_four_seconds_takes_four_and_says_so(tmp_path: Path) -> None:
    check = inspect_song(song(tmp_path, "a.wav", noise(2 * 44100), subtype="PCM_16"))
    assert check.verdict == "ok" and check.sectors == MIN_SECTORS == 300
    assert check.notes == ["Shorter than four seconds: silence is added to make four, as a music CD needs."]


def test_exactly_four_seconds_needs_no_padding_and_a_frame_less_does(tmp_path: Path) -> None:
    four = inspect_song(song(tmp_path, "four.wav", noise(4 * 44100), subtype="PCM_16"))
    assert (four.sectors, four.notes) == (300, [])
    less = inspect_song(song(tmp_path, "less.wav", noise(4 * 44100 - 1), subtype="PCM_16"))
    assert less.sectors == 300 and len(less.notes) == 1
    over = inspect_song(song(tmp_path, "over.wav", noise(4 * 44100 + 1), subtype="PCM_16"))
    assert (over.sectors, over.notes) == (301, [])


# ---------------------------------------------------------------------------------------- turning a song into CD audio


@pytest.mark.parametrize("name", ["a.wav", "a.flac"])
def test_16_bit_44_1_khz_audio_comes_out_bit_for_bit(tmp_path: Path, name: str) -> None:
    original = noise(FIVE_SECONDS + 123)
    out, sectors = decoded(tmp_path, song(tmp_path, name, original, subtype="PCM_16"))
    assert sectors == -(-len(original) // FRAMES_PER_SECTOR) and len(out) == sectors * FRAMES_PER_SECTOR
    assert np.array_equal(out[: len(original)], original)
    assert not out[len(original):].any()  # what pads the last sector is silence


def test_mono_is_doubled_without_changing_a_sample(tmp_path: Path) -> None:
    original = noise(FIVE_SECONDS, channels=1)
    out, _ = decoded(tmp_path, song(tmp_path, "m.wav", original, subtype="PCM_16"))
    assert np.array_equal(out[: len(original), 0], original[:, 0]) and np.array_equal(out[: len(original), 1], original[:, 0])


def test_a_depth_above_16_bits_is_rounded_to_the_nearest_value_give_or_take_the_dither(tmp_path: Path) -> None:
    wave = tone(FIVE_SECONDS, 44100, level=0.7)
    out, _ = decoded(tmp_path, song(tmp_path, "d.wav", wave, subtype="PCM_24"))
    error = out[: len(wave)].astype(np.float64) - wave * 32768.0
    assert np.abs(error).max() <= 1.51  # less than one bit of dither, plus half a bit of rounding
    assert abs(error.mean()) < 0.1  # no offset, no drift
    assert error.std() == pytest.approx(0.5, abs=0.03)  # triangular dither and rounding: the noise has the size theory gives


def test_dither_keeps_a_sound_quieter_than_one_bit_alive(tmp_path: Path) -> None:
    """Without dither a signal smaller than half a bit rounds to nothing at all; with it, the average of many cycles still follows the sound."""
    cycles = 500
    wave = tone(441 * cycles, 44100, hertz=1000.0, level=0.3 / 32768.0)  # ten cycles fit 441 frames exactly
    out, _ = decoded(tmp_path, song(tmp_path, "quiet.wav", wave, subtype="DOUBLE"))
    folded = out[: 441 * cycles, 0].reshape(cycles, 441).mean(axis=0)
    shape = wave[:441, 0] * 32768.0
    assert out[: 441 * cycles].any()
    assert np.corrcoef(folded, shape)[0, 1] > 0.9


def test_the_whole_range_a_cd_can_hold_passes_through_the_converter(tmp_path: Path) -> None:
    rate = 48000
    out, _ = decoded(tmp_path, song(tmp_path, "edge.wav", tone(rate * 6, rate, 20500.0, 0.5), rate=rate, subtype="PCM_16"))
    body = out[44100:-44100, 0].astype(np.float64) / 32768.0
    assert np.abs(body).max() == pytest.approx(0.5, abs=0.015)  # (a converter that gives up at 90% of the band would lose most of this)


def test_what_cannot_be_heard_is_filtered_out_not_folded_back_into_the_music(tmp_path: Path) -> None:
    rate = 48000
    out, _ = decoded(tmp_path, song(tmp_path, "high.wav", tone(rate * 6, rate, 23000.0, 0.5), rate=rate, subtype="PCM_16"))
    body = out[44100:-44100, 0].astype(np.float64) / 32768.0  # 23 kHz is above half of 44.1 kHz: a bad converter would play it back as 21.1 kHz
    assert np.abs(body).max() < 0.003


def test_floating_point_audio_louder_than_full_scale_is_clipped_not_wrapped(tmp_path: Path) -> None:
    wave = tone(FIVE_SECONDS, 44100, level=1.5, channels=2).astype(np.float32)
    out, _ = decoded(tmp_path, song(tmp_path, "loud.wav", wave, subtype="FLOAT"))
    assert out.min() == -32768 and out.max() == 32767
    assert np.abs(np.diff(out[:, 0].astype(np.int64))).max() < 20000  # a wrapped sample would jump from the top to the bottom


def test_another_rate_is_converted_and_keeps_pitch_length_and_level(tmp_path: Path) -> None:
    rate = 48000
    out, sectors = decoded(tmp_path, song(tmp_path, "r.wav", tone(rate * 6, rate, 1000.0, 0.5), rate=rate, subtype="PCM_16"))
    frames = 44100 * 6
    assert sectors == -(-frames // FRAMES_PER_SECTOR)
    body = out[:frames, 0].astype(np.float64) / 32768.0
    middle = body[44100:-44100]  # (leave out the ends, where a converter's filter is still settling)
    spectrum = np.abs(np.fft.rfft(middle * np.hanning(len(middle))))
    assert np.fft.rfftfreq(len(middle), 1 / 44100)[spectrum.argmax()] == pytest.approx(1000.0, abs=2.0)
    assert np.abs(middle).max() == pytest.approx(0.5, abs=0.01)


def test_how_the_song_is_cut_into_pieces_does_not_matter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exact = song(tmp_path, "e.flac", noise(FIVE_SECONDS), subtype="PCM_16")
    resampled = song(tmp_path, "s.wav", tone(48000 * 5, 48000), rate=48000, subtype="PCM_24")
    whole = {p.stem: decoded(tmp_path, p)[0] for p in (exact, resampled)}
    monkeypatch.setattr(burn, "CHUNK_FRAMES", 4096)
    pieces = {p.stem: decoded(tmp_path, p)[0] for p in (exact, resampled)}
    assert np.array_equal(whole["e"], pieces["e"])
    assert whole["s"].shape == pieces["s"].shape
    assert np.abs(whole["s"].astype(np.int32) - pieces["s"]).max() <= 3  # (only the dither differs)


@pytest.mark.parametrize("rate", [44100, 48000])
def test_a_song_that_ends_exactly_on_a_piece_boundary_is_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rate: int) -> None:
    monkeypatch.setattr(burn, "CHUNK_FRAMES", 8192)
    frames = 8192 * 24  # (a little over four seconds, so the four-second minimum does not hide the length)
    original = noise(frames)
    out, sectors = decoded(tmp_path, song(tmp_path, "b.wav", original, rate=rate, subtype="PCM_16"))
    expected = frames * 44100 / rate
    assert sectors == -(-round(expected) // FRAMES_PER_SECTOR) and len(out) == sectors * FRAMES_PER_SECTOR
    if rate == 44100:
        assert np.array_equal(out[:frames], original)
    else:
        assert np.abs(out[: round(expected) - 2000]).max() > 1000  # and it is the song, not silence


def test_the_same_song_is_always_decoded_to_the_same_bytes(tmp_path: Path) -> None:
    path = song(tmp_path, "same.wav", tone(48000 * 5, 48000, level=0.01), rate=48000, subtype="PCM_24")
    first, second = tmp_path / "1.pcm", tmp_path / "2.pcm"
    decode_to_pcm(path, first)
    decode_to_pcm(path, second)
    assert first.read_bytes() == second.read_bytes()


def test_a_short_song_is_padded_to_four_seconds(tmp_path: Path) -> None:
    original = noise(44100)
    out, sectors = decoded(tmp_path, song(tmp_path, "short.wav", original, subtype="PCM_16"))
    assert sectors == MIN_SECTORS and len(out) * 4 == MIN_SECTORS * SECTOR_BYTES
    assert np.array_equal(out[:44100], original) and not out[44100:].any()


def test_progress_counts_frames_of_the_song_up_to_its_length(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(burn, "CHUNK_FRAMES", 10000)
    seen: list[tuple[int, int]] = []
    decoded(tmp_path, song(tmp_path, "p.wav", noise(FIVE_SECONDS), subtype="PCM_16"), on_progress=lambda done, total: seen.append((done, total)))
    assert seen[-1] == (FIVE_SECONDS, FIVE_SECONDS) and [d for d, _ in seen] == sorted(d for d, _ in seen) and len(seen) > 5


def test_a_stop_leaves_no_file_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(burn, "CHUNK_FRAMES", 10000)
    calls = []

    class Stop(Exception):
        pass

    def check() -> None:
        calls.append(1)
        if len(calls) > 3:
            raise Stop

    target = tmp_path / "stopped.pcm"
    with pytest.raises(Stop):
        decode_to_pcm(song(tmp_path, "s.wav", noise(FIVE_SECONDS), subtype="PCM_16"), target, check=check)
    assert not target.exists()


def test_a_song_that_cannot_be_read_is_refused_with_its_name_and_leaves_nothing(tmp_path: Path) -> None:
    broken, wma = tmp_path / "broken.mp3", tmp_path / "w.wma"
    broken.write_bytes(b"\0" * 3000)
    wma.write_bytes(b"x" * 100)
    with pytest.raises(BurnError, match=r"^broken\.mp3: The song cannot be read"):
        decode_to_pcm(broken, tmp_path / "broken.pcm")
    with pytest.raises(BurnError, match=r"^w\.wma: WMA songs cannot be burned"):
        decode_to_pcm(wma, tmp_path / "w.pcm")
    assert not (tmp_path / "broken.pcm").exists() and not (tmp_path / "w.pcm").exists()


def test_running_out_of_room_for_the_temporary_copy_is_said_in_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Full:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def write(self, _data):
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(burn, "open", lambda *_a, **_k: Full(), raising=False)
    with pytest.raises(BurnError, match="not enough room on the drive for the temporary copy"):
        decode_to_pcm(song(tmp_path, "f.wav", noise(FIVE_SECONDS), subtype="PCM_16"), tmp_path / "f.pcm")


def test_a_file_that_turns_out_to_hold_no_sound_is_refused_and_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = song(tmp_path, "liar.wav", noise(FIVE_SECONDS), subtype="PCM_16")  # (its header promises sound; the decoder finds none)
    monkeypatch.setattr(sf.SoundFile, "read", lambda self, *a, **k: np.zeros((0, self.channels), dtype=np.int16))
    target = tmp_path / "liar.pcm"
    with pytest.raises(BurnError, match=r"^liar\.wav: There is no sound in this file\.$"):
        decode_to_pcm(path, target)
    assert not target.exists()


def test_a_failure_half_way_is_said_in_words_and_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(burn, "CHUNK_FRAMES", 10000)
    real = sf.SoundFile.read
    calls = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) > 2:
            raise RuntimeError("Internal error: the file ended too early")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(sf.SoundFile, "read", flaky)
    target = tmp_path / "half.pcm"
    with pytest.raises(BurnError, match=r"^h\.wav: the song could not be converted \(Internal error: the file ended too early\)\.$"):
        decode_to_pcm(song(tmp_path, "h.wav", noise(FIVE_SECONDS), subtype="PCM_16"), target)
    assert not target.exists()


def test_decoded_compressed_songs_have_the_length_of_the_song(tmp_path: Path) -> None:
    wave = tone(44100 * 5, 44100).astype(np.float32)
    for name, kwargs in (("c.mp3", {"format": "MP3", "subtype": "MPEG_LAYER_III"}), ("c.ogg", {"format": "OGG", "subtype": "VORBIS"})):
        out, sectors = decoded(tmp_path, song(tmp_path, name, wave, **kwargs))
        assert abs(len(out) - 44100 * 5) < 2 * 1152 + FRAMES_PER_SECTOR  # (an MP3 decoder's start-up and a sector's padding)
        assert np.abs(out[8000:-8000, 0]).max() > 12000  # and it is the sound, not silence


# ---------------------------------------------------------------------------------------- will it fit?


def media(**changes) -> dict:
    return {
        "writer": True, "state": "blank", "type": "CD-R", "rewritable": False, "blank": True, "can_audio": True,
        "capacity_sectors": 359846, "free_sectors": 359846, **changes,
    }


def check_of(sectors: int, verdict: str = "ok") -> SongCheck:
    return SongCheck("x.flac", verdict, [] if verdict != "cannot" else ["No."], seconds=sectors / 75, sectors=0 if verdict == "cannot" else sectors)


def plan_of(sizes: list[int], free: int | None = 359846, tmp_path: Path | None = None, **kwargs):
    checks = {f"{i}.flac": check_of(n) if n >= 0 else check_of(0, "cannot") for i, n in enumerate(sizes)}  # (a negative size stands for a song that cannot be burned)
    songs = [SongInput(path, title=f"Song {i}", track_id=i) for i, path in enumerate(checks)]
    return plan_audio(songs, media(free_sectors=free) if free is not None else None, tmp_path or Path("."), inspector=lambda path: checks[path], **kwargs)


def test_the_plan_counts_the_gaps_between_songs(tmp_path: Path) -> None:
    plan = plan_of([3000, 4500, 300], tmp_path=tmp_path)
    assert plan.song_sectors == 7800 and plan.disc_sectors == 7800 + 2 * GAP_SECTORS == 8100
    assert plan.problems == [] and plan.to_dict()["ok"] is True


def test_one_song_has_no_gap(tmp_path: Path) -> None:
    assert plan_of([3000], tmp_path=tmp_path).disc_sectors == 3000


def test_a_song_that_cannot_be_burned_is_listed_but_not_counted(tmp_path: Path) -> None:
    plan = plan_of([3000, -1, 4000], tmp_path=tmp_path)
    assert [s.position for s in plan.songs] == [1, 2, 3] and [s.burnable for s in plan.songs] == [True, False, True]
    assert plan.disc_sectors == 7000 + GAP_SECTORS  # two songs, one gap
    shown = plan.to_dict()
    assert shown["counts"] == {"ok": 2, "converted": 0, "cannot": 1} and shown["songs"][1]["verdict"] == "cannot" and shown["songs"][1]["notes"] == ["No."]
    assert (shown["disc_sectors"], shown["disc_seconds"], shown["free_sectors"], shown["free_seconds"]) == (7150, 95.3, 359846, 4797.9)
    assert shown["scratch_bytes"] == 7000 * SECTOR_BYTES and shown["ok"] is True and shown["problems"] == []
    assert [(s["position"], s["track_id"], s["title"], s["seconds"], s["sectors"]) for s in shown["songs"]] == [
        (1, 0, "Song 0", 40.0, 3000), (2, 1, "Song 1", 0.0, 0), (3, 2, "Song 2", 53.3, 4000),
    ]


def test_the_songs_fit_up_to_the_reserve_and_not_a_sector_beyond(tmp_path: Path) -> None:
    sizes = [30000, 30000]
    needs = 60000 + GAP_SECTORS + RESERVE_SECTORS
    assert plan_of(sizes, free=needs, tmp_path=tmp_path).problems == []
    short = plan_of(sizes, free=needs - 1, tmp_path=tmp_path)
    assert short.problems == ["The songs take 13:22 and the disc has 13:26 free. Take out about 1 second of music (a few seconds are kept in reserve)."]
    assert short.to_dict()["ok"] is False and short.to_dict()["problems"] == short.problems
    assert burn.fit_problem(60150, 60450) is None and burn.fit_problem(60150, 60449) == short.problems[0]
    much = plan_of(sizes, free=needs - 75 * 185, tmp_path=tmp_path)  # three minutes and five seconds too much
    assert "Take out about 3 minutes 5 seconds of music" in much.problems[0]
    assert "Take out about 2 minutes of music" in plan_of(sizes, free=needs - 75 * 120, tmp_path=tmp_path).problems[0]
    assert "Take out about 1 minute 1 second of music" in plan_of(sizes, free=needs - 75 * 61, tmp_path=tmp_path).problems[0]
    assert "Take out about 3 minutes 6 seconds of music" in plan_of(sizes, free=needs - (75 * 185 + 1), tmp_path=tmp_path).problems[0]  # (a part of a second counts as a second)
    assert "Take out about 45 seconds of music" in plan_of(sizes, free=needs - 75 * 45, tmp_path=tmp_path).problems[0]


def test_ninety_nine_songs_are_the_most_a_cd_holds(tmp_path: Path) -> None:
    assert plan_of([300] * 99, tmp_path=tmp_path).problems == []
    many = plan_of([300] * 100, tmp_path=tmp_path)
    assert many.problems == ["A music CD holds at most 99 songs, and 100 were chosen. Take some out."]


def test_nothing_chosen_and_nothing_that_can_be_burned(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Choose some songs first"):
        plan_audio([], media(), tmp_path)
    plan = plan_of([-1, -1], tmp_path=tmp_path)
    assert plan.problems == ["None of these songs can be burned to a music CD."] and plan.disc_sectors == 0


@pytest.mark.parametrize(
    "disc,said",
    [
        (None, "Choose a drive that has a blank CD-R or CD-RW in it."),
        ({"writer": False}, "This drive cannot write discs."),
        ({"state": "none", "blank": False, "type": ""}, "There is no disc in the drive."),
        ({"state": "written", "blank": False}, "This CD-R is not blank, and a disc that has been written cannot be used again. Put in a blank CD-R."),
        ({"blank": False}, "This CD-R is not blank, and a disc that has been written cannot be used again. Put in a blank CD-R."),  # (when in doubt, it is not blank)
        (
            {"state": "written", "blank": False, "type": "CD-RW", "rewritable": True},
            "This CD-RW already has something on it. Erase it first (Erase, on this screen), or put in a blank disc.",
        ),
        ({"type": "DVD-R", "can_audio": False}, "A music CD can only be made on a blank CD-R or CD-RW, and this is a DVD-R."),
    ],
)
def test_a_disc_that_cannot_take_music_says_why(tmp_path: Path, disc: dict | None, said: str) -> None:
    songs = [SongInput("a.flac")]
    plan = plan_audio(songs, None if disc is None else media(**disc), tmp_path, inspector=lambda _p: check_of(3000))
    assert plan.problems == [said] and plan.free_sectors is None


def test_no_room_for_the_temporary_copy_is_a_problem_but_not_knowing_is_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    needed = 3000 * SECTOR_BYTES + burn.SCRATCH_MARGIN
    monkeypatch.setattr(burn, "_scratch_free", lambda _folder: needed - 1)
    assert plan_of([3000], tmp_path=tmp_path).problems == [
        "There is not enough room for the temporary copy of the music: about 56.7 MB are needed and 56.7 MB are free."
    ]
    monkeypatch.setattr(burn, "_scratch_free", lambda _folder: needed)
    assert plan_of([3000], tmp_path=tmp_path).problems == []
    monkeypatch.setattr(burn, "_scratch_free", lambda _folder: None)
    assert plan_of([3000], tmp_path=tmp_path).problems == []


def test_room_is_measured_where_the_folder_will_be_even_though_it_is_not_there_yet(tmp_path: Path) -> None:
    free = burn._scratch_free(tmp_path / "cache" / "discs" / "burn")
    assert isinstance(free, int) and free > 0


def test_a_real_folder_of_real_songs_is_planned_end_to_end(tmp_path: Path) -> None:
    songs = [
        SongInput(str(song(tmp_path, "one.flac", noise(44100 * 30), subtype="PCM_16")), "One", "A", 1),
        SongInput(str(song(tmp_path, "two.wav", tone(48000 * 20, 48000), rate=48000, subtype="PCM_24")), "Two", "A", 2),
        SongInput(str(tmp_path / "gone.mp3"), "Gone", "A", 3),
    ]
    plan = plan_audio(songs, media(), tmp_path / "cache")
    shown = plan.to_dict()
    assert [s["verdict"] for s in shown["songs"]] == ["ok", "converted", "cannot"]
    assert shown["disc_sectors"] == 30 * 75 + 20 * 75 + GAP_SECTORS  # two songs and the gap between them; the missing one takes nothing
    assert shown["ok"] is True and shown["free_sectors"] == 359846


# ---------------------------------------------------------------------------------------- tidying up


def test_forgetting_a_burns_scratch_removes_that_batch_only(tmp_path: Path) -> None:
    folder = tmp_path / "burn"
    folder.mkdir()
    for name in ("abc-01.pcm", "abc-02.pcm", "xyz-01.pcm"):
        (folder / name).write_bytes(b"1234")
    burn.forget_scratch(folder, "abc")
    assert sorted(p.name for p in folder.iterdir()) == ["xyz-01.pcm"]
    burn.forget_scratch(folder, "xyz")
    assert not folder.exists()  # (and the folder goes when it is empty)
    burn.forget_scratch(folder, "xyz")  # (nothing to do, nothing raised)
