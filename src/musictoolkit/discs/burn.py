"""Burning music onto a disc: which songs can go on an audio CD (and why one cannot), turning each into the plain audio a CD holds, and how
much of the disc that takes.

An audio CD holds raw 44.1 kHz, 16-bit, stereo sound, 75 sectors of 2352 bytes to the second. A song is decoded to exactly that in a temporary
file before the drive is touched, so a song that cannot be read stops the burn while the disc is still blank. Songs that already are 16-bit
44.1 kHz PCM (a CD rip kept as FLAC or WAV, for example) pass through bit for bit; anything else is converted, and said so.

M4A, AAC and WMA songs are decoded by Windows itself (the disc helper asks Media Foundation) into a temporary WAV file, which is then converted
like any other song.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from musictoolkit.discs.helper import SECTOR_BYTES, DiscError, DiscHelper, DiscUnavailable
from musictoolkit.discs.toc import MAX_TRACKS, SECTORS_PER_SECOND

SAMPLE_RATE = 44100
CHANNELS = 2
FRAME_BYTES = 4  # one stereo frame of 16 bits
FRAMES_PER_SECTOR = SECTOR_BYTES // FRAME_BYTES  # 588
MIN_SECTORS = 4 * SECTORS_PER_SECOND  # a track on a CD-R written song by song is at least four seconds long
GAP_SECTORS = 2 * SECTORS_PER_SECOND  # and, written song by song, the recorder leaves two seconds of silence before each song after the first
NO_GAPLESS = "This recorder cannot write a music CD without gaps between the songs. Choose two seconds between songs instead."
RESERVE_SECTORS = 4 * SECTORS_PER_SECOND  # kept back when judging whether the songs fit: the recorder's own count of free sectors is not exact about the lead-in
CHUNK_FRAMES = SAMPLE_RATE * 5  # decoded five seconds at a time
SCRATCH_MARGIN = 50 * 1024 * 1024
DITHER_SEED = 0x4D544B  # the same song is always decoded to the same bytes
RESAMPLE_QUALITY = "VHQ"

# Songs the audio library cannot read but Windows can decode (when this Windows has the decoder), by file extension.
WINDOWS_DECODES = {"m4a": "M4A", "mp4": "MP4", "aac": "AAC", "wma": "WMA"}
# What a person might expect to work and does not, by file extension, named for the message.
NOT_YET = {
    "m4p": "Copy-protected M4P", "alac": "ALAC", "ape": "APE", "wv": "WavPack", "mpc": "Musepack",
    "ac3": "AC-3", "dts": "DTS", "tta": "TTA", "dsf": "DSD", "dff": "DSD",
}
BITS = {"PCM_S8": 8, "PCM_U8": 8, "PCM_24": 24, "PCM_32": 32, "FLOAT": 32, "DOUBLE": 64}
FLOATING = {"FLOAT", "DOUBLE"}


class BurnError(Exception):
    """A song could not be turned into CD audio, or the burn cannot go on. The message is written for the person."""

    for_the_person = True


def _library():
    try:
        import soundfile
    except (ImportError, OSError) as exc:
        raise BurnError(f"The audio library that reads songs could not be loaded ({exc}). Install Music Toolkit again.") from None
    return soundfile


def _resampler(rate: int):
    try:
        import soxr
    except (ImportError, OSError) as exc:
        raise BurnError(f"The audio library that converts sample rates could not be loaded ({exc}). Install Music Toolkit again.") from None
    return soxr.ResampleStream(rate, SAMPLE_RATE, CHANNELS, dtype="float32", quality=RESAMPLE_QUALITY)


def sectors_for_frames(frames: int) -> int:
    """The sectors a song of this many 44.1 kHz frames takes on the disc: whole sectors (the last one padded with silence), and at least four seconds."""
    return max(MIN_SECTORS, -(-frames // FRAMES_PER_SECTOR))


def _minutes(sectors: int) -> str:
    seconds = round(sectors / SECTORS_PER_SECOND)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _span(sectors: int) -> str:
    """A length of music in words, rounded up: "45 seconds", "1 minute", "3 minutes 20 seconds"."""
    seconds = max(1, -(-sectors // SECTORS_PER_SECOND))
    minutes, seconds = divmod(seconds, 60)
    parts = [f"{minutes} minute{'s' if minutes != 1 else ''}"] if minutes else []
    if seconds:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return " ".join(parts)


def _size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------------------------------------------------------------------------------------- can this song go on a music CD?


@dataclass
class SongCheck:
    """What was found out about one song file."""

    path: str
    verdict: str  # ok | converted (needs changing on the way: rate, channels, depth) | cannot | pending (Windows has still to say)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    sectors: int = 0  # on the disc, with the four-second minimum; 0 when the song cannot be burned
    rate: int = 0
    channels: int = 0
    format: str = ""
    subtype: str = ""
    via: str = ""  # "windows": decoded by Windows (the disc helper) first; "" for what the audio library reads itself

    @property
    def pending(self) -> bool:
        """Windows has still to be asked what this song is like (see `windows_song`)."""
        return self.via == "windows" and self.verdict == "pending"

    @property
    def exact(self) -> bool:
        """The audio goes on the disc exactly as it is in the file (16-bit PCM at 44.1 kHz; mono is doubled without changing a sample)."""
        return self.subtype == "PCM_16" and self.rate == SAMPLE_RATE and self.channels in (1, 2)


def _cannot(path: Path, why: str, **found: Any) -> SongCheck:
    return SongCheck(str(path), "cannot", [why], **found)


def inspect_song(path: str | Path) -> SongCheck:
    """Look at a song file and say whether it can go on a music CD. Reads the header only."""
    path = Path(path)
    extension = path.suffix.lower().lstrip(".")
    if not path.is_file():
        return _cannot(path, "The file is not there any more.")
    try:
        info = _library().info(str(path))
    except (OSError, RuntimeError, ValueError) as exc:
        if extension in WINDOWS_DECODES:
            return SongCheck(str(path), "pending", via="windows", format=WINDOWS_DECODES[extension])
        name = NOT_YET.get(extension)
        if name:
            return _cannot(path, f"{name} songs cannot be burned to a music CD yet. MP3, FLAC, WAV, Ogg, Opus, M4A, AAC and WMA can.")
        return _cannot(path, f"The song cannot be read ({str(exc).strip() or type(exc).__name__}).")
    found = {"rate": int(info.samplerate), "channels": int(info.channels), "format": info.format, "subtype": info.subtype}
    if info.channels > CHANNELS:
        return _cannot(path, f"This song has {info.channels} channels; a music CD holds two (stereo).", **found)
    if info.frames <= 0 or info.samplerate <= 0:
        return _cannot(path, "There is no sound in this file.", **found)
    out_frames = info.frames if info.samplerate == SAMPLE_RATE else -(-info.frames * SAMPLE_RATE // info.samplerate)
    notes: list[str] = []
    converted = False
    if info.samplerate != SAMPLE_RATE:
        converted = True
        notes.append(f"Converted from {info.samplerate / 1000:g} kHz to 44.1 kHz.")
    if info.channels == 1:
        converted = True
        notes.append("Mono: both speakers get the same sound.")
    if BITS.get(info.subtype, 0) > 16:
        converted = True
        notes.append(f"{'Floating-point' if info.subtype in FLOATING else str(BITS[info.subtype]) + '-bit'} sound is reduced to 16 bits.")
    if out_frames < MIN_SECTORS * FRAMES_PER_SECTOR:
        notes.append("Shorter than four seconds: silence is added to make four, as a music CD needs.")
    return SongCheck(
        str(path), "converted" if converted else "ok", notes, seconds=out_frames / SAMPLE_RATE, sectors=sectors_for_frames(out_frames), **found,
    )


def windows_song(path: str | Path, info: dict[str, Any] | None) -> SongCheck:
    """The check for a song that only Windows can decode, from what the disc helper said about it (`info`; None if it said nothing)."""
    path = Path(path)
    if not info or not info.get("ok"):
        return _cannot(path, str((info or {}).get("error") or "Windows could not say what is in this song."), via="windows")
    rate, channels, milliseconds = int(info.get("rate") or 0), int(info.get("channels") or 0), round(float(info.get("seconds") or 0) * 1000)
    kind = str(info.get("codec") or WINDOWS_DECODES.get(path.suffix.lower().lstrip(".")) or "")
    found: dict[str, Any] = {"rate": rate, "channels": channels, "format": kind, "subtype": "WINDOWS", "via": "windows"}
    if channels > CHANNELS:
        return _cannot(path, f"This song has {channels} channels; a music CD holds two (stereo).", **found)
    if rate <= 0 or channels <= 0:
        return _cannot(path, "Windows could not say what the sound in this song is like.", **found)
    if milliseconds <= 0:
        return _cannot(path, "Windows could not say how long this song is.", **found)
    out_frames = -(-milliseconds * SAMPLE_RATE // 1000)  # (whole frames: the length is known to the millisecond)
    notes = [f"Decoded by Windows{f' ({kind})' if kind else ''} and made into CD sound."]
    if rate != SAMPLE_RATE:
        notes.append(f"Converted from {rate / 1000:g} kHz to 44.1 kHz.")
    if channels == 1:
        notes.append("Mono: both speakers get the same sound.")
    if out_frames < MIN_SECTORS * FRAMES_PER_SECTOR:
        notes.append("Shorter than four seconds: silence is added to make four, as a music CD needs.")
    return SongCheck(str(path), "converted", notes, seconds=out_frames / SAMPLE_RATE, sectors=sectors_for_frames(out_frames), **found)


def check_with_windows(paths: list[str]) -> dict[str, SongCheck]:
    """Ask Windows (the disc helper) about songs only it can decode: {path: check}. If the helper cannot be started or asked, each of the songs is
    one that cannot be burned, with the reason in words."""
    paths = list(dict.fromkeys(paths))
    if not paths:
        return {}
    answers: dict[str, dict[str, Any]] = {}
    try:
        with DiscHelper() as helper:
            for first in range(0, len(paths), 50):
                for entry in helper.call("inspect", paths=paths[first : first + 50]).get("songs") or []:
                    answers[str(entry.get("path"))] = entry
    except (DiscError, DiscUnavailable) as error:
        return {p: _cannot(Path(p), f"Windows could not be asked to decode this song ({str(error).rstrip('.')}).", via="windows") for p in paths}
    return {p: windows_song(p, answers.get(p)) for p in paths}


# ---------------------------------------------------------------------------------------- turning a song into CD audio


def _quantise(samples: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Floating-point audio (full scale is 1.0) to 16-bit integers: rounded, with one least-significant bit of triangular noise added first so
    that quiet passages fade out cleanly instead of turning into distortion, and clipped rather than wrapped."""
    scaled = samples * 32768.0
    scaled += rng.random(scaled.shape, dtype=np.float32) - rng.random(scaled.shape, dtype=np.float32)
    np.rint(scaled, out=scaled)
    np.clip(scaled, -32768, 32767, out=scaled)
    return scaled.astype("<i2")


