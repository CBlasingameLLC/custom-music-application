"""Lyrics from the file itself: embedded tags, or a .lrc file beside it."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import mutagen
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp4 import MP4

LRC_TIME = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
LRC_OFFSET = re.compile(r"^\[offset:\s*([+-]?\d+)\]\s*$", re.IGNORECASE | re.MULTILINE)


def parse_lrc(text: str) -> list[dict[str, Any]]:
    """[{'t': seconds, 'text': line}] sorted by time; empty if the text isn't LRC."""
    offset_match = LRC_OFFSET.search(text)
    offset = int(offset_match.group(1)) / 1000.0 if offset_match else 0.0
    lines: list[dict[str, Any]] = []
    for raw in text.splitlines():
        stamps = list(LRC_TIME.finditer(raw))
        if not stamps:
            continue
        content = raw[stamps[-1].end():].strip()
        for stamp in stamps:
            minutes, seconds, fraction = stamp.group(1), stamp.group(2), stamp.group(3) or "0"
            seconds_total = int(minutes) * 60 + int(seconds) + int(fraction) / (10 ** len(fraction))
            lines.append({"t": max(0.0, seconds_total + offset), "text": content})
    lines.sort(key=lambda item: item["t"])
    return lines


def _read_text(path: Path) -> str | None:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, OSError):
            continue
    return None


def _embedded_text(path: Path) -> str | None:
    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            frames = ID3(path).getall("USLT")
            return next((str(f.text) for f in frames if str(f.text).strip()), None)
        if suffix in (".m4a", ".mp4", ".m4b"):
            values = (MP4(path).tags or {}).get("\xa9lyr")
            return str(values[0]) if values else None
        audio = mutagen.File(path)
        if audio is not None and audio.tags:
            for key in ("lyrics", "unsyncedlyrics", "LYRICS", "UNSYNCEDLYRICS"):
                values = audio.tags.get(key)
                if values:
                    return str(values[0])
    except (ID3NoHeaderError, Exception):  # lyrics are optional; never fail playback over them
        return None
    return None


def lyrics_for(path: Path) -> dict[str, Any]:
    sidecar = path.with_suffix(".lrc")
    if sidecar.is_file():
        text = _read_text(sidecar)
        if text:
            synced = parse_lrc(text)
            return {
                "source": "lrc-file",
                "synced": synced or None,
                "plain": None if synced else text.strip(),
            }

    text = _embedded_text(path)
    if not text:
        return {"source": None, "synced": None, "plain": None}
    synced = parse_lrc(text)
    return {"source": "embedded", "synced": synced or None, "plain": None if synced else text.strip()}
