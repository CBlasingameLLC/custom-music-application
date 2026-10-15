"""Ripping a disc into the library: deciding where every song goes (and showing that first), reading, writing and tagging each one, adding it
to the library, keeping a history, and taking a rip back.

Nothing is overwritten, ever: a song whose file already exists is left out, and a file is written under a temporary name and renamed when it
is complete, so a cancelled or failed rip leaves no half-written song where a player could find it.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from musictoolkit.discs import encode, store
from musictoolkit.discs.encode import EncodeError
from musictoolkit.discs.helper import SECTOR_BYTES, DiscError, DiscHelper, in_use
from musictoolkit.discs.reader import ACCURACY, HelperSource, read_track
from musictoolkit.discs.tagging import SongTags, TagError, write_tags
from musictoolkit.discs.toc import SECTORS_PER_SECOND, Toc
from musictoolkit.ingest import cleanup, moves, organizer, scanner

logger = logging.getLogger("musictoolkit")

FLAC_RATIO = 0.6  # a FLAC file is roughly this much of the raw audio
MARGIN = 50 * 1024 * 1024  # room to spare, in bytes


# ---------------------------------------------------------------------------------------- what the person decided, and what it comes to


@dataclass
class TrackInput:
    number: int
    title: str
    artist: str = ""
    recording_id: str | None = None
    rip: bool = True


@dataclass
class RipRequest:
    drive: str
    root: str
    scheme: str
    format: str
    quality: str
    accuracy: str
    album: str
    album_artist: str
    tracks: list[TrackInput]
    year: int | None = None
    date: str = ""
    genre: str = ""
    disc_number: int = 1
    disc_total: int = 1
    release_id: str | None = None
    release_group_id: str | None = None
    artist_ids: list[str] = field(default_factory=list)
    barcode: str = ""
    label: str = ""
    cover: bool = True
    eject: bool = False


@dataclass
class PlannedTrack:
    number: int
    title: str
    artist: str
    recording_id: str | None
    start: int
    sectors: int
    path: Path | None
    action: str  # rip | skip (not chosen) | exists | clash | too_long | problem
    note: str = ""

    @property
    def seconds(self) -> float:
        return self.sectors / SECTORS_PER_SECOND


@dataclass
class RipPlan:
    root: Path
    folder: Path | None
    tracks: list[PlannedTrack]
    estimated_bytes: int
    free_bytes: int | None
    problems: list[str]  # reasons the rip cannot start at all

    @property
    def to_rip(self) -> list[PlannedTrack]:
        return [t for t in self.tracks if t.action == "rip"]

    def to_dict(self) -> dict[str, Any]:
        def shown(path: Path | None) -> str | None:
            if path is None:
                return None
            try:
                return path.relative_to(self.root).as_posix()
            except ValueError:
                return str(path)

        return {
            "root": str(self.root), "folder": shown(self.folder), "estimated_bytes": self.estimated_bytes, "free_bytes": self.free_bytes,
            "problems": self.problems, "ok": not self.problems,
            "tracks": [
                {"number": t.number, "title": t.title, "artist": t.artist, "seconds": round(t.seconds, 1), "action": t.action, "note": t.note, "path": shown(t.path)}
                for t in self.tracks
            ],
            "counts": {action: sum(1 for t in self.tracks if t.action == action) for action in ("rip", "skip", "exists", "clash", "too_long", "problem")},
        }


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _free(folder: Path) -> int | None:
    here = folder
    while not here.exists() and here != here.parent:
        here = here.parent
    try:
        return shutil.disk_usage(here).free
    except OSError:
        return None


def _size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def path_too_long(path: Path) -> bool:
    """Windows refuses a path of 260 characters or more, and the temporary name is a few longer than the final one."""
    return os.name == "nt" and len(str(path)) + len(".part") > organizer.MAX_WINDOWS_PATH


def estimate_bytes(fmt: str, quality: str, seconds: float) -> int:
    if fmt == "flac":
        return int(seconds * 44100 * 4 * FLAC_RATIO)
    return int(seconds * int(quality) * 1000 / 8)


def plan_rip(toc: Toc, request: RipRequest, library_roots: list[str], cache_dir: Path) -> RipPlan:
    """Where every song would go, and whether the rip can start. Touches nothing. Raises ValueError for a request that makes no sense
    (the message is fit for the person)."""
    audio = {t.number: t for t in toc.audio_tracks()}
    if not audio:
        raise ValueError("There is no music on this disc.")
    if sorted(t.number for t in request.tracks) != sorted(audio):
        raise ValueError("The list of songs does not match the disc in the drive.")
    encode.check_choice(request.format, request.quality)
    if request.accuracy not in ACCURACY:
        raise ValueError(f"accuracy must be one of: {', '.join(ACCURACY)}")
    organizer.validate_scheme(request.scheme)
    root = next((Path(r) for r in library_roots if _same(r, request.root)), None)
    if root is None:
        raise ValueError("That folder is not in your library.")

    problems: list[str] = []
    if not root.is_dir():
        problems.append("That library folder is not available right now. Is its drive connected?")
    planned: list[PlannedTrack] = []
    claimed: set[str] = set()
    for item in sorted(request.tracks, key=lambda t: t.number):
        track = audio[item.number]
        title = " ".join(item.title.split()) or f"Track {item.number:02d}"
        entry = PlannedTrack(item.number, title, item.artist or request.album_artist, item.recording_id, track.start, toc.sectors_of(track), None, "skip", "Left out.")
        planned.append(entry)
        if not item.rip:
            continue
        row = {
            "file_path": f"track.{request.format}", "track_number": item.number, "title": title, "album_artist": request.album_artist,
            "artist": entry.artist, "album": request.album or "Unknown Album", "disc_number": request.disc_number, "year": request.year,
        }
        try:
            relative = organizer.render_relative(row, request.scheme)
        except (KeyError, ValueError, IndexError) as exc:
            entry.action, entry.note = "problem", f"The layout cannot name this song ({exc})."
            continue
        entry.path = Path(os.path.normpath(root / relative))
        key = os.path.normcase(str(entry.path))
        if key in claimed:
            entry.action, entry.note = "clash", "Another song on this disc would get the same name. Give them different titles, or use a layout with the track number."
        elif path_too_long(entry.path):
            entry.action, entry.note = "too_long", "The name is too long for Windows. Shorten the title or use a shorter layout."
        elif os.path.lexists(entry.path):
            entry.action, entry.note = "exists", "A file with this name is already there. It is left alone."
        else:
            entry.action, entry.note = "rip", ""
            claimed.add(key)

    wanted = [t for t in planned if t.action == "rip"]
    estimate = sum(estimate_bytes(request.format, request.quality, t.seconds) for t in wanted)
    free = _free(root)
    if not wanted:
        problems.append("There is nothing to rip: every song is left out or already there.")
    else:
        if free is not None and free < estimate * 1.1 + MARGIN:
            problems.append(f"There is not enough room for these songs: about {_size(estimate)} are needed and {_size(free)} are free.")
        scratch = _free(cache_dir)
        largest = max(t.sectors for t in wanted) * SECTOR_BYTES
        if scratch is not None and scratch < largest + MARGIN:
            problems.append(f"There is not enough room for the temporary copy of the longest song: about {_size(largest)} are needed and {_size(scratch)} are free.")
    folder = wanted[0].path.parent if wanted and wanted[0].path else None
    return RipPlan(root=root, folder=folder, tracks=planned, estimated_bytes=estimate, free_bytes=free, problems=problems)


def begin(conn: sqlite3.Connection, plan: RipPlan, request: RipRequest, toc: Toc, batch_id: str) -> int:
    """Put the rip in the history, with every song as a row (the ones that will not be ripped say why)."""
    return store.create_rip(
        conn, batch_id=batch_id, disc_id=toc.disc_id(), release_id=request.release_id, album=request.album or "Unknown Album",
        artist=request.album_artist or "Unknown Artist", drive=request.drive, fmt=request.format, quality=request.quality, accuracy=request.accuracy,
        root=str(plan.root), folder=str(plan.folder) if plan.folder else None,
        tracks=[
            {"number": t.number, "title": t.title, "file_path": str(t.path) if t.path else None,
             "status": "waiting" if t.action == "rip" else "skipped", "message": t.note or None}
            for t in plan.tracks
        ],
    )


# ---------------------------------------------------------------------------------------- doing it


def _when(sector: int, start: int) -> str:
    seconds = int((sector - start) / SECTORS_PER_SECOND)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _describe(stats, start: int) -> str | None:
    bits = []
    if stats.rereads:
        bits.append(f"{_plural(stats.rereads, 'part')} had to be read again")
    if stats.suspect:
        bits.append(f"{_plural(len(stats.suspect), 'part')} could not be confirmed (around {', '.join(_when(s, start) for s in stats.suspect[:5])})")
    if stats.unreadable:
        bits.append(f"{_plural(len(stats.unreadable), 'part')} could not be read and are silent (around {', '.join(_when(s, start) for s in stats.unreadable[:5])})")
    text = "; ".join(bits)
    return text[0].upper() + text[1:] + "." if bits else None


def _tags(request: RipRequest, track: PlannedTrack, track_total: int) -> SongTags:
    return SongTags(
        title=track.title, artist=track.artist or request.album_artist, album=request.album or "Unknown Album", album_artist=request.album_artist,
        track_number=track.number, track_total=track_total, disc_number=request.disc_number, disc_total=request.disc_total, year=request.year,
        date=request.date, genre=request.genre, recording_id=track.recording_id, release_id=request.release_id,
        release_group_id=request.release_group_id, artist_ids=request.artist_ids, barcode=request.barcode, label=request.label,
    )


class _TrackFailed(Exception):
    """This song could not be ripped, but the next one can be tried."""


def run_rip(
    *, db: Callable[[], Any], plan: RipPlan, request: RipRequest, toc: Toc, batch_id: str, rip_id: int, cache_dir: Path, cover: bytes | None,
    handle: Any, track_total: int,
) -> dict[str, Any]:
    """The job: rip every song in the plan. `db` gives a connection as a context manager; `handle` reports progress and notices a cancel.
    A song that cannot be read is marked and the next one is tried; anything that stops the drive (the disc taken out, no answer) ends the
    rip, keeping what is done. Raises a DiscError/EncodeError/TagError written for the person when the rip ends without finishing."""
    from musictoolkit.web.jobs import JobCancelled  # noqa: PLC0415 - the job runner's own signal for a cancel

    tracks = plan.to_rip
    passes = 2 if request.accuracy == "verified" else 1
    overall = sum(t.sectors for t in tracks) * passes
    before = 0
    cache_dir.mkdir(parents=True, exist_ok=True)
    finished = failed = 0
    status, message = "done", None
    ejected = False
    problem: BaseException | None = None
    try:
        with in_use(), DiscHelper() as helper:
            current = Toc.from_reply(helper.call("toc", drive=request.drive))
            if current.disc_id() != toc.disc_id() or current.leadout != toc.leadout:
                raise DiscError("media_changed", "The disc in the drive is not the one this rip was planned for.")
            source = HelperSource(helper, request.drive)
            for index, track in enumerate(tracks, start=1):
                handle.check()
                label = f"Song {index} of {len(tracks)}: {track.title}"
                handle.update(done=before, total=overall, message=label)
                with db() as conn:
                    store.set_track(conn, rip_id, track.number, status="ripping")
                try:
                    _rip_one(source, db, plan, request, toc, track, batch_id, rip_id, cache_dir, cover, handle, label, before, overall, track_total)
                    finished += 1
                except _TrackFailed as failure:
                    failed += 1
                    with db() as conn:
                        store.set_track(conn, rip_id, track.number, status="failed", message=str(failure))
                    handle.log(f"{track.title}: {failure}")
                before += track.sectors * passes
            handle.update(done=overall, total=overall, message="Finishing")
            if cover and finished and plan.folder is not None:
                written = _write_folder_cover(plan.folder, cover)
                if written is not None:
                    info = written.stat()
                    with db() as conn:
                        store.set_cover(conn, rip_id, str(written), info.st_size, info.st_mtime)
            if request.eject and finished and not failed:
                with db() as conn:
                    problems = conn.execute("SELECT COALESCE(SUM(suspect + unreadable), 0) FROM disc_rip_tracks WHERE rip_id = ?", (rip_id,)).fetchone()[0]
                if not problems:  # a disc that had trouble stays in the drive, in case the person wants to try again
                    try:
                        helper.call("eject", drive=request.drive)
                        ejected = True
                    except DiscError as error:
                        handle.log(f"The disc could not be ejected: {error}")
    except JobCancelled:
        status, message = "cancelled", "Stopped."
        raise
    except (DiscError, EncodeError, TagError) as error:
        problem = error
        status, message = ("partial" if finished else "failed"), str(error)
    except Exception as error:  # a slip in this code: say what it was, and keep what is done
        logger.error("Rip %s failed", batch_id, exc_info=True)
        problem = error
        status, message = ("partial" if finished else "failed"), f"{type(error).__name__}: {error}"
    finally:
        with db() as conn:
            conn.execute(
                "UPDATE disc_rip_tracks SET status = 'failed', message = COALESCE(message, 'Stopped before it was finished.') WHERE rip_id = ? AND status = 'ripping'", (rip_id,)
            )
            conn.execute(
                "UPDATE disc_rip_tracks SET status = 'skipped', message = COALESCE(message, 'Not reached.') WHERE rip_id = ? AND status = 'waiting'", (rip_id,)
            )
            if status == "done" and (failed or not finished):
                status = "partial" if finished else "failed"
            store.finish_rip(conn, rip_id, status, message)
        for leftover in cache_dir.glob(f"{batch_id}-*.pcm"):
            leftover.unlink(missing_ok=True)
    if problem is not None:
        raise problem
    if status == "failed":
        raise _NothingRipped(f"None of the songs could be ripped ({failed} failed). See the history for why.")
    return {"rip_id": rip_id, "batch_id": batch_id, "ripped": finished, "failed": failed, "folder": str(plan.folder) if plan.folder else None, "status": status, "ejected": ejected}


class _NothingRipped(Exception):
    for_the_person = True


def _rip_one(
    source: HelperSource, db, plan: RipPlan, request: RipRequest, toc: Toc, track: PlannedTrack, batch_id: str, rip_id: int,
    cache_dir: Path, cover: bytes | None, handle: Any, label: str, before: int, overall: int, track_total: int,
):
    assert track.path is not None
    music_end = toc.end_of(toc.audio_tracks()[-1])  # (a data track after the music, as on a CD-Extra, cannot be read as audio)
    raw_path = cache_dir / f"{batch_id}-{track.number:02d}.pcm"
    part = track.path.with_name(track.path.name + ".part")

    def progress(done: int, total: int) -> None:
        handle.update(done=before + done, total=overall, message=f"{label} ({100 * done // max(total, 1)}%)")

    try:
        try:
            with open(raw_path, "w+b") as raw:
                stats = read_track(
                    source, track.start, track.sectors, raw, disc_sectors=music_end, accuracy=request.accuracy,
                    on_progress=progress, check=handle.check,
                )
                part.parent.mkdir(parents=True, exist_ok=True)
                encode.encode_pcm(raw, part, request.format, request.quality, check=handle.check)
        except DiscError as error:
            if error.code == "read_error":
                raise _TrackFailed(str(error)) from None
            raise
        write_tags(part, request.format, _tags(request, track, track_total), cover)
        if os.path.lexists(track.path):
            raise _TrackFailed("A file with this name appeared while the song was being ripped. It was left alone.")
        os.replace(part, track.path)
    finally:
        raw_path.unlink(missing_ok=True)
        part.unlink(missing_ok=True)  # nothing is left if the song did not get all the way
    info = track.path.stat()
    with db() as conn:
        outcome = scanner.add_files(conn, [str(track.path)])
        track_id = outcome[0][1] if outcome else None
        if track_id is not None:
            conn.execute(
                "UPDATE tracks SET musicbrainz_recording_id = ?, musicbrainz_release_id = ?, musicbrainz_artist_id = ? WHERE id = ?",
                (track.recording_id, request.release_id, request.artist_ids[0] if request.artist_ids else None, track_id),
            )
            conn.commit()
        store.set_track(
            conn, rip_id, track.number, size=info.st_size, mtime=info.st_mtime, track_id=track_id, rereads=stats.rereads,
            suspect=len(stats.suspect), unreadable=len(stats.unreadable), status="problem" if stats.problems else "done",
            message=_describe(stats, track.start),
        )
    return stats


def _write_folder_cover(folder: Path, cover: bytes) -> Path | None:
    """The album's picture beside its songs, for the players that look there, unless the folder already has one. Returns the file it made."""
    try:
        if any(moves.is_folder_art(entry.name) for entry in folder.iterdir()):
            return None
        part = folder / "cover.jpg.part"
        part.write_bytes(cover)
        os.replace(part, folder / "cover.jpg")
        return folder / "cover.jpg"
    except OSError as error:
        logger.warning("Could not write the cover next to the songs: %s", error)
        return None


