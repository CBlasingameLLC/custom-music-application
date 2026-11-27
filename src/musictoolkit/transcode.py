"""Songs at a lower bitrate, for a phone on mobile data.

A song that is bigger than a person wants to stream (a FLAC, or a 320 kbps MP3 when 128 is asked for) is converted to MP3 on the way, by ffmpeg, and the
converted file is kept, so that the next time it is a plain file. Nothing is converted unless it is asked for (`maxBitRate`, `format` or `kbps`), and
whenever the conversion cannot be done (no ffmpeg, every slot busy, a song ffmpeg cannot read) the caller sends the original, so a listener is never left
without the song.

- **What is made:** MP3 at a constant bitrate of 32, 48, 64, 96, 128, 160, 192, 256 or 320 kbps, 44.1 kHz, stereo, with no tags and no Xing header (it is
  a stream, not a file to keep tidy). MP3 because every player there is can play it, an iPhone included.
- **Starting at once:** ffmpeg runs several times faster than the song plays, so the first bytes go out as they are made and the rest follows a few
  seconds ahead of the listener. The bytes are kept in memory while the song is made (a song of an hour at 320 kbps is 144 MB) and in a file in the cache
  folder, which is renamed when the whole song is there. A second request for a song that is being made does not start a second ffmpeg: it follows the first.
- **Cache:** `<data>/cache/transcode`, the song's number, its size and date, and the bitrate in the name (so a song that was changed is made again), kept to a
  size in megabytes by taking away the least recently wanted first. A half-made file that a crash left is taken away after an hour.
- **Limits:** at most `jobs` ffmpeg processes at once (a Raspberry Pi has four cores and a web server to run), each started at a low priority and stopped when
  it has made nothing for half a minute or when the app stops.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("musictoolkit")

DEFAULT_CACHE_MB = 2048
DEFAULT_JOBS = 2
MOST_JOBS = 16
TIERS = (32, 48, 64, 96, 128, 160, 192, 256, 320)  # the bitrates (kbps) a song is converted to
DEFAULT_KBPS = 192  # when only a format was asked for
TOLERANCE = 1.1  # a song up to 10% above what was asked for is sent as it is: converting it would cost quality and save almost nothing
FORMAT = "mp3"
MEDIA_TYPE = "audio/mpeg"
SAMPLE_RATE = 44100
READ_SIZE = 65536  # the most that is taken from ffmpeg at once (what is there is taken at once: it does not wait for this many)
SEND_SIZE = 262144  # the most that is handed to a listener at once
FIRST_BYTES_WAIT = 20.0  # seconds a request waits for the first bytes of a song that is being made before it sends the original
SLOT_WAIT = 5.0  # seconds a conversion waits for a free place among the ones that are running
COMPLETE_WAIT = 180.0  # seconds a request for a part of a song waits for the whole song to be made
STALL = 30.0  # seconds without a byte from ffmpeg, after which it is stopped
STALE_PARTIAL = 3600.0  # seconds after which a half-made file that is still lying about is taken to be left by a crash
LEFT_ALONE = 600.0  # seconds a song that could not be converted is sent as it is, without trying again (a listener who was cut off asks again at once)
PARTIAL = ".partial"
LOW_PRIORITY = 10
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # (Windows: no console window for ffmpeg)


class TranscodeError(Exception):
    """A song could not be converted; the message says why. The caller sends the original instead."""


@dataclass(frozen=True)
class Source:
    """What is known of a song's file without opening it."""

    format: str | None = None
    bitrate: int | None = None  # bits per second, as the scan stored it
    size: int | None = None
    duration: float | None = None

    def kbps(self) -> float | None:
        """How many kilobits a second the file has, from its tag or, failing that, from its size and length (None when neither is known)."""
        if self.bitrate:
            return self.bitrate / 1000
        if self.size and self.duration:
            return self.size * 8 / self.duration / 1000
        return None


@dataclass(frozen=True)
class Plan:
    kbps: int


@dataclass(frozen=True)
class Settings:
    ffmpeg: str = ""  # the program, or empty to look for it on the path
    cache_mb: int = DEFAULT_CACHE_MB
    jobs: int = DEFAULT_JOBS


