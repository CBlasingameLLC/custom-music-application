"""The corners of the converter that the first tests left open: each test here answers a change to the code (a mutant, in the check made of 0.15.0) that the others let
live. Where the answer is in the ordinary behaviour, the tests that show it are in test_transcode.py and test_transcode_web.py."""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from musictoolkit import transcode
from musictoolkit.transcode import Job, Plan, Settings, Source, TranscodeError, Transcoder, decide, exact_length
from musictoolkit.web import streaming

STUB = [sys.executable, str(Path(__file__).with_name("fake_ffmpeg.py"))]
FLAC = Source("flac", 900_000)


def song(folder: Path, plan: str, name: str = "song.flac") -> Path:
    path = folder / name
    path.write_text(plan, encoding="utf-8")
    return path


def clock(monkeypatch: pytest.MonkeyPatch, *, monotonic: float = 1000.0, now: float | None = None) -> dict:
    """The converter's own clock, set by hand (the module's `time` only, not the whole program's)."""
    hands = {"monotonic": monotonic, "time": now if now is not None else time.time()}
    monkeypatch.setattr(transcode, "time", SimpleNamespace(monotonic=lambda: hands["monotonic"], time=lambda: hands["time"], sleep=time.sleep))
    return hands


# ----------------------------------------------------------------------------------------------------------------------------------- what is asked for


def test_nothing_asked_for_is_the_file_and_a_bitrate_of_one_is_a_bitrate() -> None:
    assert decide(Source("mp3", 320_000)) is None  # (no bitrate, no format: the arguments' own defaults)
    assert decide(FLAC, 1) == Plan(32)  # (one is asked for, and it is the lowest there is)


def test_a_song_is_converted_when_it_is_over_the_bitrate_by_more_than_the_tolerance_and_not_when_it_is_exactly_that_over() -> None:
    assert transcode.TOLERANCE == 1.1 and 40 * transcode.TOLERANCE == 44.0  # (a bitrate that sits exactly on the line)
    assert decide(Source("mp3", 44_000), 40) is None
    assert decide(Source("mp3", 44_001), 40) == Plan(32)


def test_a_format_is_the_word_without_the_dots_in_front_of_it_and_nothing_else() -> None:
    assert decide(FLAC, 0, "..mp3") == Plan(192) and decide(FLAC, 0, " .MP3 ") == Plan(192)
    assert decide(FLAC, 0, "~mp3") is None  # (a tilde is not a dot)
    assert decide(Source("~mp3", 320_000), 0, "mp3") == Plan(192)  # (and nor is it, in the name of the song's own format)
    assert decide(Source(".mp3", 320_000), 0, "mp3") is None


@pytest.mark.parametrize("thing", [Source(), Plan(64), Settings()])
def test_what_is_decided_is_not_changed_afterwards(thing: object) -> None:
    field = dataclasses.fields(thing)[0].name  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(thing, field, None)


# ----------------------------------------------------------------------------------------------------------------------------- the length that was promised


def test_a_stream_is_cut_at_the_length_and_not_a_chunk_after_it_and_filled_to_it_to_the_last_byte() -> None:
    assert list(exact_length(iter([b"ab", b"cd"]), 2)) == [b"ab"]  # (when it is full, nothing empty is handed out after it)
    assert list(exact_length(iter([b"abcdef"]), 4)) == [b"abcd"]
    assert list(exact_length(iter([b"ab"]), 3)) == [b"ab", b"\x00"]  # (the last byte is made up too)
    assert list(exact_length(iter([b"a"]), 0)) == []
    assert list(exact_length(iter([]), transcode.READ_SIZE + 5)) == [bytes(transcode.READ_SIZE), bytes(5)]


def test_what_a_client_asked_to_be_guessed_is_not_guessed_for_a_song_of_no_length_or_a_negative_one() -> None:
    body = iter([b"a"])
    same, headers = streaming._promised(body, {"x": "1"}, -5.0, 64, True)
    assert same is body and headers == {"x": "1"}
    same, headers = streaming._promised(body, {}, 0.0, 64, True)
    assert same is body and headers == {}
    same, headers = streaming._promised(body, {}, None, 64, True)
    assert same is body and headers == {}
    same, headers = streaming._promised(body, {}, 10.0, 64, False)  # (not asked for)
    assert same is body and headers == {}
    other, headers = streaming._promised(iter([b"a"]), {}, 10.0, 64, True)
    assert headers == {"Content-Length": "80000"} and b"".join(other) == b"a" + bytes(79_999)


