"""Turning one song file into another: what is to be done with it (`decide`), and doing it (`convert_file`, `copy_file`).

A song that is lossless and goes to FLAC or WAV at the depth it has, and at the rate it has, passes through sample for sample (integers in,
integers out). Everything else goes through floating point: resampled with soxr when the rate changes, reduced to 16 bits with a little
dither when the depth does, and handed to the lossy encoders as floats (libsndfile 1.2's MP3 encoder distorts 16-bit stereo integers, as
`discs.encode` found). Nothing is written under the final name: the caller gives a temporary one and renames it when the file is whole.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from musictoolkit.convert import formats
from musictoolkit.convert.source import SourceInfo, library

CHUNK_SECONDS = 5  # decoded five seconds at a time
COPY_CHUNK = 1 << 20
DITHER_SEED = 0x4D544B  # the same song is always converted to the same bytes
RESAMPLE_QUALITY = "VHQ"
FIT_TOLERANCE = 1.05  # a lossy song averaging 5% over the bitrate asked for still counts as being at it
NO_ROOM = "There is not enough room on the drive for the converted songs."
LOW_ROOM = 1 << 20  # a drive with less than this left (1 MiB) that has just refused a write has run out of room
FILE_FORMATS = {"mp3": ("MP3", "MPEG_LAYER_III"), "ogg": ("OGG", "VORBIS"), "opus": ("OGG", "OPUS"), "flac": ("FLAC", None), "wav": ("WAV", None)}


class ConvertError(Exception):
    """The song could not be converted; the message says why, in words for the person."""

    for_the_person = True


@dataclass(frozen=True)
class Spec:
    format: str
    quality: str | None = None
    max_rate: int = 0  # 0 keeps the song's rate
    depth: str = "16"  # "16", or "keep" (24 bits for a song that has more than 16)
    existing: str = "copy"  # a song already in this format and as good as asked for: "copy" it as it is, "convert" it anyway, or "skip" it

    def checked(self) -> Spec:
        formats.check_choice(self.format, self.quality)
        formats.target_rate(self.format, 44100, self.max_rate)
        formats.target_bits(self.format, self.depth, 16)
        if self.existing not in formats.EXISTING:
            raise ValueError(f"existing songs must be one of: {', '.join(formats.EXISTING)}")
        return self


@dataclass
class Treatment:
    action: str  # copy | convert | skip | cannot
    rate: int = 0  # of the file that results
    channels: int = 0
    bits: int = 0  # for FLAC and WAV; 0 for the lossy formats
    notes: list[str] = field(default_factory=list)


@dataclass
class Converted:
    size: int
    rate: int
    channels: int
    frames: int  # written, at the new rate


def _khz(rate: int) -> str:
    return f"{rate / 1000:g} kHz"


def decide(info: SourceInfo, spec: Spec) -> Treatment:
    """What becomes of this song: copied as it is, converted, left out, or not possible, and what to tell the person about it."""
    if info.channels < 1 or info.channels > 2:
        return Treatment("cannot", notes=[f"This song has {info.channels} channels; Convert handles mono and stereo."])
    quality = formats.check_choice(spec.format, spec.quality)
    rate = formats.target_rate(spec.format, info.rate, spec.max_rate)
    bits = formats.target_bits(spec.format, spec.depth, info.bits)
    notes: list[str] = []
    if info.via == "windows":
        notes.append(f"Windows decodes this {info.family.upper()} song first.")
    if info.family == spec.format and not info.via:
        if info.lossy:
            fits = rate == info.rate and info.kbps <= quality.kbps * FIT_TOLERANCE
        else:
            fits = rate == info.rate and bits == info.bits
        if fits and spec.existing == "copy":
            return Treatment("copy", info.rate, info.channels, info.bits, ["Already in this format: copied as it is."])
        if fits and spec.existing == "skip":
            return Treatment("skip", info.rate, info.channels, info.bits, ["Already in this format: left out."])
    if rate != info.rate:
        notes.append(f"Resampled from {_khz(info.rate)} to {_khz(rate)}.")
    if bits and info.bits > bits:
        notes.append(f"Reduced from {info.bits} to {bits} bits (with a little dither).")
    if info.lossy and formats.FORMATS[spec.format].lossy:
        notes.append("A lossy song converted to another lossy format loses a little more quality.")
    elif info.lossy:
        notes.append("A lossy song does not get its quality back: the file only gets bigger.")
    return Treatment("convert", rate, info.channels, bits, notes)


def _open_writer(path: Path, spec: Spec, rate: int, channels: int, bits: int):
    sf = library()
    file_format, subtype = FILE_FORMATS[spec.format]
    quality = formats.check_choice(spec.format, spec.quality)
    options: dict[str, Any] = {}
    if spec.format == "mp3":
        options.update(bitrate_mode="CONSTANT", compression_level=quality.level)
    elif quality.level is not None:
        options.update(compression_level=quality.level)
    if subtype is None:
        subtype = "PCM_24" if bits == 24 else "PCM_16"
    try:
        return sf.SoundFile(str(path), "w", rate, channels, format=file_format, subtype=subtype, **options)
    except (OSError, RuntimeError) as exc:
        raise ConvertError(f"The converted file could not be created ({str(exc).strip() or type(exc).__name__}).") from None


def _exact_type(info: SourceInfo, spec: Spec, rate: int, bits: int) -> str | None:
    """"int16" or "int32" when the samples pass through untouched, else None."""
    if spec.format not in ("flac", "wav") or info.lossy or rate != info.rate:
        return None
    if bits == 16 and info.bits <= 16:
        return "int16"
    if bits == 24 and info.bits == 24:
        return "int32"  # (24-bit samples travel left-justified in 32 bits, and libsndfile drops the empty byte again)
    return None


def _quantise16(samples: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Floating point (full scale 1.0) to 16-bit integers: one least-significant bit of triangular noise added, rounded, and clipped rather than wrapped."""
    scaled = samples * 32768.0
    scaled += rng.random(scaled.shape, dtype=np.float32) - rng.random(scaled.shape, dtype=np.float32)
    np.rint(scaled, out=scaled)
    np.clip(scaled, -32768, 32767, out=scaled)
    return scaled.astype("<i2")


