"""Songs at a lower bitrate: when a song is converted, how ffmpeg is run, what is kept, who follows whom, and what happens when ffmpeg does not do what it
should. ffmpeg is stood in for by tests/fake_ffmpeg.py, so that every case, a crash and a hang included, can be made to happen; test_transcode_ffmpeg.py
runs the real one."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from musictoolkit import transcode
from musictoolkit.transcode import Job, Plan, Source, TranscodeError, Transcoder, decide, estimated_bytes, exact_length, tier
from tests.fake_ffmpeg import chunk

STUB = Path(__file__).with_name("fake_ffmpeg.py")


def stub() -> list[str]:
    return [sys.executable, str(STUB)]


def song(folder: Path, plan: str, name: str = "song.flac") -> Path:
    path = folder / name
    path.write_text(plan, encoding="utf-8")
    return path


def said(kbps: int, offset: str, count: int) -> bytes:
    """What the stand-in writes for `count` chunks."""
    return b"".join(chunk(str(kbps), offset, number) for number in range(count))


def runs(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def wait(job: Job, seconds: float = 10.0) -> Job:
    assert job.done.wait(seconds), "the job did not end"
    return job


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A transcoder that runs the stand-in, with a cache folder, and the log of what the stand-in was asked."""
    log = tmp_path / "ffmpeg.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    made = Transcoder(cache_dir=tmp_path / "cache", cache_mb=10, jobs=2, command=stub())
    yield SimpleNamespace(t=made, cache=tmp_path / "cache", log=log, tmp=tmp_path)
    made.close()


# -------------------------------------------------------------------------------------------------------------------------------- what is asked for

FLAC = Source("flac", 900_000)
LOUD_MP3 = Source("mp3", 320_000)
MP3_128 = Source("mp3", 128_000)


@pytest.mark.parametrize("source,max_kbps,format,expected", [
    (FLAC, 0, None, None),  # (nothing was asked for: the file)
    (FLAC, 128, None, 128),
    (FLAC, 999, None, None),  # (900 is within 10% of what is allowed)
    (FLAC, 320, None, 320),
    (FLAC, 500, None, 320),  # (the highest there is, below what was asked for)
    (FLAC, -5, None, None),
    (FLAC, 0, "raw", None),
    (FLAC, 128, "raw", None),  # ("raw" is no conversion at all, whatever the bitrate)
    (FLAC, 128, "RAW", None),
    (FLAC, 0, "mp3", 192),  # (an app that wants MP3 and was not told a bitrate)
    (FLAC, 96, "mp3", 96),
    (FLAC, 0, "opus", None),  # (a format that is not made is not asked for: the bitrate decides)
    (FLAC, 128, "opus", 128),
    (FLAC, 0, "", None),
    (LOUD_MP3, 128, None, 128),
    (LOUD_MP3, 320, None, None),
    (LOUD_MP3, 300, None, None),  # (320 is within 10% of 300... 330)
    (LOUD_MP3, 290, None, 256),  # (and over 10% of 290)
    (LOUD_MP3, 0, "mp3", None),  # (it is MP3 already)
    (LOUD_MP3, 128, "MP3", 128),
    (LOUD_MP3, 0, ".mp3", None),
    (MP3_128, 128, None, None),
    (MP3_128, 140, None, None),
    (MP3_128, 100, None, 96),
    (MP3_128, 96, None, 96),
    (Source("m4a", 96_000), 0, "mp3", 192),
    (Source("m4a", 96_000), 0, None, None),
    (Source("flac", None, 40_000_000, 240.0), 128, None, 128),  # (no bitrate stored: it is worked out from the size and the length)
    (Source("flac", None, 4_000_000, 240.0), 128, None, None),  # (133 kbps, within 10% of 128... 140)
    (Source(), 128, None, 128),  # (nothing is known of the file: it cannot be said that it is small enough)
    (Source(), 0, None, None),
    (Source(None), 0, "mp3", 192),  # (and not known to be MP3)
])
def test_a_song_is_converted_when_it_is_over_what_was_asked_for_or_of_the_other_format_and_to_the_bitrate_that_was_asked(source, max_kbps, format, expected) -> None:
    plan = decide(source, max_kbps, format)
    assert (plan.kbps if plan else None) == expected


