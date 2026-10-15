"""Writing the audio of a disc as FLAC (lossless) or MP3, through libsndfile (the `soundfile` package).

MP3 is written at a constant bitrate. libsndfile's MP3 files carry the header that gives players and this app the right length; its
"variable" setting does not behave like LAME's V0 and V2 (everything up to a fairly low quality comes out at 320 kbps), so it is not offered.
The compression level that gives each bitrate was measured, and the tests check the result.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO, Callable

import numpy as np

from musictoolkit.discs.helper import SECTOR_BYTES

FORMATS = ("flac", "mp3")
EXTENSIONS = {"flac": "flac", "mp3": "mp3"}
FLAC_LEVEL = 0.625  # libsndfile's 0 to 1 scale for FLAC's 0 to 8: 5, the usual choice
MP3_LEVELS = {"320": 0.05, "256": 0.19, "192": 0.44, "128": 0.65}  # compression level -> constant bitrate in kbps, from the middle of each measured band
QUALITIES = {"flac": ("standard",), "mp3": tuple(MP3_LEVELS)}
DEFAULT_QUALITY = {"flac": "standard", "mp3": "256"}
SAMPLE_RATE = 44100
CHANNELS = 2
CHUNK_BYTES = SECTOR_BYTES * 256  # about 600 KB at a time


class EncodeError(Exception):
    """The file could not be written; the message says why, in words for the person."""

    for_the_person = True


def check_choice(fmt: str, quality: str) -> None:
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of: {', '.join(FORMATS)}")
    if quality not in QUALITIES[fmt]:
        raise ValueError(f"quality for {fmt} must be one of: {', '.join(QUALITIES[fmt])}")


def _library():
    try:
        import soundfile
    except (ImportError, OSError) as exc:
        raise EncodeError(f"The audio library that writes FLAC and MP3 files could not be loaded ({exc}). Install Music Toolkit again.") from None
    return soundfile


class Encoder:
    """Writes 16-bit stereo 44.1 kHz audio (as it comes off a CD) to `path` as it arrives. The caller chooses the name - a temporary one,
    renamed when the file is complete."""

    def __init__(self, path: Path, fmt: str, quality: str | None = None) -> None:
        quality = quality or DEFAULT_QUALITY[fmt]
        check_choice(fmt, quality)
        sf = _library()
        self.path = Path(path)
        self._as_float = fmt == "mp3"
        try:
            if fmt == "flac":
                self._file = sf.SoundFile(str(path), "w", SAMPLE_RATE, CHANNELS, format="FLAC", subtype="PCM_16", compression_level=FLAC_LEVEL)
            else:
                self._file = sf.SoundFile(
                    str(path), "w", SAMPLE_RATE, CHANNELS, format="MP3", subtype="MPEG_LAYER_III",
                    bitrate_mode="CONSTANT", compression_level=MP3_LEVELS[quality],
                )
        except (OSError, RuntimeError) as exc:
            raise EncodeError(f"The file could not be created ({exc}).") from None

    def write(self, pcm: bytes) -> None:
        if len(pcm) % 4:
            raise ValueError("audio must come in whole stereo frames of 4 bytes")
        samples = np.frombuffer(pcm, dtype="<i2").reshape(-1, CHANNELS)
        if self._as_float:
            # libsndfile 1.2's MP3 encoder distorts stereo 16-bit integers (found by comparing what comes back with what went in: a
            # correlation of 0.47 for a plain sine); floating point is right, and a 16-bit value is exact as a float32 once divided by 32768.
            samples = samples.astype(np.float32) / 32768.0
        try:
            self._file.write(samples)
        except (OSError, RuntimeError) as exc:
            if "space" in str(exc).lower():
                raise EncodeError("There is not enough room on the drive for the music.") from None
            raise EncodeError(f"The file could not be written ({exc}).") from None

    def close(self) -> None:
        try:
            self._file.close()
        except (OSError, RuntimeError) as exc:
            raise EncodeError(f"The file could not be finished ({exc}).") from None

    def __enter__(self) -> "Encoder":
        return self

    def __exit__(self, exc_type, *_rest: object) -> None:
        if exc_type is None:
            self.close()
        else:
            try:
                self._file.close()
            except (OSError, RuntimeError):
                pass


def encode_pcm(source: BinaryIO, destination: Path, fmt: str, quality: str | None = None, *, check: Callable[[], None] | None = None) -> int:
    """Everything in `source` (raw 16-bit stereo audio, from its start) written to `destination`. Returns the size of the file. A failure
    leaves nothing behind. `check()` is called between chunks and may raise to stop."""
    source.seek(0)
    try:
        with Encoder(destination, fmt, quality) as encoder:
            while True:
                if check:
                    check()
                chunk = source.read(CHUNK_BYTES)
                if not chunk:
                    break
                encoder.write(chunk)
        return os.path.getsize(destination)
    except BaseException:
        try:
            Path(destination).unlink(missing_ok=True)
        except OSError:
            pass
        raise
