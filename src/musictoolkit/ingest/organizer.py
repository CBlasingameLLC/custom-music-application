from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from musictoolkit.ingest import moves

logger = logging.getLogger("musictoolkit")

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_path_component(value: str) -> str:
    cleaned = _INVALID_CHARS.sub("_", value).replace(_BLANK, "").strip().rstrip(".")
    return cleaned or "Unknown"


_BLANK = "\ue000"  # stands in for an untagged number until the punctuation around it has been tidied
_LEADING_TRACK = re.compile(r"^\s*(\d{1,3})\s*[-._)]\s*(\S.*)$")  # "03 - Song", "03. Song", "03_Song"
_SEP = r"[\s\-\u2013_]"


class _Number(int):
    """A tag number that shows as nothing when the song has none, so "{track:02d} - {title}" gives "Song",
    not "00 - Song"."""

    def __format__(self, spec: str) -> str:
        return format(int(self), spec) if self else _BLANK


def _drop_blanks(part: str) -> str:
    """Remove the stand-ins for missing numbers together with the dashes and spaces that hung on them."""
    if _BLANK not in part:
        return part
    part = re.sub(rf"^{_BLANK}(?:\.(?=\s)|{_SEP})*", "", part)                 # "<none> - Song"
    part = re.sub(rf"{_SEP}*{_BLANK}(?=(?:\.[^.]*)?$)", "", part)                # "Album - <none>" (before an extension too)
    part = re.sub(rf"\s*[(\[]{_BLANK}[)\]]", "", part)                          # "Album (<none>)"
    part = re.sub(rf"({_SEP}+){_BLANK}{_SEP}+", r"\1", part)                     # "A - <none> - B"
    return part.replace(_BLANK, "")


# Windows refuses these as a file or folder name, with or without an extension (and with spaces before it).
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{n}" for n in "0123456789¹²³"), *(f"LPT{n}" for n in "0123456789¹²³"),
}


def _safe_relative(relative: str) -> str:
    """Make every folder and file name in a template result usable on Windows."""
    parts = []
    for part in re.split(r"[\\/]", relative):
        part = _drop_blanks(part)
        part = part.strip().rstrip(" .") if part not in (".", "..") else part
        if part.split(".")[0].strip().upper() in _RESERVED_NAMES:
            part = "_" + part
        parts.append(part)
    return "/".join(parts)


MAX_WINDOWS_PATH = 259

Progress = Callable[[int, int], None]


@dataclass
class MoveProposal:
    track_id: int
    old_path: Path
    new_path: Path


@dataclass
class OrganizeResult:
    proposals: list[MoveProposal]
    unchanged: int = 0
    collisions: list[tuple[Path, Path]] = field(default_factory=list)
    too_long: list[tuple[Path, Path]] = field(default_factory=list)
    unknown: int = 0  # proposed moves that land under "Unknown Artist" / "Unknown Album" because tags are missing


SCHEME_FIELDS = ("album_artist", "artist", "album", "title", "track", "disc", "year", "ext")


def _is_plain_relative(relative: str) -> bool:
    """False for rooted ("/x", "\\x"), drive-qualified ("C:x") and parent-escaping ("../x") paths.

    Judged by both path flavours, whatever the platform: "/x" has no drive letter, so Windows does not
    call it absolute, yet joined onto a folder it still lands at the drive root, outside the library."""
    windows = PureWindowsPath(relative)
    if windows.drive or windows.root or PurePosixPath(relative).is_absolute():
        return False
    return ".." not in windows.parts and ".." not in PurePosixPath(relative).parts


def validate_scheme(scheme: str) -> None:
    """Raise ValueError (with a message fit for the Settings page) if the template can't be filled in."""
    sample = {"album_artist": "A", "artist": "A", "album": "B", "title": "C", "track": 1, "disc": 1, "year": 2000, "ext": "mp3"}
    try:
        relative = scheme.format(**sample)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            f"Invalid template ({type(exc).__name__}: {exc}). Available fields: "
            + ", ".join("{" + name + "}" for name in SCHEME_FIELDS)
        ) from None
    if not scheme.strip() or not _is_plain_relative(relative):
        raise ValueError("The template must be a relative path like {album_artist}/{album}/{track:02d} - {title}.{ext}")
    if "{ext}" not in scheme:
        raise ValueError("The template must end with the file extension: {ext}")