@pytest.mark.parametrize("asked,tiered", [
    (0, 32), (1, 32), (31, 32), (32, 32), (47, 32), (48, 48), (63.9, 48), (64, 64), (95, 64), (96, 96), (127, 96), (128, 128), (159, 128), (160, 160),
    (191, 160), (192, 192), (255, 192), (256, 256), (319, 256), (320, 320), (321, 320), (10_000, 320),
])
def test_the_bitrate_is_the_highest_there_is_that_is_not_above_what_was_asked(asked: float, tiered: int) -> None:
    assert tier(asked) == tiered
    assert transcode.TIERS == (32, 48, 64, 96, 128, 160, 192, 256, 320) and transcode.DEFAULT_KBPS == 192 and transcode.FORMAT == "mp3"


def test_the_bitrate_of_a_file_is_its_tag_and_failing_that_its_size_over_its_length() -> None:
    assert Source(bitrate=192_000, size=1, duration=1.0).kbps() == 192
    assert Source(bitrate=0, size=3_000_000, duration=200.0).kbps() == 120
    assert Source(size=3_000_000, duration=200.0).kbps() == 120
    assert Source(size=3_000_000, duration=0).kbps() is None and Source(size=3_000_000).kbps() is None and Source(duration=200.0).kbps() is None
    assert Source().kbps() is None and Plan(128).kbps == 128


def test_the_size_of_a_constant_bitrate_stream_is_its_seconds_times_its_bytes_a_second() -> None:
    assert estimated_bytes(240, 128) == 3_840_000 and estimated_bytes(0.5, 128) == 8000 and estimated_bytes(0.0001, 128) == 1 and estimated_bytes(1, 320) == 40_000


@pytest.mark.parametrize("parts,size,expected", [
    ([b"abc", b"def"], 6, b"abcdef"),
    ([b"abc", b"def"], 4, b"abcd"),  # (cut in the middle of a chunk)
    ([b"abc", b"def"], 3, b"abc"),
    ([b"abc", b"def"], 0, b""),
    ([b"abc"], 5, b"abc\0\0"),  # (filled)
    ([], 3, b"\0\0\0"),
    ([b"", b"ab", b""], 2, b"ab"),
])
def test_a_stream_is_given_exactly_the_length_that_was_promised(parts: list[bytes], size: int, expected: bytes) -> None:
    assert b"".join(exact_length(iter(parts), size)) == expected


def test_a_big_fill_is_given_in_blocks_and_not_in_one_piece() -> None:
    blocks = list(exact_length(iter([]), transcode.READ_SIZE * 2 + 5))
    assert [len(block) for block in blocks] == [transcode.READ_SIZE, transcode.READ_SIZE, 5]


# ------------------------------------------------------------------------------------------------------------------------------------ is ffmpeg there


def test_it_says_when_ffmpeg_is_there_and_which(world) -> None:
    assert world.t.available() and world.t.status() == {"available": True, "ffmpeg": "stub-1.0", "reason": None}


