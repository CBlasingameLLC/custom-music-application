"""The job that converts, song by song, and the job that takes a conversion back.

Each song is written to a temporary name beside its place (`<name>.part`) and renamed when it is whole, so a stop, a failure or a closed app never leaves
a half-written song under a real name; a file that is already there is never replaced. A song that fails is said so and the rest go on; running out of
room ends the whole batch (every song after it would fail the same way). Songs only Windows can decode are decoded to a temporary WAV first, by the same
helper the music CD burn uses, and that copy is removed as soon as the song is done.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from musictoolkit.convert import engine, store, windows
from musictoolkit.convert import plan as planning
from musictoolkit.convert.engine import ConvertError, Spec
from musictoolkit.convert.source import SourceError, inspect_source
from musictoolkit.convert.tags import read_tags
from musictoolkit.discs.helper import DiscError, DiscHelper, DiscUnavailable
from musictoolkit.discs.rip import _move_into_place as move_into_place
from musictoolkit.discs.rip import _patiently as patiently
from musictoolkit.discs.tagging import TagError, write_tags
from musictoolkit.media import art

MTIME_SLACK = 2.5  # seconds: a FAT drive keeps a file's time to the nearest two seconds


class ConvertProblem(Exception):
    """The conversion could not begin or could not go on; the message says why, in words for the person."""

    for_the_person = True


class _Skip(Exception):
    """A song that is left as it is."""


@dataclass(frozen=True)
class Request:
    spec: Spec
    destination: str
    scheme: str
    cover: bool = True  # carry the cover picture into the converted songs
    previewed: int = 0  # how many songs the preview said would be written


class _Progress:
    """The progress of the whole conversion, in milliseconds of music, and what to say of the song being done."""

    def __init__(self, handle: Any, total: int) -> None:
        self.handle, self.total, self.base = handle, max(total, 1), 0
        self.label, self.weight = "", 1

    def begin(self, label: str, weight: int) -> None:
        self.label, self.weight = label, max(weight, 1)
        self.handle.update(done=self.base, total=self.total, message=self.label)

    def part(self, from_half: int, halves: int) -> Callable[[int, int], None]:
        """Progress for a part of this song: from `from_half` halves of it done, for `halves` halves."""
        def report(done: int, total: int) -> None:
            total = max(total, 1)
            whole = from_half * total + halves * done
            self.handle.update(done=self.base + self.weight * whole // (2 * total), total=self.total, message=f"{self.label} ({100 * whole // (2 * total)}%)")
        return report

    def end(self) -> None:
        self.base += self.weight


def _tags_for(song: planning.PlannedSong) -> Any:
    tags = read_tags(song.source)
    tags.title = tags.title or song.title
    tags.artist = tags.artist or song.artist
    return tags


def _make_folders(folder: Path) -> list[Path]:
    """Make `folder`, and any above it that are missing. Returns the ones that this made, the innermost first."""
    made: list[Path] = []
    probe = folder
    while not probe.exists() and probe != probe.parent:
        made.append(probe)
        probe = probe.parent
    folder.mkdir(parents=True, exist_ok=True)
    return made


def _remove_folders(folders: list[Path]) -> None:
    """Take away the folders a song that did not get made had made for itself: only while they are empty, and never one that was there before."""
    for folder in folders:
        try:
            folder.rmdir()
        except OSError:
            return


def _write_song(song: planning.PlannedSong, request: Request, scratch: Path, batch_id: str, number: int, handle: Any, progress: _Progress, helper_now: Callable[[], DiscHelper]) -> None:
    """Put one song in its place. Raises _Skip for one that is left alone, anything else for one that failed (or was stopped): then nothing of it is left,
    not the temporary file and not the folders it made."""
    target = Path(song.target)
    if os.path.lexists(target):
        raise _Skip("Already there: left as it is.")
    made_folders = _make_folders(target.parent)
    part = target.with_name(target.name + store.PART_SUFFIX)
    decoded: Path | None = None
    try:
        part.unlink(missing_ok=True)  # (a leftover of an earlier run: it is replaced, never written through, which a link under this name would otherwise be)
        if song.action == "copy":
            engine.copy_file(song.source, part, check=handle.check, on_progress=progress.part(0, 2))
        else:
            info, source, converting = song.info, song.source, progress.part(0, 2)
            if info.via == "windows":
                scratch.mkdir(parents=True, exist_ok=True)
                decoded = scratch / f"{batch_id}-{number:05d}.wav"
                helper = helper_now()

                def decoding(event: dict[str, Any]) -> None:
                    total, done = int(event.get("total") or 0), int(event.get("done") or 0)
                    if total > 0:
                        progress.part(0, 1)(min(done, total), total)

                with windows.carrying_stop(handle, helper):
                    windows.decode(helper, song.source, str(decoded), on_event=decoding)
                info, source, converting = inspect_source(decoded), str(decoded), progress.part(1, 1)  # (what Windows made of it, which may differ from what it said first)
            engine.convert_file(source, part, info, request.spec, check=handle.check, on_progress=converting)
            tags, cover = _tags_for(song), art.find_art(Path(song.source)) if request.cover else None
            patiently(lambda: write_tags(part, request.spec.format, tags, cover))  # (as the ripper does: a virus scanner may have the file for a moment, and Windows says it is in use)
        if os.path.lexists(target):  # (it appeared while this song was being converted)
            raise _Skip("Already there: left as it is.")
        try:
            patiently(lambda: move_into_place(part, target))  # (on Windows this refuses a name that was taken a moment ago, and waits if the file is in use)
        except FileExistsError:
            raise _Skip("Already there: left as it is.") from None
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        _remove_folders(made_folders)
        raise
    finally:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        if decoded is not None:
            with contextlib.suppress(OSError):
                decoded.unlink(missing_ok=True)


def run_conversion(
    *, db: Callable[[], Any], make_plan: Callable[[Callable[[int, int], None], Callable[[], None]], planning.ConvertPlan], request: Request, batch_id: str,
    scratch: Path, handle: Any,
) -> dict[str, Any]:
    """Convert what `make_plan` says (it is made again here: the files may have changed since the preview). `db` gives a connection as a context
    manager; `handle` reports progress and notices a stop. Raises an error written for the person when nothing could be converted, and JobCancelled
    when it was stopped."""
    from musictoolkit.web.jobs import (
        JobCancelled,  # noqa: PLC0415 - the job runner's own signal for a stop
    )

    def looking(done: int, total: int) -> None:
        handle.update(done=done, total=total, message=f"Looking at the songs again: {done:,} of {total:,}")

    handle.update(message="Looking at the songs again…")
    plan = make_plan(looking, handle.check)
    handle.check()  # (a stop that came as the looking ended: nothing has been written or recorded yet)
    if plan.problems:
        raise ConvertProblem(plan.problems[0])
    writing = plan.writing
    try:
        Path(plan.destination).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConvertProblem(f"The folder could not be made ({exc.strerror or exc}).") from None
    spec = request.spec
    label = formats_label(spec)
    with db() as conn:
        conversion_id, ids = store.create_conversion(
            conn, batch_id=batch_id, title=f"{len(writing)} song{'s' if len(writing) != 1 else ''} to {label}", fmt=spec.format, quality=spec.quality or "",
            destination=plan.destination, scheme=plan.scheme, options={"max_rate": spec.max_rate, "depth": spec.depth, "existing": spec.existing, "cover": request.cover},
            left_out=len(plan.songs) - len(writing), items=[{"track_id": s.track_id, "title": s.title, "source": s.source, "target": s.target, "action": s.action} for s in writing],
        )
    progress = _Progress(handle, sum(max(int(s.seconds * 1000), 1) for s in writing))
    stack = contextlib.ExitStack()
    helpers: list[DiscHelper] = []

    def helper_now() -> DiscHelper:
        if not helpers:
            helpers.append(stack.enter_context(DiscHelper()))
        return helpers[0]

    stopped, fatal = False, None
    try:
        for number, (song, item_id) in enumerate(zip(writing, ids), start=1):
            try:
                handle.check()
            except JobCancelled:
                stopped = True
                break
            progress.begin(f"{'Copying' if song.action == 'copy' else 'Converting'} song {number} of {len(writing)}: {song.title}", int(song.seconds * 1000))
            with db() as conn:
                store.set_item(conn, item_id, status="working")
            fields: dict[str, Any]
            try:
                _write_song(song, request, scratch, batch_id, number, handle, progress, helper_now)
            except _Skip as skip:
                fields = {"status": "skipped", "message": str(skip)}
            except JobCancelled:
                stopped = True
                fields = {"status": "skipped", "message": "Stopped before this song was finished."}
            except ConvertError as error:
                fields = {"status": "failed", "message": str(error)}
                if str(error) == engine.NO_ROOM:
                    fatal = str(error)
            except DiscError as error:
                if error.code == "cancelled":  # (a press of Stop that reached Windows' decoder)
                    stopped = True
                    fields = {"status": "skipped", "message": "Stopped before this song was finished."}
                else:
                    fields = {"status": "failed", "message": str(error)}
            except (TagError, SourceError, DiscUnavailable) as error:
                fields = {"status": "failed", "message": str(error)}
            except OSError as error:
                fields = {"status": "failed", "message": f"The file could not be written ({error.strerror or error})."}
                if engine.no_room(error, Path(song.target).parent):
                    fields["message"], fatal = engine.NO_ROOM, engine.NO_ROOM
            else:
                made = os.stat(song.target)
                fields = {"status": "done", "size": made.st_size, "mtime": made.st_mtime}
            with db() as conn:
                store.set_item(conn, item_id, **fields)
            if stopped or fatal:
                break
            progress.end()
    finally:
        stack.close()
        with contextlib.suppress(OSError):
            for leftover in scratch.glob(f"{batch_id}-*"):
                leftover.unlink()
        for song in writing:  # (a program that held on to a half-written file a moment ago has most likely let go by now)
            with contextlib.suppress(OSError):
                Path(song.target + store.PART_SUFFIX).unlink(missing_ok=True)
    note = "Stopped." if stopped else fatal or "Stopped: there was no room for the rest."
    with db() as conn:
        conn.execute("UPDATE conversion_items SET status = 'skipped', message = ? WHERE conversion_id = ? AND status = 'waiting'", (note, conversion_id))
        conn.commit()
        done = conn.execute("SELECT COUNT(*) FROM conversion_items WHERE conversion_id = ? AND status = 'done'", (conversion_id,)).fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM conversion_items WHERE conversion_id = ? AND status = 'failed'", (conversion_id,)).fetchone()[0]
        status = "cancelled" if stopped else ("failed" if not done and (failed or fatal) else "partial" if failed else "done")
        message = ("Stopped. The songs that were finished stay where they are." if stopped else fatal) or (f"{failed} song{'s' if failed != 1 else ''} could not be converted." if failed else None)
        store.finish_conversion(conn, conversion_id, status, message)
        header = store.get_conversion(conn, conversion_id, limit=1) or {}
    if stopped:
        raise JobCancelled()
    if status == "failed":
        raise ConvertProblem(message or "No song could be converted.")
    return {
        "conversion_id": conversion_id, "batch_id": batch_id, "status": status, "message": message, "destination": plan.destination, "songs": header.get("songs", 0),
        "converted": header.get("converted", 0), "copied": header.get("copied", 0), "failed": header.get("failed", 0), "left_out": header.get("left_out", 0),
        "bytes": header.get("bytes", 0), "previewed": request.previewed, "ended": fatal,  # (why the batch was ended before its last song, if it was: no room)
    }


def formats_label(spec: Spec) -> str:
    from musictoolkit.convert import (
        formats,  # noqa: PLC0415 - (only for the words of the title)
    )

    chosen = formats.FORMATS[spec.format]
    return f"{chosen.label} ({chosen.quality(spec.quality).label.lower()})" if chosen.lossy else chosen.label


def undo_conversion(*, db: Callable[[], Any], conversion_id: int, handle: Any) -> dict[str, Any]:
    """Take a conversion back: delete the files it made that are still as it left them (a file that has been changed since is the person's now, and
    is left alone), then the folders that this empties. Never touches anything else."""
    with db() as conn:
        header = store.get_conversion(conn, conversion_id, limit=1)
        items = store.done_items(conn, conversion_id)
    if header is None:
        raise ConvertProblem("That conversion is not in the history.")
    if header["undone_at"]:
        raise ConvertProblem("That conversion has been taken back already.")
    folder = Path(header["destination"])
    removed = changed = gone = failed = 0
    for number, item in enumerate(items, start=1):
        handle.check()
        handle.update(done=number, total=len(items), message=f"Taking back song {number} of {len(items)}")
        path = Path(item["target"])
        try:
            made = os.stat(path)
        except FileNotFoundError:
            gone += 1
            continue
        except OSError:
            failed += 1
            continue
        if made.st_size != item["size"] or abs(made.st_mtime - (item["mtime"] or 0)) > MTIME_SLACK:
            changed += 1
            continue
        try:
            path.unlink()
        except OSError:
            failed += 1
            continue
        removed += 1
        parent = path.parent
        while parent != folder and planning.inside(str(parent), str(folder)):
            try:
                parent.rmdir()  # (only if it is empty now)
            except OSError:
                break
            parent = parent.parent
    with db() as conn:
        store.mark_undone(conn, conversion_id)
    return {"conversion_id": conversion_id, "removed": removed, "changed": changed, "gone": gone, "failed": failed}