def settings_of(server: object) -> Settings:
    """The settings of `[server]` for converting songs (`ffmpeg`, `transcode_cache_mb`, `transcode_jobs`), checked; TranscodeError, in words, for one that is not
    what it should be."""
    ffmpeg, cache_mb, jobs = (getattr(server, name) for name in ("ffmpeg", "transcode_cache_mb", "transcode_jobs"))
    if not isinstance(ffmpeg, str):
        raise TranscodeError('[server] ffmpeg must be the path of the program in quotes, or "" to look for it.')
    if isinstance(cache_mb, bool) or not isinstance(cache_mb, int) or cache_mb < 0:
        raise TranscodeError("[server] transcode_cache_mb must be a whole number of megabytes, 0 or more.")
    if isinstance(jobs, bool) or not isinstance(jobs, int) or not 1 <= jobs <= MOST_JOBS:
        raise TranscodeError(f"[server] transcode_jobs must be a whole number from 1 to {MOST_JOBS}.")
    return Settings(ffmpeg.strip(), cache_mb, jobs)


def tier(kbps: float) -> int:
    """The highest of TIERS that is not above `kbps` (the lowest, when it is below them all)."""
    return max((one for one in TIERS if one <= kbps), default=TIERS[0])


def decide(source: Source, max_kbps: int = 0, format: str | None = None) -> Plan | None:
    """Whether a song is converted for what the client asked (Subsonic's `maxBitRate` and `format`, the web player's `kbps`), and to what; None: send the file.

    Never when `format` is `raw`. When `maxBitRate` is given: if the file is over it by more than TOLERANCE, or is of an unknown size so that it cannot be
    told. When the format asked for is MP3 and the file is not (an app that cannot play the file's own format): at that bitrate, or DEFAULT_KBPS when none was
    given. A format that is not MP3 is not made, so it is not asked for: the song is judged by the bitrate alone."""
    wanted = (format or "").strip().lower().lstrip(".")
    if wanted == "raw":
        return None
    kbps = source.kbps()
    too_big = max_kbps > 0 and (kbps is None or kbps > max_kbps * TOLERANCE)
    other_format = wanted == FORMAT and (source.format or "").strip().lower().lstrip(".") != FORMAT
    if not (too_big or other_format):
        return None
    return Plan(tier(max_kbps) if max_kbps > 0 else DEFAULT_KBPS)


def estimated_bytes(duration: float, kbps: int) -> int:
    """How big a constant-bitrate stream of this length is, near enough (to the frame) for a Content-Length that a client asked to be guessed."""
    return int(duration * kbps * 125)


def exact_length(chunks: Iterator[bytes], size: int) -> Iterator[bytes]:
    """The stream cut or filled with zero bytes to exactly `size` (a Content-Length that was promised is kept; a player skips what is not sound)."""
    left = size
    for chunk in chunks:
        if left <= 0:
            return
        chunk = chunk[:left]
        left -= len(chunk)
        yield chunk
    while left > 0:
        block = min(left, READ_SIZE)
        left -= block
        yield bytes(block)


class Job:
    """One song being made: what has been made so far, kept for everyone who is listening to it."""

    def __init__(self, key: str, partial: Path, final: Path) -> None:
        self.key, self.partial, self.final = key, partial, final
        self.buffer = bytearray()
        self.cond = threading.Condition()
        self.started = threading.Event()  # the first bytes are there, or it is over
        self.done = threading.Event()
        self.ok = False
        self.error: str | None = None
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.busy = False  # it was not made because every place was taken, which says nothing of the song
        self.deadline = time.monotonic() + STALL

    @property
    def size(self) -> int:
        return len(self.buffer)