def _stereo(block: np.ndarray) -> np.ndarray:
    return np.repeat(block, 2, axis=1) if block.shape[1] == 1 else block


def decode_to_pcm(
    source: str | Path, target: Path, *, check: Callable[[], None] | None = None, on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """Write the song as raw CD audio (44.1 kHz, 16-bit, stereo, little-endian) to `target`, padded with silence to whole sectors and to at
    least four seconds. Returns the number of sectors. A failure, or a stop from `check()`, leaves no file behind. `on_progress(done, total)`
    counts frames of the song as it is read."""
    sf = _library()
    song = inspect_song(source)
    if song.verdict == "cannot":
        raise BurnError(f"{Path(source).name}: {song.notes[0]}")
    if song.pending:
        raise BurnError(f"{Path(source).name}: this song has to be decoded by Windows first.")
    target = Path(target)
    written = 0
    try:
        with sf.SoundFile(str(source)) as reader, open(target, "wb") as out:
            exact = song.exact
            dtype = "int16" if exact else "float32"
            resampler = None if reader.samplerate == SAMPLE_RATE else _resampler(reader.samplerate)
            rng = np.random.default_rng(DITHER_SEED)
            total = max(reader.frames, 1)
            read_frames = 0

            def put(block: np.ndarray) -> None:
                nonlocal written
                pcm = block.astype("<i2", copy=False) if exact else _quantise(block, rng)
                out.write(np.ascontiguousarray(pcm).tobytes())
                written += len(pcm)

            def take() -> np.ndarray:
                nonlocal read_frames
                if check:
                    check()
                data = reader.read(CHUNK_FRAMES, dtype=dtype, always_2d=True)
                read_frames += len(data)
                if on_progress:
                    on_progress(min(read_frames, total), total)
                return data

            ahead = take()
            while True:
                following = take() if len(ahead) else ahead
                last = len(following) == 0
                block = _stereo(ahead)
                if resampler is not None:
                    block = resampler.resample_chunk(block, last=last)
                if len(block):
                    put(block)
                if last:
                    break
                ahead = following
            if written == 0:
                raise BurnError(f"{Path(source).name}: There is no sound in this file.")
            padding = sectors_for_frames(written) * FRAMES_PER_SECTOR - written
            if padding:
                out.write(bytes(padding * FRAME_BYTES))
        return sectors_for_frames(written)
    except (OSError, RuntimeError, ValueError) as exc:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        if isinstance(exc, OSError) and "space" in (exc.strerror or "").lower():
            raise BurnError("There is not enough room on the drive for the temporary copy of the music.") from None
        raise BurnError(f"{Path(source).name}: the song could not be converted ({str(exc).strip() or type(exc).__name__}).") from None
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------------------- will it fit?


@dataclass
class SongInput:
    """A song somebody wants on the disc, as the server resolved it."""

    path: str
    title: str = ""
    artist: str = ""
    track_id: int | None = None


@dataclass
class PlannedSong:
    position: int  # in the list the person made, from 1
    song: SongInput
    check: SongCheck

    @property
    def burnable(self) -> bool:
        return self.check.verdict != "cannot"


@dataclass
class AudioPlan:
    songs: list[PlannedSong]
    free_sectors: int | None  # on the disc in the drive; None when no usable disc was named
    scratch_free: int | None  # bytes free where the temporary copies go
    problems: list[str]  # reasons the burn cannot start at all
    gapless: bool = False  # the songs run on from one another (disc at once); otherwise two seconds of silence lie between them (song by song)

    @property
    def burnable(self) -> list[PlannedSong]:
        return [s for s in self.songs if s.burnable]

    @property
    def song_sectors(self) -> int:
        return sum(s.check.sectors for s in self.burnable)

    @property
    def disc_sectors(self) -> int:
        """What the songs take on the disc: their sectors, and (unless they run on from one another) two seconds of silence between one song and
        the next."""
        n = len(self.burnable)
        return self.song_sectors + (0 if self.gapless else GAP_SECTORS * max(n - 1, 0))

    @property
    def scratch_bytes(self) -> int:
        """The room the temporary copies take: every song as CD sound, and (one at a time) a song Windows decodes first, at worst as floating point."""
        decoded = max((s.check.sectors for s in self.burnable if s.check.via == "windows"), default=0)
        return self.song_sectors * SECTOR_BYTES + decoded * SECTOR_BYTES * 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "songs": [
                {
                    "position": s.position, "track_id": s.song.track_id, "title": s.song.title, "artist": s.song.artist, "verdict": s.check.verdict,
                    "notes": s.check.notes, "seconds": round(s.check.seconds, 1), "sectors": s.check.sectors,
                }
                for s in self.songs
            ],
            "counts": {verdict: sum(1 for s in self.songs if s.check.verdict == verdict) for verdict in ("ok", "converted", "cannot")},
            "disc_sectors": self.disc_sectors, "disc_seconds": round(self.disc_sectors / SECTORS_PER_SECOND, 1),
            "free_sectors": self.free_sectors, "free_seconds": None if self.free_sectors is None else round(self.free_sectors / SECTORS_PER_SECOND, 1),
            "scratch_bytes": self.scratch_bytes, "problems": self.problems, "ok": not self.problems, "gapless": self.gapless,
        }