# ---------------------------------------------------------------------------------------- taking a rip back


def undo_rip(
    conn: sqlite3.Connection, rip_id: int, roots: list[Path], on_progress: Callable[[int, int], None] | None = None,
    trash: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Send the songs of a rip to the Recycle Bin and forget them - those that are still exactly as they were written. A song that has
    been edited, retagged or replaced since is not the rip's any more and is left alone (and listed). Returns the counts."""
    if trash is None:
        from send2trash import send2trash as trash  # noqa: PLC0415 - only needed here, and keeps start-up light
    rip = store.get_rip(conn, rip_id)
    if rip is None:
        raise ValueError("There is no such rip.")
    if rip["undone_at"]:
        raise ValueError("That rip has already been taken back.")
    songs = [t for t in rip["tracks"] if t["status"] in ("done", "problem") and t["file_path"]]
    result: dict[str, Any] = {"trashed": 0, "gone": 0, "changed": [], "failed": [], "cover": False}
    folders: set[Path] = set()
    forgotten: list[int] = []
    for done, song in enumerate(songs):
        if on_progress:
            on_progress(done, len(songs))
        path = Path(song["file_path"])
        row = conn.execute("SELECT id FROM tracks WHERE id = ?", (song["track_id"],)).fetchone() if song["track_id"] else None
        if row is None:  # (the folder may have been scanned under another spelling of the path since)
            row = conn.execute("SELECT id FROM tracks WHERE file_path = ?" + (" COLLATE NOCASE" if os.name == "nt" else ""), (str(path),)).fetchone()
        try:
            info = path.stat()
        except FileNotFoundError:
            result["gone"] += 1
            if row is not None:
                forgotten.append(row["id"])
            continue
        if song["size"] is None or info.st_size != song["size"] or abs(info.st_mtime - (song["mtime"] or 0)) > 0.001:
            result["changed"].append(str(path))
            continue
        try:
            trash(str(path))
        except Exception as exc:  # send2trash raises OSError subclasses, but a COM failure can be anything
            logger.warning("Could not move %s to the Recycle Bin: %s", path, exc)
            result["failed"].append({"path": str(path), "reason": str(exc)})
            continue
        result["trashed"] += 1
        folders.add(path.parent)
        if row is not None:
            forgotten.append(row["id"])
    if forgotten:
        cleanup.forget_tracks(conn, forgotten)
    cover = Path(rip["cover_path"]) if rip["cover_path"] else None
    if cover is not None and cover.is_file():  # the picture this rip made goes too, unless it has been replaced since
        info = cover.stat()
        if info.st_size == rip["cover_size"] and abs(info.st_mtime - (rip["cover_mtime"] or 0)) <= 0.001:
            try:
                trash(str(cover))
                result["cover"] = True
            except Exception as exc:
                logger.warning("Could not move %s to the Recycle Bin: %s", cover, exc)
    root = Path(rip["root"])
    for folder in folders:
        moves.prune_empty_folders(folder, root, allow_art=False)  # a picture that was there before the rip is not ours to remove
    if not result["changed"] and not result["failed"]:
        store.mark_undone(conn, rip_id)
    if on_progress:
        on_progress(len(songs), len(songs))
    return result


def new_batch_id() -> str:
    return uuid.uuid4().hex[:12]