@pytest.mark.parametrize("header", ["bytes=0-", " bytes=0- ", "Bytes=0-", "bytes= 0-", "bytes =0-", "BYTES = 0 -"])
def test_a_request_from_the_first_byte_to_the_end_is_one_whatever_the_spaces_and_capitals(header: str) -> None:
    assert streaming.from_the_start(header) is True


@pytest.mark.parametrize("header", ["bytes=0-1", "bytes=1-", "bytes=0-0", "", "bytes=-1", "items=0-"])
def test_a_request_for_a_part_is_not(header: str) -> None:
    assert streaming.from_the_start(header) is False


# ---------------------------------------------------------------------------------------------------------------------------------------- the maker


def test_a_job_has_time_before_it_is_taken_to_have_stopped() -> None:
    job = Job("k", Path("a.partial"), Path("a"))
    assert job.deadline > time.monotonic() + transcode.STALL - 5


def test_a_maker_without_a_folder_uses_one_of_its_own_in_the_temporary_folder_and_one_without_places_has_one() -> None:
    assert Transcoder()._dir == Path(tempfile.gettempdir()) / "musictoolkit-transcode"


@pytest.mark.parametrize("jobs", [0, -3, 1])
def test_a_maker_always_has_a_place_to_make_one_song(tmp_path: Path, jobs: int) -> None:
    made = Transcoder(cache_dir=tmp_path / "cache", jobs=jobs, command=STUB)
    path = song(tmp_path, "ok 2 0")
    job = made.job(made.key(1, path, 64), path, 64)
    assert job.done.wait(10) and job.ok, job.error
    made.close()


def test_the_program_is_asked_for_its_version_and_its_encoders_and_the_version_is_the_third_word_of_the_first_line(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict]] = []
    said = {"-version": "", "-encoders": " A....D libmp3lame libmp3lame MP3 (codec mp3)\n"}

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=said[argv[-1]])

    monkeypatch.setattr(transcode, "subprocess", SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError))
    for first, name in [
        ("ffmpeg version 6.1.1 Copyright (c) 2000-2023 the FFmpeg developers", "6.1.1"),
        ("ffmpeg version 7", "7"),
        ("ffmpeg version", "ffmpeg version"),  # (two words: there is no third to take)
        ("avconv version 9.9 Copyright", "avconv version 9.9 Copyright"),  # (another program's line is kept as it is)
        ("", None),
    ]:
        calls.clear()
        said["-version"] = first + "\nbuilt with gcc\n" if first else ""
        got = Transcoder(command=["prog"]).status()
        assert got == {"available": True, "ffmpeg": name, "reason": None}, first
        assert [argv for argv, _ in calls] == [["prog", "-hide_banner", "-version"], ["prog", "-hide_banner", "-encoders"]]
        assert all(kwargs["timeout"] == 30 and kwargs["capture_output"] is True and kwargs["text"] is True for _, kwargs in calls)


def test_asking_for_the_arguments_without_ffmpeg_is_an_error_and_a_start_is_asked_for_only_when_it_is_after_the_beginning(tmp_path: Path) -> None:
    nothing = Transcoder(ffmpeg=str(tmp_path / "no-such-ffmpeg"), cache_dir=tmp_path)
    with pytest.raises(TranscodeError) as refused:
        nothing._arguments(tmp_path / "a.flac", 128)
    assert str(refused.value) == "ffmpeg was not found"
    made = Transcoder(cache_dir=tmp_path, command=["prog"])
    path = tmp_path / "a.flac"
    assert "-ss" not in made._arguments(path, 128) and "-ss" not in made._arguments(path, 128, 0.0)
    for offset, written in ((0.5, "0.500"), (1.0, "1.000"), (90.25, "90.250")):
        args = made._arguments(path, 128, offset)
        assert args[args.index("-ss") + 1] == written and args.index("-ss") < args.index("-i")  # (before the input: it is sought, not read through)


def test_a_maker_that_has_been_stopped_makes_nothing_and_says_so(tmp_path: Path) -> None:
    made = Transcoder(cache_dir=tmp_path / "cache", command=STUB)
    path = song(tmp_path, "ok 1 0")
    made.close()
    with pytest.raises(TranscodeError) as refused:
        made.job(made.key(1, path, 64), path, 64)
    assert str(refused.value) == "converting is not available"
    with pytest.raises(TranscodeError) as refused:
        next(made.direct(path, 64, 5.0))
    assert str(refused.value) == "converting is not available"


