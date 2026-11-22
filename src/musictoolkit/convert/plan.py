"""What a conversion would do, song by song, worked out before anything is written.

Every song comes out as one of: convert (it will be written in the new format), copy (it already is in that format and as good as asked for: copied as
it is), skip (the same, and the person asked for such songs to be left out), exists (the file it would be is already there: never replaced), clash
(another song in the batch has that name), or cannot (with the reason in words). The names come from the same folder layouts as the library's and the
devices', with the new file extension.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

from musictoolkit.convert import engine, formats, store, windows
from musictoolkit.convert.engine import Spec
from musictoolkit.convert.source import (
    SourceError,
    SourceInfo,
    from_windows,
    inspect_source,
)
from musictoolkit.discs.burn import WINDOWS_DECODES
from musictoolkit.ingest import organizer

ACTIONS = ("convert", "copy", "skip", "cannot", "exists", "clash")
SPARE = 0.05  # room to spare over the estimate
SPARE_BYTES = 64 << 20
COVER_ALLOWANCE = 150 << 10  # about what a cover picture adds to a song that carries one
PAGE = 100


@dataclass
class PlannedSong:
    track_id: int | None
    source: str
    title: str
    artist: str
    action: str = "cannot"
    relative: str = ""  # under the destination, with forward slashes
    target: str = ""  # the whole path
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    bytes: int = 0  # about how big the file that will be written is
    info: SourceInfo | None = field(default=None, repr=False)

    @property
    def writes(self) -> bool:
        return self.action in ("convert", "copy")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.track_id, "title": self.title, "artist": self.artist, "action": self.action, "from": self.source, "to": self.relative,
            "notes": self.notes, "seconds": round(self.seconds, 1), "bytes": self.bytes,
        }


@dataclass
class ConvertPlan:
    spec: Spec
    scheme: str
    destination: str
    songs: list[PlannedSong]
    problems: list[str]
    free_bytes: int | None = None

    def count(self, action: str) -> int:
        return sum(1 for s in self.songs if s.action == action)

    @property
    def writing(self) -> list[PlannedSong]:
        return [s for s in self.songs if s.writes]

    @property
    def bytes(self) -> int:
        return sum(s.bytes for s in self.songs if s.writes)

    @property
    def seconds(self) -> float:
        return sum(s.seconds for s in self.songs if s.writes)

    @property
    def room_needed(self) -> int:
        return int(self.bytes * (1 + SPARE)) + SPARE_BYTES if self.bytes else 0

    @property
    def ok(self) -> bool:
        return not self.problems and bool(self.writing)

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "problems": self.problems, "destination": self.destination, "format": self.spec.format, "quality": self.spec.quality or formats.FORMATS[self.spec.format].default,
            "scheme": self.scheme, "songs": len(self.songs), "counts": {a: self.count(a) for a in ACTIONS}, "bytes": self.bytes, "seconds": round(self.seconds),
            "room_needed": self.room_needed, "free_bytes": self.free_bytes,
        }

    def page(self, action: str, offset: int = 0, limit: int = PAGE) -> dict[str, Any]:
        if action not in ACTIONS:
            raise ValueError(f"action must be one of: {', '.join(ACTIONS)}")
        chosen = [s for s in self.songs if s.action == action]
        offset, limit = max(0, offset), max(1, min(limit, 500))
        return {"action": action, "total": len(chosen), "items": [s.to_dict() for s in chosen[offset : offset + limit]]}


def size_text(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def inside(path: str, folder: str) -> bool:
    a, b = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def _existing_ancestor(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return None


def check_destination(raw: str, roots: Iterable[str]) -> tuple[Path | None, str | None]:
    """The folder the converted songs go in, or the reason it cannot be: it must be a whole path, outside the library folders (a converted copy
    inside them would turn up in the library as a second song), and not a file."""
    text = (raw or "").strip()
    if not text:
        return None, "Choose a folder for the converted songs."
    if text.startswith(("\\\\.\\", "\\\\?\\")):
        return None, "That is not a folder the converted songs can go in."
    if not os.path.isabs(text) or (sys.platform == "win32" and not PureWindowsPath(text).drive):
        return None, "Give the whole path of the folder, such as C:\\Music\\Converted."
    folder = Path(os.path.normpath(text))
    for root in roots:
        if root and inside(str(folder), root):
            return None, "Choose a folder outside your library folders: a converted copy inside one would show up there as a second song."
    if folder.exists() and not folder.is_dir():
        return None, "That is a file, not a folder."
    ancestor = _existing_ancestor(folder)
    if ancestor is None:
        return None, "That folder's drive is not there."
    if not ancestor.is_dir():
        return None, "A file is in the way of that folder."
    return folder, None


def _title(row: Any) -> str:
    return str(row["title"] or Path(str(row["file_path"])).stem)


def _named(row: Any, extension: str) -> dict[str, Any]:
    """The song's row as the layout engine reads it, with the extension of the new format."""
    return {**{key: row[key] for key in row.keys()}, "file_path": str(Path(str(row["file_path"])).with_suffix("." + extension))}