def test_it_says_when_ffmpeg_is_not_there(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # (nothing on it)
    for named in ("", str(tmp_path / "no" / "ffmpeg"), "ffmpeg"):
        none = Transcoder(ffmpeg=named, cache_dir=tmp_path / "c")
        assert not none.available()
        assert none.status() == {"available": False, "ffmpeg": None, "reason": "ffmpeg was not found (on Linux: sudo apt install ffmpeg)"}
        with pytest.raises(TranscodeError, match=r"^ffmpeg was not found \(on Linux: sudo apt install ffmpeg\)$"):
            none.job("k.mp3", tmp_path / "song.flac", 128)
        with pytest.raises(TranscodeError, match="ffmpeg was not found"):
            list(none.direct(tmp_path / "song.flac", 128, 3.0))


def test_it_says_when_this_ffmpeg_cannot_write_mp3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_NO_LAME", "1")
    without = Transcoder(cache_dir=tmp_path / "c", command=stub())
    assert not without.available()
    assert without.status() == {"available": False, "ffmpeg": "stub-1.0", "reason": "this ffmpeg cannot write MP3 (it was made without libmp3lame)"}


def test_it_says_when_ffmpeg_cannot_be_run(tmp_path: Path) -> None:
    broken = Transcoder(cache_dir=tmp_path / "c", command=[str(tmp_path / "does-not-run")])
    status = broken.status()
    assert status["available"] is False and status["ffmpeg"] is None and status["reason"].startswith("ffmpeg could not be run (")


def test_an_explicit_path_is_used_and_the_one_on_the_path_when_there_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "bin" / "my-ffmpeg"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    assert Transcoder(ffmpeg=str(fake))._program() == [str(fake)]  # (it is a file, whether or not it can be run)
    on_path = tmp_path / "path"
    on_path.mkdir()
    named = on_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    named.write_text("", encoding="utf-8")
    named.chmod(0o755)
    monkeypatch.setenv("PATH", str(on_path))
    found = shutil.which("ffmpeg")
    assert found is not None and Path(found).parent == on_path
    assert Transcoder()._program() == [found] and Transcoder(ffmpeg="ffmpeg")._program() == [found]


def test_a_closed_transcoder_has_no_ffmpeg_for_anyone(world) -> None:
    assert world.t.available()
    world.t.close()
    assert not world.t.available() and world.t.status()["available"] is False
    with pytest.raises(TranscodeError):
        world.t.job("k.mp3", song(world.tmp, "ok 1 0"), 128)


# ------------------------------------------------------------------------------------------------------------------------------------ making a song


def test_a_song_is_made_kept_and_found_again(world) -> None:
    path = song(world.tmp, "ok 5 0")
    key = world.t.key(7, path, 128)
    assert world.t.cached(key) is None
    job = wait(world.t.job(key, path, 128))
    assert job.ok and job.error is None and job.size == 5000
    assert b"".join(world.t.follow(job)) == said(128, "0", 5)
    kept = world.t.cached(key)
    assert kept == world.cache / key and kept.read_bytes() == said(128, "0", 5)
    assert sorted(p.name for p in world.cache.iterdir()) == [key]  # (nothing half-made is left)
    assert len(runs(world.log)) == 1


def test_the_file_is_asked_for_the_way_it_should_be(world) -> None:
    path = song(world.tmp, "ok 1 0", "a song with spaces, and ünïcode.flac")
    wait(world.t.job(world.t.key(1, path, 96), path, 96))
    [asked] = runs(world.log)
    assert asked == f"-nostdin -hide_banner -loglevel error -i {path} -map 0:a:0 -vn -sn -dn -map_metadata -1 -c:a libmp3lame -b:a 96k -ar 44100 -ac 2 -write_xing 0 -id3v2_version 0 -f mp3 pipe:1"


def test_the_arguments_are_one_each_whatever_the_name_of_the_file_and_the_start_is_before_the_file(world) -> None:
    path = world.tmp / "-odd name; rm -rf.flac"
    argv = world.t._arguments(path, 128, 12.5)
    assert argv[:2] == stub() and argv[argv.index("-i") + 1] == str(path) and argv.count(str(path)) == 1
    assert argv[argv.index("-ss") + 1] == "12.500" and argv.index("-ss") < argv.index("-i")
    assert argv[-1] == "pipe:1" and argv[argv.index("-b:a") + 1] == "128k"
    assert "-ss" not in world.t._arguments(path, 128) and "-ss" not in world.t._arguments(path, 128, 0)


@pytest.mark.skipif(os.name != "posix" or shutil.which("nice") is None, reason="nice")
def test_a_real_ffmpeg_is_run_at_a_low_priority_and_the_stand_in_is_not(tmp_path: Path, world) -> None:
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    argv = Transcoder(ffmpeg=str(fake))._arguments(tmp_path / "s.flac", 128)
    assert argv[:4] == ["nice", "-n", "10", str(fake)]
    assert world.t._arguments(tmp_path / "s.flac", 128)[0] == sys.executable


def test_the_name_of_a_kept_song_changes_with_the_song_its_file_and_the_bitrate(world) -> None:
    path = song(world.tmp, "ok 1 0")
    first = world.t.key(7, path, 128)
    assert first == f"7-{path.stat().st_size}-{path.stat().st_mtime_ns}-128.mp3"
    assert world.t.key(8, path, 128) != first and world.t.key(7, path, 96) != first
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    assert world.t.key(7, path, 128) != first
    path.write_text("ok 1 0 and more", encoding="utf-8")
    assert world.t.key(7, path, 128).split("-")[1] == str(path.stat().st_size)


def test_a_second_request_for_a_song_that_is_being_made_follows_the_first_and_does_not_start_another_ffmpeg(world) -> None:
    path = song(world.tmp, "ok 10 50")
    key = world.t.key(1, path, 128)
    first = world.t.job(key, path, 128)
    second = world.t.job(key, path, 128)
    assert second is first
    assert b"".join(world.t.follow(first)) == b"".join(world.t.follow(second)) == said(128, "0", 10)
    wait(first)
    assert len(runs(world.log)) == 1
    again = world.t.job(key, path, 128)  # (it is over: a new request is a new job, which the caller does not make: the file is kept)
    assert again is not first
    wait(again)


def test_the_first_bytes_are_handed_out_before_the_song_is_whole(world) -> None:
    path = song(world.tmp, "ok 10 100")
    job = world.t.job(world.t.key(1, path, 128), path, 128)
    assert job.started.wait(10)
    follower = world.t.follow(job)
    first = next(follower)
    assert first.startswith(b"[128|0|0000]") and not job.done.is_set()  # (a second of making is still to come)
    assert first + b"".join(follower) == said(128, "0", 10)


def test_a_listener_can_come_in_part_of_the_way_and_have_the_rest(world) -> None:
    path = song(world.tmp, "ok 6 0")
    job = wait(world.t.job(world.t.key(1, path, 128), path, 128))
    whole = said(128, "0", 6)
    assert b"".join(world.t.follow(job, 2500)) == whole[2500:]
    assert b"".join(world.t.follow(job, 6000)) == b""
    assert b"".join(world.t.follow(job, 100_000)) == b""


def test_a_long_song_is_handed_out_in_pieces(world, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcode, "SEND_SIZE", 1500)
    path = song(world.tmp, "ok 5 0")
    job = wait(world.t.job(world.t.key(1, path, 128), path, 128))
    assert [len(piece) for piece in world.t.follow(job)] == [1500, 1500, 1500, 500]


def test_a_finished_song_is_found_before_a_new_one_is_made(world) -> None:
    path = song(world.tmp, "ok 3 0")
    key = world.t.key(1, path, 128)
    wait(world.t.job(key, path, 128))
    older = time.time() - 5000
    os.utime(world.cache / key, (older, older))
    assert world.t.cached(key) is not None
    assert (world.cache / key).stat().st_mtime > older + 4000  # (it was wanted just now)


# ------------------------------------------------------------------------------------------------------------------------------ when it goes wrong


def test_a_crash_leaves_nothing_kept_says_why_and_cuts_a_listener_who_was_already_hearing_it(world) -> None:
    path = song(world.tmp, "crash 3 0")
    key = world.t.key(1, path, 128)
    job = wait(world.t.job(key, path, 128))
    assert not job.ok and job.error == "Error: boom"
    assert world.t.cached(key) is None and list(world.cache.iterdir()) == []
    heard = []
    with pytest.raises(TranscodeError, match="Error: boom"):
        for piece in world.t.follow(job):
            heard.append(piece)
    assert b"".join(heard) == said(128, "0", 3)  # (what was made is handed out, and then the listener is told it stopped)


def test_a_song_that_makes_nothing_is_an_error_and_so_is_one_that_ends_badly_without_saying_why(world) -> None:
    path = song(world.tmp, "empty")
    job = wait(world.t.job(world.t.key(1, path, 128), path, 128))
    assert not job.ok and job.error == "ffmpeg ended with 0 and made nothing" and list(world.cache.iterdir()) == []
    with pytest.raises(TranscodeError, match="ffmpeg ended with 0 and made nothing"):
        list(world.t.follow(job))


def test_ffmpeg_that_makes_nothing_for_too_long_is_stopped(world, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcode, "STALL", 0.5)
    path = song(world.tmp, "hang")
    started = time.monotonic()
    job = wait(world.t.job(world.t.key(1, path, 128), path, 128), 20)
    assert time.monotonic() - started < 15
    assert not job.ok and job.error == "ffmpeg made nothing for 0.5 seconds" and list(world.cache.iterdir()) == []


def test_a_file_that_ffmpeg_cannot_open_is_an_error_with_what_it_said(world, tmp_path: Path) -> None:
    nothing = wait(world.t.job("1-1-1-128.mp3", tmp_path / "no-such-file.flac", 128))
    assert not nothing.ok and "no-such-file.flac" in nothing.error  # (the stand-in says what it could not open)
    assert list(world.cache.iterdir()) == []


def test_a_cache_folder_that_cannot_be_made_is_an_error_and_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log"))
    blocked = tmp_path / "file"
    blocked.write_text("x", encoding="utf-8")
    made = Transcoder(cache_dir=blocked / "inside", command=stub())
    try:
        job = wait(made.job("1-1-1-128.mp3", song(tmp_path, "ok 1 0"), 128))
        assert not job.ok and job.error
    finally:
        made.close()


def test_a_song_that_failed_is_left_alone_for_a_while_and_tried_again_after(world, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcode, "LEFT_ALONE", 0.4)
    path = song(world.tmp, "crash 2 0")
    key = world.t.key(1, path, 128)
    assert not world.t.failed(key)
    wait(world.t.job(key, path, 128))
    assert world.t.failed(key) and not world.t.failed(world.t.key(1, path, 96)) and not world.t.failed(world.t.key(2, path, 128))
    time.sleep(0.6)
    assert not world.t.failed(key)  # (and it is forgotten)
    assert key not in world.t._failed


def test_only_a_song_that_was_not_made_for_what_is_wrong_with_it_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log"))
    monkeypatch.setattr(transcode, "SLOT_WAIT", 0.2)
    one = Transcoder(cache_dir=tmp_path / "c", cache_mb=10, jobs=1, command=stub())
    try:
        good, busy = song(tmp_path, "ok 6 100", "a.flac"), song(tmp_path, "ok 2 0", "b.flac")
        first = one.job(one.key(1, good, 128), good, 128)
        assert first.started.wait(10)
        wait(one.job(one.key(2, busy, 128), busy, 128))  # (every place is taken: that says nothing of the song)
        assert not one.failed(one.key(2, busy, 128))
        assert wait(first).ok and not one.failed(one.key(1, good, 128))  # (and one that was made is not a failure)
        long = song(tmp_path, "ok 200 50", "c.flac")
        stopped = one.job(one.key(3, long, 128), long, 128)
        assert stopped.started.wait(10)
        one.close()
        wait(stopped)
        assert not one.failed(one.key(3, long, 128))  # (nor one that was stopped because the app was)
    finally:
        one.close()


def test_the_memory_of_failures_is_cleared_when_the_cache_is_pruned(world, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcode, "LEFT_ALONE", 0.2)
    world.t._failed = {"old": time.monotonic() - 5, "new": time.monotonic()}
    world.t.prune()
    assert list(world.t._failed) == ["new"]


# ------------------------------------------------------------------------------------------------------------------------------------ the places


def test_only_so_many_songs_are_made_at_once_and_one_that_waits_too_long_is_not_made(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log"))
    monkeypatch.setattr(transcode, "SLOT_WAIT", 0.2)
    one = Transcoder(cache_dir=tmp_path / "c", cache_mb=10, jobs=1, command=stub())
    try:
        slow, other = song(tmp_path, "ok 10 100", "a.flac"), song(tmp_path, "ok 2 0", "b.flac")
        first = one.job("1-1-1-128.mp3", slow, 128)
        assert first.started.wait(10)
        second = wait(one.job("2-1-1-128.mp3", other, 128))
        assert not second.ok and second.error == "every place for converting a song is taken" and list(one._dir.glob("2-*")) == []
        assert wait(first).ok  # (the one that was running was not hurt)
    finally:
        one.close()


def test_a_song_that_waits_a_little_for_a_place_is_made_when_one_is_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log"))
    one = Transcoder(cache_dir=tmp_path / "c", cache_mb=10, jobs=1, command=stub())
    try:
        first = one.job("1-1-1-128.mp3", song(tmp_path, "ok 3 50", "a.flac"), 128)
        second = one.job("2-1-1-128.mp3", song(tmp_path, "ok 2 0", "b.flac"), 128)
        assert wait(first).ok and wait(second).ok
        assert (one._dir / "2-1-1-128.mp3").read_bytes() == said(128, "0", 2)
    finally:
        one.close()


def test_a_song_for_one_listener_is_made_from_the_time_asked_and_not_kept(world) -> None:
    path = song(world.tmp, "ok 4 0")
    assert b"".join(world.t.direct(path, 64, 90.0)) == said(64, "90.000", 4)
    assert not world.cache.exists() or list(world.cache.iterdir()) == []
    assert len(runs(world.log)) == 1 and "-ss 90.000" in runs(world.log)[0]
    assert b"".join(world.t.direct(path, 64, 0.0)) == said(64, "0", 4)


def test_ffmpeg_is_stopped_and_the_place_given_back_when_the_listener_of_a_direct_stream_goes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log"))
    monkeypatch.setattr(transcode, "SLOT_WAIT", 1.0)
    one = Transcoder(cache_dir=tmp_path / "c", jobs=1, command=stub())
    try:
        path = song(tmp_path, "ok 1000 20")
        listening = one.direct(path, 128, 5.0)
        assert next(listening).startswith(b"[128|5.000|0000]")
        listening.close()
        assert b"".join(one.direct(song(tmp_path, "ok 2 0", "b.flac"), 128, 0.0)) == said(128, "0", 2)  # (the only place is free again)
    finally:
        one.close()


def test_a_direct_stream_waits_for_a_place_and_then_says_there_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log"))
    monkeypatch.setattr(transcode, "SLOT_WAIT", 0.2)
    one = Transcoder(cache_dir=tmp_path / "c", jobs=1, command=stub())
    try:
        holding = one.direct(song(tmp_path, "ok 1000 20"), 128, 1.0)
        next(holding)
        with pytest.raises(TranscodeError, match="^every place for converting a song is taken$"):
            list(one.direct(song(tmp_path, "ok 2 0", "b.flac"), 128, 1.0))
        holding.close()
    finally:
        one.close()


def test_stopping_the_app_stops_what_is_running_and_the_song_that_was_being_made_is_not_kept(world) -> None:
    path = song(world.tmp, "ok 200 50")
    job = world.t.job(world.t.key(1, path, 128), path, 128)
    assert job.started.wait(10)
    world.t.close()
    wait(job, 10)
    assert not job.ok and job.error == "it was stopped" and job.cancelled
    assert list(world.cache.iterdir()) == []


# ------------------------------------------------------------------------------------------------------------------------------------- the cache


def fill(folder: Path, name: str, size: int, age: float) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"x" * size)
    then = time.time() - age
    os.utime(path, (then, then))
    return path


def test_the_cache_is_kept_to_its_size_by_taking_away_the_least_recently_wanted_first(tmp_path: Path) -> None:
    folder = tmp_path / "c"
    files = [fill(folder, f"{n}-1-1-128.mp3", 400_000, age) for n, age in ((1, 400), (2, 300), (3, 200), (4, 100))]
    cache = Transcoder(cache_dir=folder, cache_mb=1, command=stub())  # (a megabyte: 1,048,576 bytes; four songs are 1,600,000)
    cache.prune()
    assert [p.exists() for p in files] == [False, False, True, True]


def test_a_song_that_was_wanted_just_now_is_the_last_to_go(tmp_path: Path) -> None:
    folder = tmp_path / "c"
    files = [fill(folder, f"{n}-1-1-128.mp3", 400_000, age) for n, age in ((1, 400), (2, 300), (3, 200), (4, 100))]
    cache = Transcoder(cache_dir=folder, cache_mb=1, command=stub())
    assert cache.cached("1-1-1-128.mp3") is not None
    cache.prune()
    assert [p.exists() for p in files] == [True, False, False, True]


def test_the_song_that_was_just_made_is_kept_even_when_it_alone_is_over_the_size(tmp_path: Path) -> None:
    folder = tmp_path / "c"
    old = fill(folder, "1-1-1-128.mp3", 100, 500)
    new = fill(folder, "2-1-1-128.mp3", 100, 1)
    Transcoder(cache_dir=folder, cache_mb=0, command=stub()).prune(protect=new)
    assert not old.exists() and new.exists()
    Transcoder(cache_dir=folder, cache_mb=0, command=stub()).prune()
    assert not new.exists()


def test_what_is_not_a_song_of_the_cache_is_left_alone_and_a_missing_folder_is_no_trouble(tmp_path: Path) -> None:
    folder = tmp_path / "c"
    other = fill(folder, "notes.txt", 5_000_000, 10_000)
    fill(folder, "5-1-1-128.mp3", 100, 10)
    cache = Transcoder(cache_dir=folder, cache_mb=0, command=stub())
    cache.prune()
    assert other.exists()
    Transcoder(cache_dir=tmp_path / "nowhere", command=stub()).prune()


def test_half_made_files_that_a_crash_left_are_cleared_after_an_hour_and_not_before_and_not_those_of_a_job_that_is_running(tmp_path: Path) -> None:
    folder = tmp_path / "c"
    old = fill(folder, "1-1-1-128.mp3.partial", 10, transcode.STALE_PARTIAL + 5)
    fresh = fill(folder, "2-1-1-128.mp3.partial", 10, transcode.STALE_PARTIAL - 60)
    running = fill(folder, "3-1-1-128.mp3.partial", 10, transcode.STALE_PARTIAL + 5)
    cache = Transcoder(cache_dir=folder, command=stub())
    cache._jobs["3-1-1-128.mp3"] = Job("3-1-1-128.mp3", running, folder / "3-1-1-128.mp3")
    cache.prune()
    assert not old.exists() and fresh.exists() and running.exists()


def test_a_finished_song_prunes_the_cache_and_is_not_itself_what_goes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log"))
    folder = tmp_path / "c"
    old = fill(folder, "9-1-1-128.mp3", 100, 500)
    made = Transcoder(cache_dir=folder, cache_mb=0, command=stub())
    try:
        path = song(tmp_path, "ok 2 0")
        key = made.key(1, path, 128)
        assert wait(made.job(key, path, 128)).ok
        assert (folder / key).exists() and not old.exists()
    finally:
        made.close()


def test_two_listeners_and_a_pruning_at_once_do_no_harm(world) -> None:
    paths = [song(world.tmp, "ok 6 10", f"s{n}.flac") for n in range(4)]
    jobs = [world.t.job(world.t.key(n, path, 128), path, 128) for n, path in enumerate(paths)]
    heard: list[bytes] = []
    threads = [threading.Thread(target=lambda j=job: heard.append(b"".join(world.t.follow(j)))) for job in jobs for _ in range(2)]
    for thread in threads:
        thread.start()
    for _ in range(20):
        world.t.prune()
    for thread in threads:
        thread.join(30)
    assert heard == [said(128, "0", 6)] * 8


# ------------------------------------------------------------------------------------------------------------------------------------ the settings


def server(**given) -> SimpleNamespace:
    return SimpleNamespace(**{"ffmpeg": "", "transcode_cache_mb": 2048, "transcode_jobs": 2, **given})


def test_the_usual_settings_are_taken_as_they_are_and_so_are_the_smallest_and_the_biggest_that_make_sense() -> None:
    assert transcode.settings_of(server()) == transcode.Settings() == transcode.Settings("", 2048, 2)
    assert transcode.settings_of(server(ffmpeg="  /usr/local/bin/ffmpeg  ", transcode_cache_mb=0, transcode_jobs=1)) == transcode.Settings("/usr/local/bin/ffmpeg", 0, 1)
    assert transcode.settings_of(server(transcode_cache_mb=100_000, transcode_jobs=16)) == transcode.Settings("", 100_000, 16)
    assert (transcode.DEFAULT_CACHE_MB, transcode.DEFAULT_JOBS, transcode.MOST_JOBS) == (2048, 2, 16)


@pytest.mark.parametrize("given,said", [
    ({"ffmpeg": 3}, '[server] ffmpeg must be the path of the program in quotes, or "" to look for it.'),
    ({"ffmpeg": None}, '[server] ffmpeg must be the path of the program in quotes, or "" to look for it.'),
    ({"transcode_cache_mb": -1}, "[server] transcode_cache_mb must be a whole number of megabytes, 0 or more."),
    ({"transcode_cache_mb": 1.5}, "[server] transcode_cache_mb must be a whole number of megabytes, 0 or more."),
    ({"transcode_cache_mb": "2048"}, "[server] transcode_cache_mb must be a whole number of megabytes, 0 or more."),
    ({"transcode_cache_mb": True}, "[server] transcode_cache_mb must be a whole number of megabytes, 0 or more."),
    ({"transcode_jobs": 0}, "[server] transcode_jobs must be a whole number from 1 to 16."),
    ({"transcode_jobs": 17}, "[server] transcode_jobs must be a whole number from 1 to 16."),
    ({"transcode_jobs": 2.0}, "[server] transcode_jobs must be a whole number from 1 to 16."),
    ({"transcode_jobs": "2"}, "[server] transcode_jobs must be a whole number from 1 to 16."),
    ({"transcode_jobs": True}, "[server] transcode_jobs must be a whole number from 1 to 16."),
])
def test_a_setting_that_is_wrong_is_said_in_words(given: dict, said: str) -> None:
    with pytest.raises(TranscodeError) as caught:
        transcode.settings_of(server(**given))
    assert str(caught.value) == said