# --------------------------------------------------------------------------------------------------------------------------------------- the clock


def test_a_song_that_failed_is_left_alone_for_exactly_the_time_it_is_said_to_be(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hands = clock(monkeypatch)
    made = Transcoder(cache_dir=tmp_path, command=["prog"])
    made._failed["k"] = 1000.0
    hands["monotonic"] = 1000.0 + transcode.LEFT_ALONE
    assert made.failed("k") is True  # (on the last second it still is)
    hands["monotonic"] += 0.001
    assert made.failed("k") is False and "k" not in made._failed
    assert made.failed("never") is False


def test_what_was_forgotten_about_failures_is_forgotten_by_the_cleaning_up_at_the_same_second(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hands = clock(monkeypatch)
    made = Transcoder(cache_dir=tmp_path, command=["prog"])
    made._failed["k"] = 1000.0
    hands["monotonic"] = 1000.0 + transcode.LEFT_ALONE
    made.prune()
    assert "k" in made._failed
    hands["monotonic"] += 0.001
    made.prune()
    assert made._failed == {}


def test_the_cache_is_kept_to_its_size_to_the_byte_and_a_song_that_was_just_made_is_never_the_one_that_goes(tmp_path: Path) -> None:
    folder = tmp_path / "cache"
    folder.mkdir()
    made = Transcoder(cache_dir=folder, command=["prog"])
    files = []
    for number, age in enumerate((300, 200, 100)):
        path = folder / f"{number}.mp3"
        path.write_bytes(bytes(1000))
        os.utime(path, (time.time() - age, time.time() - age))
        files.append(path)
    made._limit = 3000
    made.prune()
    assert all(path.exists() for path in files)  # (exactly full is not over)
    made._limit = 2999
    made.prune()
    assert [path.exists() for path in files] == [False, True, True]  # (the least recently wanted goes)
    made._limit = 0
    made.prune(protect=files[2])
    assert [path.exists() for path in files] == [False, False, True]  # (and not the one that was asked to be kept)


def test_a_half_made_file_is_taken_to_be_a_crash_s_only_after_the_hour_and_not_while_a_job_makes_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder = tmp_path / "cache"
    folder.mkdir()
    hands = clock(monkeypatch, now=1_800_000_000.0)
    made = Transcoder(cache_dir=folder, command=["prog"])
    old, live = folder / "old.mp3.partial", folder / "live.mp3.partial"
    for path in (old, live):
        path.write_bytes(b"x")
        os.utime(path, (1_800_000_000.0 - transcode.STALE_PARTIAL, 1_800_000_000.0 - transcode.STALE_PARTIAL))
    made._jobs["live.mp3"] = Job("live.mp3", live, folder / "live.mp3")
    made.prune()
    assert old.exists() and live.exists()  # (it is exactly an hour old: that is not more than an hour)
    hands["time"] += 1
    made.prune()
    assert not old.exists() and live.exists()


def test_cleaning_up_after_files_that_went_by_themselves_is_no_trouble(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A file that is listed and is gone when it is to be taken away (a song was just replaced, a person cleared the folder) is not a fault."""
    folder = tmp_path / "cache"
    folder.mkdir()

    def entry(name: str, mtime: float, size: int):
        return SimpleNamespace(name=name, path=str(folder / name), stat=lambda: SimpleNamespace(st_mtime=mtime, st_size=size), is_file=lambda: True)

    entries = [entry("gone.mp3.partial", 0.0, 1), entry("gone.mp3", 0.0, 10)]
    monkeypatch.setattr(transcode, "os", SimpleNamespace(scandir=lambda _folder: entries, utime=os.utime, replace=os.replace, name=os.name))
    made = Transcoder(cache_dir=folder, cache_mb=0, command=["prog"])
    made.prune()  # (it raises if it asks for a file that is not there to be taken away as if it had to be)


def test_a_program_that_has_made_nothing_for_the_time_is_stopped_but_not_a_moment_before(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hands = clock(monkeypatch, monotonic=1000.0)
    killed = threading.Event()
    job = Job("k", tmp_path / "k.partial", tmp_path / "k")
    job.deadline = 1000.0
    job.proc = SimpleNamespace(kill=killed.set)  # type: ignore[assignment]
    watcher = threading.Thread(target=Transcoder(cache_dir=tmp_path, command=["prog"])._watch, args=(job,), daemon=True)
    watcher.start()
    assert not killed.wait(1.6)  # (it looks once a second: it has looked, at the very second the time is up, and let it be)
    hands["monotonic"] = 1000.001
    assert killed.wait(3) and job.error == f"ffmpeg made nothing for {transcode.STALL:g} seconds"
    watcher.join(3)
    assert not watcher.is_alive()


# --------------------------------------------------------------------------------------------------------------------------------------- what runs


def test_a_song_that_could_not_be_made_is_said_in_the_log_and_leaves_no_half_made_file_and_is_left_alone(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="musictoolkit")
    made = Transcoder(cache_dir=tmp_path / "cache", command=STUB)
    path = song(tmp_path, "ok 1 0")
    key = made.key(1, path, 64)

    def broken(job: Job, argv: list[str]) -> None:
        job.partial.parent.mkdir(parents=True, exist_ok=True)
        job.partial.write_bytes(b"half")
        raise RuntimeError("boom")

    made._pump = broken  # type: ignore[method-assign]
    job = made.job(key, path, 64)
    assert job.done.wait(10) and not job.ok
    assert job.error == "RuntimeError: boom" and not job.partial.exists() and made.failed(key) is True
    assert [r.getMessage() for r in caplog.records if r.name == "musictoolkit"] == ["Converting a song failed: boom"]
    made.close()


def test_the_threads_that_make_songs_do_not_hold_the_program_up_when_it_ends(tmp_path: Path) -> None:
    made = Transcoder(cache_dir=tmp_path / "cache", command=STUB)
    path = song(tmp_path, "hang")
    job = made.job(made.key(1, path, 64), path, 64)
    assert job.started.wait(10)
    threads = [thread for thread in threading.enumerate() if thread.name == "transcode"]
    assert len(threads) == 1 and threads[0].daemon is True
    made.close()
    assert job.done.wait(10)


def test_a_song_that_made_nothing_ends_the_wait_of_whoever_waits_for_its_first_bytes_at_once(tmp_path: Path) -> None:
    made = Transcoder(cache_dir=tmp_path / "cache", command=STUB)
    path = song(tmp_path, "crash 0 0")
    job = made.job(made.key(1, path, 64), path, 64)
    assert job.done.wait(10) and job.started.is_set() and not job.ok and job.size == 0
    made.close()


def test_a_song_made_for_one_listener_has_a_place_or_is_refused_and_the_program_is_stopped_when_the_listener_goes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pids = tmp_path / "pids"
    monkeypatch.setenv("FAKE_PIDS", str(pids))
    monkeypatch.setattr(transcode, "SLOT_WAIT", 0.05)
    made = Transcoder(cache_dir=tmp_path / "cache", jobs=1, command=STUB)
    path = song(tmp_path, "hang")
    assert made._slots.acquire(blocking=False)  # (the only place is taken)
    with pytest.raises(TranscodeError, match="every place for converting a song is taken"):
        next(made.direct(path, 64, 5.0))
    made._slots.release()

    heard = made.direct(path, 64, 5.0)
    assert next(heard).startswith(b"[64|5.000|0000]")  # (it began: the stand-in gave a piece, and now sleeps for a minute)
    pid = int(pids.read_text(encoding="utf-8").split()[-1])
    heard.close()  # (the listener goes)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert made._slots.acquire(blocking=False)  # (and the place is free again)
    made._slots.release()
    made.close()


# -------------------------------------------------------------------------------------------------------------------------------------- listeners


class Counting:
    """A job's condition that counts what is done with it (and does it)."""

    def __init__(self, inner: threading.Condition, job: Job) -> None:
        self.inner, self.job, self.waits, self.notices, self.after_the_end = inner, job, 0, 0, 0

    def __enter__(self):
        return self.inner.__enter__()

    def __exit__(self, *details):
        return self.inner.__exit__(*details)

    def wait(self, timeout: float | None = None) -> bool:
        self.waits += 1
        return self.inner.wait(timeout)

    def notify_all(self) -> None:
        self.notices += 1
        self.after_the_end += self.job.done.is_set()
        self.inner.notify_all()


def test_a_listener_waits_for_what_is_not_made_yet_and_the_maker_tells_it_when_it_is(tmp_path: Path) -> None:
    made = Transcoder(cache_dir=tmp_path / "cache", command=STUB)
    path = song(tmp_path, "ok 4 250")
    job = made.job(made.key(1, path, 64), path, 64)
    counting = job.cond = Counting(job.cond, job)  # type: ignore[assignment]
    heard = b"".join(made.follow(job))
    assert len(heard) == 4000 and job.ok
    assert counting.waits >= 2  # (it did not spin on the spot where it had heard all that there was)
    assert counting.notices >= 4 and counting.after_the_end >= 1  # (and was told of each piece, and of the end)
    made.close()


def test_two_songs_one_after_the_other_are_made_in_the_same_folder(tmp_path: Path) -> None:
    made = Transcoder(cache_dir=tmp_path / "cache", command=STUB)
    for number in (1, 2):
        path = song(tmp_path, "ok 2 0", f"song{number}.flac")
        job = made.job(made.key(number, path, 64), path, 64)
        assert job.done.wait(10) and job.ok, job.error
    assert sorted(item.name.split("-")[0] for item in (tmp_path / "cache").iterdir()) == ["1", "2"]
    made.close()


def test_a_cache_folder_that_is_two_folders_deep_is_made(tmp_path: Path) -> None:
    made = Transcoder(cache_dir=tmp_path / "data" / "cache" / "transcode", command=STUB)
    path = song(tmp_path, "ok 1 0")
    job = made.job(made.key(1, path, 64), path, 64)
    assert job.done.wait(10) and job.ok, job.error
    made.close()


def test_the_thread_that_watches_the_program_does_not_hold_the_program_up_either(tmp_path: Path) -> None:
    made = Transcoder(cache_dir=tmp_path / "cache", command=STUB)
    path = song(tmp_path, "hang")
    job = made.job(made.key(1, path, 64), path, 64)
    assert job.started.wait(10)
    watchers = [thread for thread in threading.enumerate() if thread.name == "transcode-watch"]
    assert len(watchers) == 1 and watchers[0].daemon is True
    made.close()
    assert job.done.wait(10)


def test_a_job_whose_cache_cannot_be_cleaned_after_it_is_said_in_the_log_and_the_song_is_kept(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="musictoolkit")
    made = Transcoder(cache_dir=tmp_path / "cache", command=STUB)

    def broken(protect=None) -> None:
        raise OSError("full")

    made.prune = broken  # type: ignore[method-assign]
    path = song(tmp_path, "ok 2 0")
    job = made.job(made.key(1, path, 64), path, 64)
    assert job.done.wait(10) and job.ok and job.final.exists()
    assert "Clearing the cache of converted songs failed: full" in [r.getMessage() for r in caplog.records]
    made.close()


def test_what_ffmpeg_complained_of_is_the_last_four_hundred_characters_of_it_read_as_far_as_they_can_be(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    made = Transcoder(cache_dir=tmp_path / "cache", command=STUB)
    path = song(tmp_path, "crash 1 0")
    monkeypatch.setenv("FAKE_STDERR_HEX", (b"x" * 1000 + b"END").hex())
    job = made.job(made.key(1, path, 64), path, 64)
    assert job.done.wait(10) and job.error == "x" * 397 + "END"
    path = song(tmp_path, "crash 1 0", "other.flac")
    monkeypatch.setenv("FAKE_STDERR_HEX", b"bad \xff byte".hex())  # (not text: what cannot be read is made visible, and does not stop the reading)
    job = made.job(made.key(2, path, 64), path, 64)
    assert job.done.wait(10) and job.error == "bad \ufffd byte"
    made.close()


def test_a_half_made_file_that_was_taken_away_by_someone_is_no_trouble_when_the_program_fails(tmp_path: Path) -> None:
    made = Transcoder(cache_dir=tmp_path / "cache", command=STUB)
    path = song(tmp_path, "crash 5 200")
    job = made.job(made.key(1, path, 64), path, 64)
    assert job.started.wait(10)
    job.partial.unlink()  # (the cache was cleared by hand while the song was being made)
    assert job.done.wait(10) and not job.ok
    assert job.error == "Error: boom"  # (what ffmpeg said, not what went wrong with the file that was not there)
    made.close()