def build_plan(
    rows: Iterable[Any], spec: Spec, destination: str, scheme: str, roots: Iterable[str], *,
    inspector: Callable[[Path], SourceInfo] = inspect_source, ask_windows: Callable[[list[str]], dict[str, dict[str, Any]]] = windows.ask,
    progress: Callable[[int, int], None] | None = None, check: Callable[[], None] | None = None,
    free_bytes: Callable[[Path], int | None] | None = None, cover: bool = True,
) -> ConvertPlan:
    """Work out what becomes of each song (`rows` are anything indexed by the tracks table's column names). Raises ValueError for a choice that
    is not offered or a layout that cannot be filled in; everything else is in the plan, in words."""
    spec = spec.checked()
    organizer.validate_scheme(scheme)
    roots = [r for r in roots if r]
    folder, problem = check_destination(destination, roots)
    problems = [problem] if problem else []
    extension = formats.FORMATS[spec.format].extension
    rows = list(rows)
    songs: list[PlannedSong] = []
    waiting: list[int] = []
    for number, row in enumerate(rows, start=1):
        if check:
            check()
        song = PlannedSong(row["id"], str(row["file_path"]), _title(row), str(row["artist"] or ""))
        try:
            song.info = inspector(Path(song.source))
        except SourceError as error:
            if Path(song.source).suffix.lower().lstrip(".") in WINDOWS_DECODES:
                waiting.append(len(songs))
            else:
                song.notes = [str(error)]
        songs.append(song)
        if progress:
            progress(number, len(rows))
    if waiting:  # (asked once, for all of them)
        answers = ask_windows([songs[i].source for i in waiting])
        for i in waiting:
            answer = answers.get(songs[i].source) or {}
            if answer.get("ok"):
                songs[i].info = from_windows(songs[i].source, answer)
            else:
                songs[i].notes = [str(answer.get("error") or "Windows could not say what is in this song.")]
    claimed: dict[str, int] = {}
    for song, row in zip(songs, rows):
        if song.info is None:
            continue
        song.seconds = song.info.seconds
        treatment = engine.decide(song.info, spec)
        song.notes = list(treatment.notes)
        if treatment.action == "cannot":
            continue
        if treatment.action == "skip":
            song.action = "skip"
            continue
        try:
            song.relative = organizer.render_relative(_named(row, extension), scheme)
        except (ValueError, KeyError, IndexError) as error:
            song.notes = [f"The layout does not give this song a place ({error})."]
            continue
        if folder is not None:
            song.target = os.path.normpath(os.path.join(str(folder), *song.relative.split("/")))
            limit = organizer.MAX_WINDOWS_PATH - len(store.PART_SUFFIX)  # (the song is written under a name a little longer than its own)
            if len(song.target) > limit:
                song.notes = [f"The name is too long for Windows ({len(song.target)} characters; at most {limit}, which leaves room for the temporary name the song is written under)."]
                continue
            if any(inside(song.target, root) for root in roots):
                song.notes = ["This would write into your library."]
                continue
            key = os.path.normcase(song.target)
            if key in claimed:
                song.action, song.notes = "clash", ["Another song in this batch has the same name: this one is left out."]
                continue
            claimed[key] = len(claimed)
            if os.path.exists(song.target):
                song.action, song.notes = "exists", ["Already there: left as it is."]
                continue
        song.action = treatment.action
        if treatment.action == "copy":
            song.bytes = song.info.size
        else:
            song.bytes = formats.estimate_bytes(spec.format, spec.quality, song.info.seconds, treatment.channels, treatment.rate, treatment.bits) + (COVER_ALLOWANCE if cover else 0)
    plan = ConvertPlan(spec, scheme, str(folder) if folder else "", songs, problems)
    if folder is not None:
        ancestor = _existing_ancestor(folder)
        try:
            plan.free_bytes = free_bytes(folder) if free_bytes else (shutil.disk_usage(ancestor).free if ancestor else None)
        except OSError:
            plan.free_bytes = None
    if not plan.writing and not problems:
        plan.problems.append("There is nothing to convert: every song is already as asked for, left out, or cannot be converted.")
    if plan.free_bytes is not None and plan.writing and plan.room_needed > plan.free_bytes:
        plan.problems.append(f"There is not enough room: about {size_text(plan.room_needed)} is needed and {size_text(plan.free_bytes)} is free.")
    return plan