def media_problems(media: dict[str, Any] | None) -> list[str]:
    """Why the disc in the drive cannot take a music CD, in words for the person; empty when it can."""
    if not media:
        return ["Choose a drive that has a blank CD-R or CD-RW in it."]
    if not media.get("writer"):
        return ["This drive cannot write discs."]
    state = media.get("state")
    if state == "none":
        return ["There is no disc in the drive."]
    kind = str(media.get("type") or "disc")
    if state == "unusable":
        return [f"This drive cannot use this {kind}: it may be damaged, dirty, or a kind of disc the drive does not write. Try another disc."]
    if state != "blank" or not media.get("blank"):
        if media.get("rewritable"):
            return [f"This {kind} already has something on it. Erase it first (Erase, on this screen), or put in a blank disc."]
        return [f"This {kind} is not blank, and a disc that has been written cannot be used again. Put in a blank CD-R."]
    if media.get("protected"):
        return [f"This {kind} is write-protected. Use another disc."]
    if not media.get("can_audio"):
        return [f"A music CD can only be made on a blank CD-R or CD-RW, and this is a {kind}."]
    return []


def fit_problem(disc_sectors: int, free_sectors: int) -> str | None:
    """Words for why songs that take `disc_sectors` of a disc do not fit in `free_sectors`, or None when they do."""
    over = disc_sectors + RESERVE_SECTORS - free_sectors
    if over <= 0:
        return None
    return (
        f"The songs take {_minutes(disc_sectors)} and the disc has {_minutes(free_sectors)} free. "
        f"Take out about {_span(over)} of music (a few seconds are kept in reserve)."
    )


