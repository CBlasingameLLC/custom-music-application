"""Write tags into audio files (MP3, FLAC, M4A, Ogg, Opus) and put back what was there.

Everything goes through mutagen's "easy" interface, which names tags the same
way in every format (title, artist, albumartist, album, genre, date,
tracknumber, discnumber), so one code path serves all of them.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import mutagen
from mutagen.id3 import ID3
from mutagen.mp3 import MP3

logger = logging.getLogger("musictoolkit")

# tracks-table column -> easy tag key
EASY_KEYS = {
    "title": "title",
    "artist": "artist",
    "album_artist": "albumartist",
    "album": "album",
    "genre": "genre",
    "year": "date",
    "track_number": "tracknumber",
    "disc_number": "discnumber",
}
EDITABLE_FIELDS = tuple(EASY_KEYS)
TEXT_FIELDS = ("title", "artist", "album_artist", "album", "genre")
NUMBER_FIELDS = ("year", "track_number", "disc_number")
WRITABLE_SUFFIXES = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus"}
MAX_TEXT_LENGTH = 500


class TagWriteError(Exception):
    """A file's tags could not be written; the message is fit to show to the user."""


def can_write(path: Path | str) -> bool:
    return Path(path).suffix.lower() in WRITABLE_SUFFIXES


def clean_changes(changes: dict[str, Any]) -> dict[str, str | int | None]:
    """Validate a {column: value} edit. '' / None clear a field. Raises ValueError with a readable message."""
    cleaned: dict[str, str | int | None] = {}
    for field, value in changes.items():
        if field not in EASY_KEYS:
            raise ValueError(f"'{field}' can't be edited")
        if field in TEXT_FIELDS:
            text = "" if value is None else "".join(ch for ch in str(value) if ch >= " " or ch == "\t").strip()
            if len(text) > MAX_TEXT_LENGTH:
                raise ValueError(f"{field.replace('_', ' ')} is too long (max {MAX_TEXT_LENGTH} characters)")
            cleaned[field] = text or None
        else:
            if value is None or (isinstance(value, str) and not value.strip()):
                cleaned[field] = None
                continue
            try:
                number = int(str(value).strip())
            except ValueError:
                raise ValueError(f"{field.replace('_', ' ')} must be a whole number") from None
            low, high = (1, 9999) if field == "year" else (0, 9999)
            if not low <= number <= high:
                raise ValueError(f"{field.replace('_', ' ')} must be between {low} and {high}")
            cleaned[field] = number
    return cleaned


def _leading_int(raw: str | None) -> int | None:
    digits = ""
    for ch in raw or "":
        if not ch.isdigit():
            break
        digits += ch
    return int(digits) if digits else None


def _open(path: Path):
    if not path.exists():
        raise TagWriteError("The file is missing (is its drive connected?)")
    if not can_write(path):
        raise TagWriteError(f"Tags in {path.suffix.upper() or 'this kind of'} files can't be edited yet")
    if not os.access(path, os.W_OK):  # on Windows this is the file's read-only attribute
        raise TagWriteError("The file is read-only or in use by another program")
    try:
        audio = mutagen.File(path, easy=True)
    except Exception as exc:
        raise TagWriteError(f"The file could not be read ({exc})") from None
    if audio is None:
        raise TagWriteError("The file is not a recognised audio file")
    return audio


def _current(audio, key: str) -> str | None:
    values = audio.tags.get(key) if audio.tags is not None else None
    return str(values[0]) if values else None


def _id3_version(path: Path) -> int:
    """Keep ID3v2.4 where a file already has it; write v2.3 otherwise (older players and devices read it best)."""
    try:
        return 4 if ID3(path).version[1] >= 4 else 3
    except Exception:
        return 3


def _save(path: Path, audio) -> None:
    try:
        if isinstance(audio, MP3):
            audio.save(v2_version=_id3_version(path))
        else:
            audio.save()
    except PermissionError:
        raise TagWriteError("The file is read-only or in use by another program") from None
    except OSError as exc:
        raise TagWriteError(f"The file could not be written ({exc.strerror or exc})") from None
    except mutagen.MutagenError as exc:  # mutagen wraps the OSError of a file it cannot open for writing
        if exc.args and isinstance(exc.args[0], PermissionError):
            raise TagWriteError("The file is read-only or in use by another program") from None
        raise TagWriteError(f"The tags could not be saved ({exc})") from None
    except Exception as exc:
        raise TagWriteError(f"The tags could not be saved ({exc})") from None


def _plan(audio, changes: dict[str, str | int | None]) -> dict[str, str | None]:
    """Easy-tag values to write for these changes, leaving out anything already correct.

    Keeps the "/total" of "3/12" track numbers and the month and day of full dates
    when only the number or year is being edited."""
    wanted: dict[str, str | None] = {}
    for field, value in changes.items():
        key = EASY_KEYS[field]
        current = _current(audio, key)
        if field in TEXT_FIELDS:
            new = value if value is None else str(value)
        elif value is None:
            new = None
        elif field == "year":
            new = current if current and _leading_int(current[:4]) == value else f"{value:04d}"
        else:  # track / disc number
            total = current.split("/", 1)[1].strip() if current and "/" in current else ""
            same = current is not None and _leading_int(current) == value
            new = current if same else (f"{value}/{total}" if total else str(value))
        if new != current:
            wanted[key] = new
    return wanted


def _write(path: Path, audio, values: dict[str, str | None]) -> dict[str, str | None]:
    if audio.tags is None:
        audio.add_tags()
    before = {key: _current(audio, key) for key in values}
    for key, value in values.items():
        if value is None:
            if key in audio.tags:
                del audio.tags[key]
        else:
            audio.tags[key] = [value]
    _save(path, audio)
    return before


def apply_changes(path: Path, changes: dict[str, str | int | None]) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """Apply a validated {column: value} edit. Returns (before, after) as raw easy-tag values for just the
    tags that changed; both are empty (and the file untouched) when it already matched."""
    audio = _open(path)
    values = _plan(audio, changes)
    if not values:
        return {}, {}
    return _write(path, audio, values), values


def restore(path: Path, raw: dict[str, str | None]) -> None:
    """Put raw easy-tag values back exactly as they were (None removes the tag)."""
    audio = _open(path)
    _write(path, audio, raw)