def _resampler(rate_in: int, rate_out: int, channels: int):
    try:
        import soxr
    except (ImportError, OSError) as exc:
        raise ConvertError(f"The audio library that converts sample rates could not be loaded ({exc}). Install Music Toolkit again.") from None
    return soxr.ResampleStream(rate_in, rate_out, channels, dtype="float32", quality=RESAMPLE_QUALITY)


def _not_onto_itself(source: str | Path, target: Path) -> None:
    """Writing a song onto the file it is being read from would destroy it."""
    try:
        same = os.path.samefile(source, target)
    except OSError:  # (the target is not there yet: it cannot be the source)
        same = False
    if same:
        raise ConvertError("A song cannot be converted onto itself.")


def no_room(exc: BaseException, folder: Path | None = None) -> bool:
    """Whether a failed write was the drive running full. A plain file write says so with its error number (28, ENOSPC). The audio library says
    only "System error." of any write the system refused (found on a drive that was really full), so for that the drive is looked at: how much
    is left in `folder`. The words of a message are no use for this: they often hold the song's own file name, and a folder called "Outer Space"
    is not a full drive."""
    if getattr(exc, "errno", None) == errno.ENOSPC or "no space left" in str(exc).lower():
        return True
    if folder is not None and "system error" in str(exc).lower():
        try:
            return shutil.disk_usage(folder).free < LOW_ROOM
        except OSError:
            return False
    return False


def convert_file(
    source: str | Path, target: Path, info: SourceInfo, spec: Spec, *,
    check: Callable[[], None] | None = None, on_progress: Callable[[int, int], None] | None = None,
) -> Converted:
    """Write `source` to `target` (a temporary name: the caller renames it) in the format of `spec`. A failure or a stop leaves no file behind.
    `on_progress(done, total)` counts frames of the song as they are read."""
    spec = spec.checked()
    treatment = decide(info, spec)
    if treatment.action == "cannot":
        raise ConvertError(treatment.notes[0])
    _not_onto_itself(source, target)
    rate, channels, bits = treatment.rate, info.channels, treatment.bits  # (for a song that would only be copied these are what it already has)
    sf = library()
    target = Path(target)
    exact = _exact_type(info, spec, rate, bits)
    lossy_target = formats.FORMATS[spec.format].lossy
    written = 0
    try:
        with sf.SoundFile(str(source)) as reader:
            total = max(reader.frames, 1)
            chunk = max(int(reader.samplerate * CHUNK_SECONDS), 1)
            resampler = _resampler(reader.samplerate, rate, channels) if rate != reader.samplerate else None
            rng = np.random.default_rng(DITHER_SEED)
            dtype = exact or "float32"
            read_frames = 0
            with _open_writer(target, spec, rate, channels, bits) as out:

                def take() -> np.ndarray:
                    nonlocal read_frames
                    if check:
                        check()
                    data = reader.read(chunk, dtype=dtype, always_2d=True)
                    read_frames += len(data)
                    if on_progress:
                        on_progress(min(read_frames, total), total)
                    return data

                def put(block: np.ndarray) -> None:
                    nonlocal written
                    if exact is None:
                        if lossy_target or bits == 24:
                            block = np.clip(block, -1.0, 1.0)
                        else:
                            block = _quantise16(block, rng)
                    out.write(block)
                    written += len(block)

                ahead = take()
                while True:
                    following = take() if len(ahead) else ahead
                    last = len(following) == 0
                    block = ahead
                    if resampler is not None:
                        block = resampler.resample_chunk(block, last=last)
                    if len(block):
                        put(block)
                    if last:
                        break
                    ahead = following
        if written == 0:
            raise ConvertError("There is no sound in this file.")
        return Converted(os.path.getsize(target), rate, channels, written)
    except ConvertError:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        full = no_room(exc, target.parent)  # (before what was half written is taken away: that gives the room back)
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        if full:
            raise ConvertError(NO_ROOM) from None
        raise ConvertError(f"The song could not be converted ({str(exc).strip() or type(exc).__name__}).") from None
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        raise


def copy_file(source: str | Path, target: Path, *, check: Callable[[], None] | None = None, on_progress: Callable[[int, int], None] | None = None) -> int:
    """Copy a file as it is, to a temporary name the caller renames. Returns its size. A failure or a stop leaves no file behind."""
    target = Path(target)
    _not_onto_itself(source, target)
    try:
        total = max(os.path.getsize(source), 1)
        done = 0
        with open(source, "rb") as src, open(target, "wb") as out:
            while True:
                if check:
                    check()
                block = src.read(COPY_CHUNK)
                if not block:
                    break
                out.write(block)
                done += len(block)
                if on_progress:
                    on_progress(min(done, total), total)
        return os.path.getsize(target)
    except OSError as exc:
        full = no_room(exc, target.parent)  # (before what was half written is taken away: that gives the room back)
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        if full:
            raise ConvertError(NO_ROOM) from None
        raise ConvertError(f"The song could not be copied ({exc.strerror or exc}).") from None
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        raise