def render_relative(row: sqlite3.Row, scheme: str, sanitize: Callable[[str], str] = sanitize_path_component) -> str:
    """The relative path (forward slashes) a song gets under a folder template, with every name made usable
    on Windows. `sanitize` cleans one tag value for use in a name; devices pass their own. Pure string work."""
    path = Path(row["file_path"])
    # A file named "03 - Song.mp3" with no tags still tells us its track number and title; use that rather
    # than renaming it "00 - 03 - Song.mp3" (and again on every later run).
    lead = _LEADING_TRACK.match(path.stem)
    track = row["track_number"] or (int(lead.group(1)) if lead else 0)
    title = row["title"] or (lead.group(2) if lead else path.stem)
    fields = {
        "album_artist": sanitize(row["album_artist"] or row["artist"] or "Unknown Artist"),
        "artist": sanitize(row["artist"] or "Unknown Artist"),
        "album": sanitize(row["album"] or "Unknown Album"),
        "title": sanitize(title),
        "track": _Number(track),
        "disc": row["disc_number"] or 1,
        "year": _Number(row["year"] or 0),
        "ext": path.suffix.lstrip(".").lower(),
    }
    relative = scheme.format(**fields)
    if not _is_plain_relative(relative):
        raise ValueError(f"template result {relative!r} is not a path inside the library")
    return _safe_relative(relative)


def _target(row: sqlite3.Row, library_root: Path, scheme: str) -> Path:
    """Where this track belongs under an already-resolved library folder. Pure string work: no disk access,
    so planning a very large library stays fast."""
    return Path(os.path.normpath(library_root / render_relative(row, scheme)))


def compute_target_path(row: sqlite3.Row, library_root: Path, scheme: str) -> Path:
    return _target(row, library_root.resolve(), scheme)


def propose_organization(
    conn: sqlite3.Connection, library_root: Path, scheme: str, on_progress: Progress | None = None
) -> OrganizeResult:
    library_root = library_root.resolve()
    result = OrganizeResult(proposals=[])
    rows = conn.execute("SELECT * FROM tracks WHERE is_missing = 0").fetchall()
    rows = [r for r in rows if Path(r["file_path"]).is_relative_to(library_root)]

    planned: set[str] = set()  # normcase'd targets already claimed, so two songs can't take one name
    listed = {  # every path the library knows, including songs whose files went missing: a path holds one row
        os.path.normcase(r["file_path"]): r["id"] for r in conn.execute("SELECT id, file_path FROM tracks")
    }
    for done, row in enumerate(rows):
        if on_progress and done % 250 == 0:
            on_progress(done, len(rows))
        old_path = Path(row["file_path"])
        try:
            new_path = _target(row, library_root, scheme)
        except (KeyError, ValueError, IndexError) as exc:
            logger.warning("Could not compute target path for %s: %s", old_path, exc)
            continue

        # A different spelling of the same path (artist/ vs Artist/) is the same place on a case-insensitive
        # disk. Renaming folders for letter case alone isn't worth the risk, so such songs count as in place.
        if os.path.normcase(str(new_path)) == os.path.normcase(str(old_path)):
            result.unchanged += 1
            continue
        if os.name == "nt" and len(str(new_path)) > MAX_WINDOWS_PATH:
            result.too_long.append((old_path, new_path))
            continue

        # On a case-insensitive disk "a.mp3" and "A.mp3" are one name: judge collisions the way the disk does.
        key = os.path.normcase(str(new_path))
        taken = os.path.lexists(new_path) and not moves.same_file(old_path, new_path)
        if key in planned or taken or listed.get(key, row["id"]) != row["id"]:
            result.collisions.append((old_path, new_path))
            continue

        planned.add(key)
        result.proposals.append(MoveProposal(track_id=row["id"], old_path=old_path, new_path=new_path))
        if not (row["album_artist"] or row["artist"]) or not row["album"]:
            result.unknown += 1

    if on_progress:
        on_progress(len(rows), len(rows))
    return result


def apply_with_log(
    conn: sqlite3.Connection,
    proposals: list[MoveProposal],
    library_root: Path | None,
    on_progress: Progress | None = None,
) -> moves.BatchResult:
    """Move the files, with lyrics and covers following, as one batch that can be undone. If `on_progress`
    raises (a cancelled job), what was moved so far stays logged and emptied folders are still tidied."""
    mover = moves.Mover(conn, "organize", library_root.resolve() if library_root else None, copy_art=True)
    try:
        for done, proposal in enumerate(proposals):
            if on_progress:
                on_progress(done, len(proposals))
            mover.move(proposal.track_id, proposal.old_path, proposal.new_path)
        if on_progress:
            on_progress(len(proposals), len(proposals))
    finally:
        result = mover.finish()
    return result


def apply_organization(conn: sqlite3.Connection, proposals: list[MoveProposal], library_root: Path | None = None) -> int:
    """Command-line entry point: returns how many files moved."""
    return apply_with_log(conn, proposals, library_root).moved