class Transcoder:
    """Makes songs at a lower bitrate with ffmpeg (see the top of the file). `ffmpeg` is a path, or empty to look for it; `command` is the whole program to run
    in its place (what the tests use)."""

    def __init__(self, ffmpeg: str = "", cache_dir: Path | None = None, cache_mb: int = DEFAULT_CACHE_MB, jobs: int = DEFAULT_JOBS, command: Sequence[str] | None = None) -> None:
        self._ffmpeg = ffmpeg
        self._command = list(command) if command else None
        self._dir = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "musictoolkit-transcode"
        self._limit = cache_mb * 1024 * 1024
        self._slots = threading.BoundedSemaphore(max(1, jobs))
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._failed: dict[str, float] = {}  # the songs that could not be converted, and when
        self._probed: tuple[bool, str | None, str | None] | None = None
        self._closed = False

    # ------------------------------------------------------------------------------------------------------------------------------- is it there

    def _program(self) -> list[str] | None:
        if self._command:
            return list(self._command)
        found = shutil.which(self._ffmpeg) if self._ffmpeg else shutil.which("ffmpeg")
        if found is None and self._ffmpeg and Path(self._ffmpeg).is_file():
            found = self._ffmpeg
        return [found] if found else None

    def _probe(self) -> tuple[bool, str | None, str | None]:
        with self._lock:
            if self._probed is None:
                self._probed = self._look()
            return self._probed

    def _look(self) -> tuple[bool, str | None, str | None]:
        program = self._program()
        if program is None:
            return False, None, "ffmpeg was not found (on Linux: sudo apt install ffmpeg)"
        try:
            version = subprocess.run([*program, "-hide_banner", "-version"], capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
            encoders = subprocess.run([*program, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as error:
            return False, None, f"ffmpeg could not be run ({error})"
        first = version.stdout.splitlines()[0] if version.stdout.strip() else ""
        words = first.split()
        name = words[2] if len(words) > 2 and words[:2] == ["ffmpeg", "version"] else (first or None)
        if "libmp3lame" not in encoders.stdout:
            return False, name, "this ffmpeg cannot write MP3 (it was made without libmp3lame)"
        return True, name, None

    def available(self) -> bool:
        return not self._closed and self._probe()[0]

    def status(self) -> dict:
        """What About says: whether songs can be converted, with which ffmpeg, and if not why."""
        ok, version, reason = self._probe()
        return {"available": ok and not self._closed, "ffmpeg": version, "reason": reason}

    # ----------------------------------------------------------------------------------------------------------------------------------- the cache

    def key(self, track_id: int, path: Path, kbps: int) -> str:
        info = Path(path).stat()
        return f"{track_id}-{info.st_size}-{info.st_mtime_ns}-{kbps}.{FORMAT}"

    def cached(self, key: str) -> Path | None:
        """The finished file for this key, if there is one; it counts as wanted just now."""
        path = self._dir / key
        if not path.is_file():
            return None
        try:
            os.utime(path)
        except OSError:
            pass
        return path

    def failed(self, key: str) -> bool:
        """Did this song fail to be made a short while ago? Then it is sent as it is, and not tried again at once."""
        with self._lock:
            when = self._failed.get(key)
            if when is not None and time.monotonic() - when > LEFT_ALONE:
                del self._failed[key]
                return False
            return when is not None

    def prune(self, protect: Path | None = None) -> None:
        """Keep the cache to its size by taking away the least recently wanted songs, and clear half-made files that a crash left."""
        now = time.time()
        with self._lock:
            live = {job.partial for job in self._jobs.values()}
            self._failed = {key: when for key, when in self._failed.items() if time.monotonic() - when <= LEFT_ALONE}
        kept: list[tuple[float, int, Path]] = []
        try:
            entries = list(os.scandir(self._dir))
        except OSError:
            return
        for entry in entries:
            path = Path(entry.path)
            try:
                info = entry.stat()
            except OSError:
                continue
            if entry.name.endswith(PARTIAL):
                if path not in live and now - info.st_mtime > STALE_PARTIAL:
                    path.unlink(missing_ok=True)
            elif entry.name.endswith("." + FORMAT) and entry.is_file():
                kept.append((info.st_mtime, info.st_size, path))
        total = sum(size for _, size, _ in kept)
        for _, size, path in sorted(kept):
            if total <= self._limit:
                break
            if path != protect:
                path.unlink(missing_ok=True)
                total -= size

    # ----------------------------------------------------------------------------------------------------------------------------------- the work

    def _arguments(self, path: Path, kbps: int, offset: float = 0.0) -> list[str]:
        program = self._program()
        if program is None:
            raise TranscodeError("ffmpeg was not found")
        lead = ["nice", "-n", str(LOW_PRIORITY)] if not self._command and os.name == "posix" and shutil.which("nice") else []
        start = ["-ss", f"{offset:.3f}"] if offset > 0 else []
        return [
            *lead, *program, "-nostdin", "-hide_banner", "-loglevel", "error", *start, "-i", str(path),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
            "-c:a", "libmp3lame", "-b:a", f"{kbps}k", "-ar", str(SAMPLE_RATE), "-ac", "2",
            "-write_xing", "0", "-id3v2_version", "0", "-f", FORMAT, "pipe:1",
        ]

    def job(self, key: str, path: Path, kbps: int) -> Job:
        """The job that is making this song: the one that is running, or a new one. Raises TranscodeError when songs cannot be made."""
        if not self.available():
            raise TranscodeError(self._probe()[2] or "converting is not available")
        argv = self._arguments(path, kbps)
        with self._lock:
            running = self._jobs.get(key)
            if running is not None:
                return running
            made = Job(key, self._dir / (key + PARTIAL), self._dir / key)
            self._jobs[key] = made
        threading.Thread(target=self._run, args=(made, argv), name="transcode", daemon=True).start()
        return made

    def _run(self, job: Job, argv: list[str]) -> None:
        got_slot = self._slots.acquire(timeout=SLOT_WAIT)
        try:
            if not got_slot:
                job.busy = True
                job.error = "every place for converting a song is taken"
                return
            self._pump(job, argv)
        except Exception as error:  # (whatever it was, the listener is sent the original)
            logger.warning("Converting a song failed: %s", error)
            job.error = job.error or f"{type(error).__name__}: {error}"
            with contextlib.suppress(OSError):  # (the folder may be the very thing that is wrong; and a file that is not there is no trouble)
                job.partial.unlink()
        finally:
            if got_slot:
                self._slots.release()
            with self._lock:
                self._jobs.pop(job.key, None)
                if not job.ok and not job.cancelled and not job.busy:
                    self._failed[job.key] = time.monotonic()
            if job.ok:  # (before it is said to be done: whoever hears that finds the cache as it should be)
                try:
                    self.prune(protect=job.final)
                except OSError as error:
                    logger.warning("Clearing the cache of converted songs failed: %s", error)
            with job.cond:
                job.done.set()
                job.started.set()
                job.cond.notify_all()

    def _pump(self, job: Job, argv: list[str]) -> None:
        job.partial.parent.mkdir(parents=True, exist_ok=True)
        with job.partial.open("wb") as out, tempfile.TemporaryFile() as complaints:
            job.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=complaints, creationflags=NO_WINDOW)
            threading.Thread(target=self._watch, args=(job,), name="transcode-watch", daemon=True).start()
            assert job.proc.stdout is not None
            while True:
                job.deadline = time.monotonic() + STALL
                chunk = job.proc.stdout.read1(READ_SIZE)
                if not chunk:
                    break
                out.write(chunk)
                with job.cond:
                    job.buffer.extend(chunk)
                    job.cond.notify_all()
                job.started.set()
            code = job.proc.wait()
            complaints.seek(0)
            said = complaints.read().decode("utf-8", "replace").strip()[-400:]
        if code == 0 and job.size and not job.cancelled:
            os.replace(job.partial, job.final)  # (the file is closed: followers read the bytes kept in memory, so that a rename is no trouble on Windows)
            job.ok = True
        else:
            job.partial.unlink(missing_ok=True)
            job.error = job.error or said or ("it was stopped" if job.cancelled else f"ffmpeg ended with {code}" + ("" if job.size else " and made nothing"))

    def _watch(self, job: Job) -> None:
        """Stop ffmpeg when it has made nothing for STALL seconds (a file on a drive that went away can hold it for ever)."""
        while not job.done.wait(1.0):
            if time.monotonic() > job.deadline and job.proc is not None:
                job.error = f"ffmpeg made nothing for {STALL:g} seconds"
                job.proc.kill()
                return

    def follow(self, job: Job, offset: int = 0) -> Iterator[bytes]:
        """The bytes of the song as they are made, from `offset`. Ends when the song is whole; raises TranscodeError when it could not be made after
        bytes were handed out (the connection is then cut: there is no way to say it in the stream)."""
        position = offset
        while True:
            with job.cond:
                while job.size <= position and not job.done.is_set():
                    job.cond.wait(0.5)
                end = min(job.size, position + SEND_SIZE)
                data = bytes(job.buffer[position:end])
                finished = job.done.is_set()
            if data:
                position += len(data)
                yield data
            elif finished:
                if not job.ok:
                    raise TranscodeError(job.error or "the song could not be converted")
                return

    def direct(self, path: Path, kbps: int, offset: float) -> Iterator[bytes]:
        """A song from `offset` seconds, made for this listener only and not kept (a client that seeks a converted song by asking for it from a time).
        Stopped when the listener goes."""
        if not self.available():
            raise TranscodeError(self._probe()[2] or "converting is not available")
        if not self._slots.acquire(timeout=SLOT_WAIT):
            raise TranscodeError("every place for converting a song is taken")
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(self._arguments(path, kbps, offset), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            assert proc.stdout is not None
            while chunk := proc.stdout.read1(READ_SIZE):
                yield chunk
        finally:
            if proc is not None:
                proc.kill()
                proc.wait()
            self._slots.release()

    def close(self) -> None:
        """Stop what is running (the app is stopping)."""
        self._closed = True
        with self._lock:
            running = list(self._jobs.values())
        for job in running:
            job.cancelled = True
            if job.proc is not None:
                try:
                    job.proc.kill()
                except OSError:
                    pass
