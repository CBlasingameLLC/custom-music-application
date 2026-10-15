"""Writing files onto a data disc, or into an ISO image file: which files go, what they are called on the disc, and whether they fit.

The names are where it goes wrong. ISO 9660 and Joliet (which every CD reader, car stereo and computer understands) and UDF (DVDs and
bigger) each refuse different characters and lengths, so a name is made safe once, here, for all of them, and two files that would end
up with the same name on the disc are told apart. Nothing in here touches a drive; the files are only looked at.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

SECTOR = 2048  # a data disc is made of sectors of this many bytes
JOLIET_NAME = 64  # characters in one name (the Joliet limit)
JOLIET_PATH = 240  # characters in a whole path on a disc
MAX_DEPTH = 8  # folders inside folders (the ISO 9660 limit)
LABEL_LENGTH = 16  # the Joliet limit for the name of the disc
FOUR_GIB = 4 * 1024**3
RESERVE_SECTORS = 4096  # 8 MB kept back: the file system itself, and the recorder's own count of free sectors is not exact
LAYOUTS = ("library", "flat")
FILESYSTEMS = ("iso9660", "joliet", "udf")
CD_TYPES = ("CD-R", "CD-RW")
BLOCK = 1024 * 1024
_FORBIDDEN = re.compile(r'[<>:"/\\|?*;\x00-\x1f]')
_TRACK_NUMBER = re.compile(r"^\s*\d{1,3}\s*[-._)]\s*")  # "01 - ", "01. ", "01-": the number a file name starts with when it has no title to go by
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class DataError(Exception):
    """The files could not be put on a disc; the message says why, in words for the person."""

    for_the_person = True


def default_filesystems(disc_type: str | None) -> list[str]:
    """What a disc of this kind is given: ISO 9660 and Joliet for a CD (which every reader can open), and UDF as well for the bigger discs."""
    return ["iso9660", "joliet"] if not disc_type or disc_type in CD_TYPES else ["iso9660", "joliet", "udf"]


def clean_label(text: str, default: str = "MUSIC") -> str:
    """The name of the disc as people see it: no characters a disc refuses, no more than a disc can hold (16)."""
    text = unicodedata.normalize("NFC", str(text or ""))
    text = " ".join(_FORBIDDEN.sub(" ", text).split())[:LABEL_LENGTH].strip()
    return text or default


def iso_label(label: str) -> str:
    """The same name for ISO 9660, which only has capital letters, digits and the underscore."""
    ascii_only = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii")
    return (re.sub(r"[^A-Z0-9_]", "_", ascii_only.upper()).strip("_") or "MUSIC")[:32]


def safe_name(name: str, limit: int = JOLIET_NAME) -> str:
    """One file or folder name that every disc format takes: no forbidden characters, no trailing dots or spaces, no name Windows keeps for
    itself, no longer than `limit` characters (the extension is kept when the rest has to be cut)."""
    name = unicodedata.normalize("NFC", str(name or ""))
    name = " ".join(_FORBIDDEN.sub("_", name).split()).rstrip(" .")
    stem, dot, extension = name.rpartition(".")
    if not dot or not stem or len(extension) > 8:
        stem, extension = name, ""
    elif extension:
        extension = "." + extension
    if stem.upper() in _RESERVED:
        stem = "_" + stem
    room = max(limit - len(extension), 1)
    stem = stem[:room].rstrip(" .")
    return (stem or "_") + extension


def _unique(names: set[str], name: str) -> str:
    """`name`, or "name (2)", "name (3)" ... when a name that differs only in capitals is taken already (a disc does not tell them apart)."""
    candidate, number = name, 1
    stem, dot, extension = name.rpartition(".")
    if not dot or not stem:
        stem, extension = name, ""
    else:
        extension = "." + extension
    while candidate.casefold() in names:
        number += 1
        suffix = f" ({number})"
        candidate = stem[: max(JOLIET_NAME - len(extension) - len(suffix), 1)].rstrip(" .") + suffix + extension
    names.add(candidate.casefold())
    return candidate


# ---------------------------------------------------------------------------------------- what goes where


@dataclass
class DataSong:
    """A library song somebody wants on the disc, as the server resolved it."""

    path: str
    title: str = ""
    artist: str = ""
    track_id: int | None = None
    root: str = ""  # the library folder it lives in: where its place in the library starts


@dataclass
class DataFile:
    position: int  # in the list the person made, from 1
    song: DataSong
    dest: str | None = None  # where it goes on the disc ("Artist/Album/01 - Title.mp3"); None when it cannot go
    size: int = 0
    problem: str = ""

    @property
    def sectors(self) -> int:
        return -(-self.size // SECTOR)

    @property
    def goes(self) -> bool:
        return self.dest is not None


@dataclass
class DataPlan:
    files: list[DataFile]
    label: str
    layout: str
    filesystems: list[str]
    free_sectors: int | None  # on the disc in the drive; None for an image file, which has no limit but the disk it is written to
    problems: list[str]  # reasons it cannot start at all
    warnings: list[str] = field(default_factory=list)

    @property
    def going(self) -> list[DataFile]:
        return [f for f in self.files if f.goes]

    @property
    def folders(self) -> int:
        return len({p for f in self.going for p in PurePosixPath(f.dest).parents if str(p) != "."})  # type: ignore[arg-type]

    @property
    def disc_sectors(self) -> int:
        """What the files take on the disc: their sectors, and a few more for each file and folder (their entries in every file system)."""
        return sum(f.sectors for f in self.going) + 3 * len(self.going) + 3 * self.folders

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.going)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [
                {"position": f.position, "track_id": f.song.track_id, "title": f.song.title, "artist": f.song.artist, "dest": f.dest, "size": f.size, "problem": f.problem}
                for f in self.files
            ],
            "label": self.label, "label_iso": iso_label(self.label), "layout": self.layout, "filesystems": self.filesystems,
            "counts": {"going": len(self.going), "left_out": len(self.files) - len(self.going)},
            "bytes": self.total_bytes, "disc_sectors": self.disc_sectors, "free_sectors": self.free_sectors,
            "problems": self.problems, "warnings": self.warnings, "ok": not self.problems,
        }


def _place(song: DataSong, position: int, total: int, layout: str) -> list[str]:
    """The folders and the name of this song on the disc, in a form every disc format takes."""
    path = Path(song.path)
    extension = path.suffix.lower()
    if layout == "flat":
        width = max(2, len(str(total)))
        title = (song.title or _TRACK_NUMBER.sub("", path.stem)).strip()  # (a song without a title: its file name, without the number it begins with)
        return [safe_name(f"{position:0{width}d} - {title}{extension}")]
    try:
        relative = path.relative_to(song.root) if song.root else Path(path.name)
    except ValueError:
        relative = Path(path.name)  # (a song outside the library folder it was said to be in: just its name)
    parts = [safe_name(p, JOLIET_NAME) for p in relative.parts if p not in ("", ".")]
    return parts or [safe_name(path.name)]


def plan_data(
    songs: list[DataSong], *, layout: str = "library", label: str = "", filesystems: list[str] | None = None, media: dict[str, Any] | None = None,
    stat: Callable[[str], int | None] | None = None,
) -> DataPlan:
    """Where every song would go on the disc and whether it fits. `media` is what the recorder said about the disc (or None for an image
    file). Touches nothing. Raises ValueError for a request that makes no sense (the message is fit for the person)."""
    if layout not in LAYOUTS:
        raise ValueError(f"layout must be one of: {', '.join(LAYOUTS)}")
    if not songs:
        raise ValueError("Choose some songs first.")
    chosen = filesystems or default_filesystems(str((media or {}).get("type") or ""))
    if any(f not in FILESYSTEMS for f in chosen):
        raise ValueError(f"file systems must be some of: {', '.join(FILESYSTEMS)}")
    measure = stat or _size_of
    files: list[DataFile] = []
    taken: dict[str, set[str]] = {}
    for position, song in enumerate(songs, start=1):
        entry = DataFile(position, song)
        files.append(entry)
        size = measure(song.path)
        if size is None:
            entry.problem = "The file is not there any more."
            continue
        entry.size = size
        parts = _place(song, position, len(songs), layout)
        if len(parts) - 1 > MAX_DEPTH:
            entry.problem = f"The folders are nested too deep for a disc ({MAX_DEPTH} levels at most)."
            continue
        if len("/".join(parts)) > JOLIET_PATH:
            parts = [safe_name(p, 32) for p in parts[:-1]] + [parts[-1]]
        if len("/".join(parts)) > JOLIET_PATH:
            entry.problem = "The name is too long for a disc."
            continue
        if size >= FOUR_GIB and "udf" not in chosen:
            entry.problem = "The file is bigger than 4 GB, which only a UDF disc can hold."
            continue
        folder = "/".join(parts[:-1])
        parts[-1] = _unique(taken.setdefault(folder.casefold(), set()), parts[-1])
        entry.dest = "/".join(parts)
    plan = DataPlan(files=files, label=clean_label(label), layout=layout, filesystems=list(chosen), free_sectors=None, problems=[])
    if not plan.going:
        plan.problems.append("None of these files can be put on a disc.")
        return plan
    left_out = len(plan.files) - len(plan.going)
    if left_out:
        plan.warnings.append(f"{left_out} file{'s' if left_out != 1 else ''} will be left out: see the list.")
    if media is not None:
        plan.problems.extend(media_problems(media))
        if not plan.problems:
            plan.free_sectors = int(media.get("free_sectors") or 0)
            if plan.disc_sectors + RESERVE_SECTORS > plan.free_sectors:
                over = plan.disc_sectors + RESERVE_SECTORS - plan.free_sectors
                plan.problems.append(
                    f"The files take {megabytes(plan.disc_sectors * SECTOR)} and the disc has {megabytes(plan.free_sectors * SECTOR)} free. "
                    f"Take out about {megabytes(over * SECTOR)} (a little is kept in reserve)."
                )
    return plan


def _size_of(path: str) -> int | None:
    try:
        target = Path(path)
        return target.stat().st_size if target.is_file() else None
    except OSError:
        return None


def megabytes(n: float) -> str:
    return f"{n / 1024 / 1024:.0f} MB" if n < 1024**3 else f"{n / 1024**3:.1f} GB"


def media_problems(media: dict[str, Any]) -> list[str]:
    """Why the disc in the drive cannot take files, in words for the person; empty when it can."""
    if not media.get("writer"):
        return ["This drive cannot write discs."]
    if media.get("state") == "none":
        return ["There is no disc in the drive."]
    kind = str(media.get("type") or "disc")
    if media.get("state") == "unusable":
        return [f"This drive cannot use this {kind}: it may be damaged, dirty, or a kind of disc the drive does not write. Try another disc."]
    if media.get("state") != "blank" or not media.get("blank"):
        if media.get("rewritable"):
            return [f"This {kind} already has something on it. Erase it first (Erase, on this screen), or put in a blank disc."]
        return [f"This {kind} is not blank, and a disc that has been written cannot be used again. Put in a blank disc."]
    if media.get("protected"):
        return [f"This {kind} is write-protected. Use another disc."]
    return []


# ---------------------------------------------------------------------------------------- reading it back


@dataclass
class Verdict:
    checked: int = 0
    missing: list[str] = field(default_factory=list)  # files the disc does not show
    different: list[str] = field(default_factory=list)  # files that read back otherwise than they were written

    @property
    def ok(self) -> bool:
        return not self.missing and not self.different

    def words(self) -> str:
        if self.ok:
            return f"All {self.checked} files were read back from the disc and match."
        bad = [*(f"{n} is not on the disc" for n in self.missing[:3]), *(f"{n} does not match" for n in self.different[:3])]
        more = len(self.missing) + len(self.different) - len(bad)
        return "The disc was written, but it cannot be trusted: " + "; ".join(bad) + (f" (and {more} more)" if more > 0 else "") + "."


def _digest(path: Path, check: Callable[[], None] | None) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            if check:
                check()
            block = handle.read(BLOCK)
            if not block:
                return sha.hexdigest()
            sha.update(block)


def verify_files(
    plan: DataPlan, root: Path, *, check: Callable[[], None] | None = None, on_progress: Callable[[int, int], None] | None = None,
) -> Verdict:
    """Read every file back from the disc (mounted at `root`) and compare it with the file it was made from, by checksum. Slow on purpose:
    the point is that the disc, not the memory of the drive, is what is compared. `on_progress(done, total)` counts bytes."""
    verdict = Verdict()
    total, done = plan.total_bytes, 0
    for entry in plan.going:
        target = Path(root) / str(entry.dest)
        verdict.checked += 1
        try:
            on_the_disc = _digest(target, check)  # (an error: the disc does not show the file, or cannot read it)
        except OSError:
            verdict.missing.append(str(entry.dest))
        else:
            if on_the_disc != _digest(Path(entry.song.path), check):
                verdict.different.append(str(entry.dest))
        done += entry.size
        if on_progress:
            on_progress(done, max(total, 1))
    return verdict


def iso_target(folder: Path, label: str) -> Path:
    """The ISO file to write in `folder`: named after the disc, and never one that exists already (it gets " (2)", " (3)" ...)."""
    base = safe_name(label or "MUSIC", 48)
    candidate, number = Path(folder) / f"{base}.iso", 1
    while candidate.exists():
        number += 1
        candidate = Path(folder) / f"{base} ({number}).iso"
    return candidate