def plan_audio(
    songs: list[SongInput], media: dict[str, Any] | None, scratch: Path, *, gapless: bool = False, inspector: Callable[[str], SongCheck] = inspect_song,
    windows: Callable[[list[str]], dict[str, SongCheck]] = check_with_windows,
) -> AudioPlan:
    """Which of these songs can go on a music CD, how much of the disc they take, and whether the burn can start. Touches nothing but the disc
    helper, which is asked once about the songs only Windows can decode (`windows`). Raises ValueError for a request that makes no sense (the
    message is fit for the person)."""
    if not songs:
        raise ValueError("Choose some songs first.")
    planned = [PlannedSong(position, song, inspector(song.path)) for position, song in enumerate(songs, start=1)]
    waiting = [p.check.path for p in planned if p.check.pending]
    if waiting:
        resolved = windows(waiting)
        for one in planned:
            if one.check.pending:
                one.check = resolved.get(one.check.path) or _cannot(Path(one.check.path), "Windows could not say what is in this song.", via="windows")
    free = _scratch_free(scratch)
    plan = AudioPlan(songs=planned, free_sectors=None, scratch_free=free, problems=[], gapless=gapless)
    problems = plan.problems
    problems.extend(media_problems(media))
    if gapless and media and not problems and not media.get("gapless"):
        problems.append(NO_GAPLESS)
    if media and not problems:
        plan.free_sectors = int(media.get("free_sectors") or 0)
    count = len(plan.burnable)
    if not count:
        problems.append("None of these songs can be burned to a music CD.")
        return plan
    if count > MAX_TRACKS:
        problems.append(f"A music CD holds at most {MAX_TRACKS} songs, and {count} were chosen. Take some out.")
    if plan.free_sectors is not None:
        tight = fit_problem(plan.disc_sectors, plan.free_sectors)
        if tight:
            problems.append(tight)
    if free is not None and free < plan.scratch_bytes + SCRATCH_MARGIN:
        problems.append(
            f"There is not enough room for the temporary copy of the music: about {_size(plan.scratch_bytes + SCRATCH_MARGIN)} are needed "
            f"and {_size(free)} are free."
        )
    return plan


def _scratch_free(folder: Path) -> int | None:
    here = Path(folder)
    while not here.exists() and here != here.parent:
        here = here.parent
    try:
        return shutil.disk_usage(here).free
    except OSError:
        return None


def forget_scratch(folder: Path, batch_id: str) -> None:
    """Remove a burn's temporary songs and decoded copies (and any a burn that was cut short left behind). Never raises."""
    with contextlib.suppress(OSError):
        for leftover in Path(folder).glob(f"{batch_id}-*"):
            with contextlib.suppress(OSError):
                leftover.unlink()
    with contextlib.suppress(OSError):
        os.rmdir(folder)  # (only if it is empty)
