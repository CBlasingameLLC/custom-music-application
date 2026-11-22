"""What a song file is, as far as converting it goes: its codec family, sample rate, channels, depth, length and (for a lossy one) bitrate."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PCM_BITS = {"PCM_S8": 8, "PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "PCM_32": 32, "FLOAT": 32, "DOUBLE": 64}


class SourceError(Exception):
    """The song cannot be read; the message says why, in words for the person."""

    for_the_person = True


@dataclass
class SourceInfo:
    path: str
    family: str  # mp3 | flac | ogg (Vorbis) | opus | wav | other (another kind of PCM) | m4a | wma | aac | mp4 (the ones Windows decodes)
    rate: int
    channels: int
    bits: int  # per sample, for a lossless song; 0 for a lossy one
    seconds: float
    lossy: bool
    kbps: int  # of a lossy song, from its size and length; 0 for a lossless one
    size: int
    via: str = ""  # "windows": only Windows' own decoders can read it

    @property
    def frames(self) -> int:
        return round(self.seconds * self.rate)


def library():
    try:
        import soundfile
    except (ImportError, OSError) as exc:
        raise SourceError(f"The audio library that reads songs could not be loaded ({exc}). Install Music Toolkit again.") from None
    return soundfile


def _family(file_format: str, subtype: str) -> tuple[str, bool]:
    """The codec family of a file libsndfile read, and whether it is lossy."""
    if file_format == "MP3":
        return "mp3", True
    if file_format == "OGG":
        return ("opus" if subtype == "OPUS" else "ogg"), True
    if file_format == "FLAC":
        return "flac", False
    if file_format in ("WAV", "WAVEX"):
        return ("mp3", True) if subtype == "MPEG_LAYER_III" else ("wav", False)  # (an MP3 stream inside a WAV is rare, but it is what it is)
    return "other", subtype not in PCM_BITS


def inspect_source(path: str | Path) -> SourceInfo:
    """Read what a song file is like. Raises SourceError, in words, for one that is missing, unreadable or silent."""
    sf = library()
    path = Path(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        raise SourceError("The file is not there any more.") from None
    try:
        info = sf.info(str(path))
    except (RuntimeError, OSError) as exc:
        raise SourceError(f"The song cannot be read ({str(exc).strip() or type(exc).__name__}).") from None
    if not info.frames or not info.samplerate:
        raise SourceError("There is no sound in this file.")
    family, lossy = _family(info.format, info.subtype)
    seconds = info.frames / info.samplerate
    return SourceInfo(
        str(path), family, int(info.samplerate), int(info.channels), 0 if lossy else PCM_BITS.get(info.subtype, 16),
        seconds, lossy, round(size * 8 / seconds / 1000) if lossy else 0, size,
    )


def from_windows(path: str | Path, answer: dict[str, Any], size: int = 0) -> SourceInfo:
    """What Windows says of a song only it can decode (the disc helper's `inspect` answer)."""
    path = Path(path)
    seconds = float(answer.get("seconds") or 0)
    if not size:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
    return SourceInfo(
        str(path), path.suffix.lower().lstrip("."), int(answer.get("rate") or 0), int(answer.get("channels") or 0), 0, seconds, True,
        round(size * 8 / seconds / 1000) if seconds > 0 and size else 0, size, via="windows",
    )
