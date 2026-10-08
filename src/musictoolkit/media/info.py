"""Per-file technical details for the player (ReplayGain, format, size)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import mutagen
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp4 import MP4

_GAIN = re.compile(r"([+-]?\d+(?:\.\d+)?)")


def _parse_db(value: object) -> float | None:
    match = _GAIN.search(str(value))
    return float(match.group(1)) if match else None


def replaygain(path: Path) -> dict[str, float | None]:
    out: dict[str, float | None] = {"track_db": None, "album_db": None}
    wanted = {"replaygain_track_gain": "track_db", "replaygain_album_gain": "album_db"}
    try:
        suffix = path.suffix.lower()
        if suffix == ".mp3":
            for frame in ID3(path).getall("TXXX"):
                key = wanted.get(str(frame.desc).lower())
                if key and frame.text:
                    out[key] = _parse_db(frame.text[0])
        elif suffix in (".m4a", ".mp4", ".m4b"):
            for tag, value in (MP4(path).tags or {}).items():
                key = wanted.get(tag.split(":")[-1].lower())
                if key and value:
                    raw = value[0]
                    out[key] = _parse_db(raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw)
        else:
            audio = mutagen.File(path)
            if audio is not None and audio.tags:
                for tag, key in wanted.items():
                    values = audio.tags.get(tag) or audio.tags.get(tag.upper())
                    if values:
                        out[key] = _parse_db(values[0])
    except (ID3NoHeaderError, Exception):
        pass
    return out


def file_info(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"size": None, "sample_rate": None, "channels": None, "bitrate": None}
    try:
        info["size"] = path.stat().st_size
        audio = mutagen.File(path)
        if audio is not None and audio.info is not None:
            info["sample_rate"] = getattr(audio.info, "sample_rate", None)
            info["channels"] = getattr(audio.info, "channels", None)
            info["bitrate"] = getattr(audio.info, "bitrate", None)
    except Exception:
        pass
    return info
