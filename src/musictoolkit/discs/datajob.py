"""The jobs that write files to a data disc, make an ISO image file, or erase a rewritable disc.

Like a music burn (burnjob.py), a data burn looks at everything it can before the disc is touched: that every file is still as it was when the
person saw the list, then the disc itself. Afterwards it reads the files back from the disc and compares them with the originals, because a
burned disc that cannot be read is worse than no disc: the person would find out in a car, or years from now.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from musictoolkit.discs import datadisc, store
from musictoolkit.discs import helper as disc_helper
from musictoolkit.discs.burn import BurnError
from musictoolkit.discs.burnjob import BURN_TIMEOUT, _pass_on_cancel, after_a_stop
from musictoolkit.discs.datadisc import SECTOR, DataPlan
from musictoolkit.discs.helper import DiscError, DiscHelper, in_use

logger = logging.getLogger("musictoolkit")

MOUNT_WAIT = 90.0  # seconds Windows may take to show the files of a disc that has just been written
MOUNT_LOOK = 1.0
ERASE_TIMEOUT = 3600.0  # a full erase of a rewritable disc takes many minutes; IMAPI itself gives up after an hour
REWRITABLE = ("CD-RW", "DVD-RW", "DVD+RW", "DVD-RAM", "BD-RE")
TRANSIENT = ("not_ready", "busy", "no_media", "media_changed")  # what a drive says for a moment after it has written and closed a disc


@dataclass
class _State:
    wrote: bool = False  # the recorder reported that sectors were written (or that the erase began)
    written: bool = False  # the burn call came back: what is left is checking
    rewritable: bool = False
    ejected: bool = False


@dataclass
class DataRequest:
    drive: str
    layout: str = "library"
    label: str = ""
    speed: int | None = None
    eject: bool = True
    verify: bool = True
    title: str = ""


@dataclass
class IsoRequest:
    folder: str
    layout: str = "library"
    label: str = ""
    title: str = ""


@dataclass
class EraseRequest:
    drive: str
    full: bool = False


def title_of(request: DataRequest | IsoRequest, plan: DataPlan) -> str:
    count = len(plan.going)
    given = request.title.strip()
    if given:
        return given
    kind = "Data disc" if isinstance(request, DataRequest) else "Disc image"
    return f"{kind} {plan.label} ({count} file{'s' if count != 1 else ''})"


def _items(plan: DataPlan) -> list[dict[str, Any]]:
    return [
        {
            "title": f.song.title or Path(f.song.path).stem, "artist": f.song.artist, "source": f.song.path, "track_id": f.song.track_id,
            "sectors": f.sectors if f.goes else None, "status": "waiting" if f.goes else "skipped", "message": f.problem or None,
        }
        for f in plan.files
    ]


def begin_data(conn: Any, plan: DataPlan, request: DataRequest, batch_id: str, media: dict[str, Any]) -> int:
    return store.create_burn(
        conn, batch_id=batch_id, kind="data", title=title_of(request, plan), drive=request.drive, disc_type=str(media.get("type") or "") or None,
        label=plan.label, speed=request.speed, sectors=plan.disc_sectors, items=_items(plan),
    )


def begin_iso(conn: Any, plan: DataPlan, request: IsoRequest, batch_id: str) -> int:
    return store.create_burn(
        conn, batch_id=batch_id, kind="iso", title=title_of(request, plan), drive=None, label=plan.label, sectors=plan.disc_sectors,
        items=_items(plan),
    )


def begin_erase(conn: Any, request: EraseRequest, batch_id: str, media: dict[str, Any]) -> int:
    kind = str(media.get("type") or "disc")
    return store.create_burn(conn, batch_id=batch_id, kind="erase", title=f"Erase {kind}", drive=request.drive, disc_type=kind or None)


def _files(plan: DataPlan) -> list[dict[str, str]]:
    return [{"src": f.song.path, "dest": str(f.dest)} for f in plan.going]


def check_sources(plan: DataPlan) -> None:
    """Every file is still the file the person was shown: there, and the size it was. Raised before anything is written."""
    for entry in plan.going:
        try:
            size = os.stat(entry.song.path).st_size
        except OSError:
            raise BurnError(f"{Path(entry.song.path).name} is not there any more. Look at the list again.") from None
        if size != entry.size:
            raise BurnError(f"{Path(entry.song.path).name} has changed since the list was made. Look at the list again.")


def _wait_for_files(helper: DiscHelper, drive: str, plan: DataPlan, handle: Any) -> Path:
    """Windows takes a moment to show the files of a disc that was just written: wait until the first of them can be seen."""
    first = plan.going[0]
    deadline = time.monotonic() + MOUNT_WAIT
    while True:
        handle.check()
        try:
            root = helper.call("media", drive=drive).get("root")
        except DiscError as error:
            if error.code not in TRANSIENT:
                raise
            root = None  # (the drive is still settling after the write: look again in a moment)
        if root and (Path(root) / str(first.dest)).is_file():
            return Path(root)
        if time.monotonic() > deadline:
            raise BurnError("The disc was written, but Windows did not show its files in time to check them. Take it out and put it back, then look at it.")
        time.sleep(MOUNT_LOOK)


def run_data(
    *, db: Callable[[], Any], plan: DataPlan, request: DataRequest, batch_id: str, burn_id: int, handle: Any,
) -> dict[str, Any]:
    """The job: write the files, read them back and compare. Raises an error written for the person when it ends without finishing, and
    JobCancelled when it was stopped."""
    from musictoolkit.web.jobs import JobCancelled  # noqa: PLC0415 - the job runner's own signal for a stop

    burn_units = plan.disc_sectors
    verify_units = -(-plan.total_bytes // SECTOR) if request.verify else 0
    overall = burn_units + verify_units
    state = _State()
    status, message, verified = "done", None, None
    problem: BaseException | None = None
    stopped = False
    intact = False
    written = None
    try:
        handle.check()
        check_sources(plan)
        with in_use(), DiscHelper() as helper:
            handle.update(done=0, total=overall, message="Looking at the disc")
            media = helper.call("media", drive=request.drive)
            state.rewritable = bool(media.get("rewritable"))
            trouble = datadisc.media_problems(media)
            if not trouble:
                free = int(media.get("free_sectors") or 0)
                if plan.disc_sectors + datadisc.RESERVE_SECTORS > free:
                    trouble = [
                        f"The files take {datadisc.megabytes(plan.disc_sectors * SECTOR)} and the disc has {datadisc.megabytes(free * SECTOR)} free."
                    ]
            if trouble:
                raise BurnError(trouble[0])
            handle.check()

            def tell(event: dict[str, Any]) -> None:
                done = int(event.get("done") or 0)
                if done > 0 and not state.wrote:
                    state.wrote = True
                    with db() as conn:
                        store.mark_spoiled(conn, burn_id)
                total = int(event.get("total") or 0) or burn_units
                words = f"Writing to the disc: {100 * min(done, total) // max(total, 1)}%"
                if event.get("message"):
                    words += f" ({str(event['message']).lower()})"
                handle.update(done=min(done, burn_units), total=overall, message=words)

            handle.update(done=0, total=overall, message="Preparing the disc")
            stop_watching = threading.Event()
            watcher = threading.Thread(target=_pass_on_cancel, args=(handle, helper, stop_watching), name="disc-stop-watcher", daemon=True)
            watcher.start()
            try:
                helper.call(
                    "burn_data", drive=request.drive, label=plan.label, label_iso=datadisc.iso_label(plan.label), files=_files(plan),
                    filesystems=plan.filesystems, **({"speed": request.speed} if request.speed else {}), timeout=BURN_TIMEOUT, on_event=tell,
                )
            finally:
                stop_watching.set()
                watcher.join(timeout=2)
            written = plan.disc_sectors
            state.written = True
            if request.verify:
                handle.update(done=burn_units, total=overall, message="Waiting for the disc")
                root = _wait_for_files(helper, request.drive, plan, handle)

                def reading(done: int, total: int) -> None:
                    handle.update(done=burn_units + verify_units * done // max(total, 1), total=overall, message=f"Checking the disc: {100 * done // max(total, 1)}%")

                verdict = datadisc.verify_files(plan, root, check=handle.check, on_progress=reading)
                verified = verdict.ok
                if not verdict.ok:
                    raise BurnError(verdict.words())
            handle.update(done=overall, total=overall, message="Finishing")
            if request.eject:
                try:
                    helper.call("eject", drive=request.drive)
                    state.ejected = True
                except DiscError as error:
                    handle.log(f"The disc could not be ejected: {error}")
    except JobCancelled:
        stopped = True
        status, message = "cancelled", "Stopped. " + (_after_a_stop_while_checking() if state.written else after_a_stop(state.wrote, state.rewritable))
        intact = state.written  # (the disc was finished; only the checking was cut short)
    except DiscError as error:
        if error.code == "cancelled":
            stopped = True
            status, message = "cancelled", "Stopped. " + after_a_stop(state.wrote, state.rewritable)
        else:
            status, message = "failed", f"{error} {_what_became_of_the_disc(state)}"
            problem = DiscError(error.code, message)
            intact = state.written  # (the disc was finished; what failed was the looking at it)
    except BurnError as error:
        status, message = "failed", str(error)
        problem = error
    except Exception as error:  # a slip in this code: say what it was
        logger.error("Data burn %s failed", batch_id, exc_info=True)
        status, message = "failed", f"{type(error).__name__}: {error}. {_what_became_of_the_disc(state)}"
        problem = BurnError(message)
        intact = state.written
    finally:
        with db() as conn:
            store.finish_burn(conn, burn_id, status, message, sectors=written, verified=verified, spoiled=False if intact else None)
        disc_helper.forget_listing()
    if stopped:
        raise JobCancelled()
    if problem is not None:
        raise problem
    return {
        "burn_id": burn_id, "batch_id": batch_id, "files": len(plan.going), "sectors": written, "verified": verified, "ejected": state.ejected,
        "status": status,
    }


def _after_a_stop_while_checking() -> str:
    return "The files had been written, but they were not all checked."


def _what_became_of_the_disc(state: _State) -> str:
    """For a stop or a failure: whether the disc was touched, and if it was finished, that only the checking of it did not happen."""
    return _after_a_stop_while_checking() if state.written else after_a_stop(state.wrote, state.rewritable)


def run_iso(*, db: Callable[[], Any], plan: DataPlan, request: IsoRequest, batch_id: str, burn_id: int, output: Path, handle: Any) -> dict[str, Any]:
    """The job: write the image file. A stop or a failure removes whatever was written of it."""
    from musictoolkit.web.jobs import JobCancelled  # noqa: PLC0415

    status, message = "done", None
    problem: BaseException | None = None
    stopped = False
    made = None
    try:
        handle.check()
        check_sources(plan)
        with DiscHelper() as helper:
            def tell(event: dict[str, Any]) -> None:
                done, total = int(event.get("done") or 0), int(event.get("total") or 0) or plan.disc_sectors
                handle.update(done=min(done, total), total=total, message=f"Making the image: {100 * min(done, total) // max(total, 1)}%")

            handle.update(done=0, total=plan.disc_sectors, message="Making the image")
            stop_watching = threading.Event()
            watcher = threading.Thread(target=_pass_on_cancel, args=(handle, helper, stop_watching), name="disc-stop-watcher", daemon=True)
            watcher.start()
            try:
                helper.call(
                    "build_iso", label=plan.label, label_iso=datadisc.iso_label(plan.label), files=_files(plan), filesystems=plan.filesystems,
                    out=str(output), timeout=BURN_TIMEOUT, on_event=tell,
                )
            finally:
                stop_watching.set()
                watcher.join(timeout=2)
        size = output.stat().st_size if output.is_file() else 0
        if size == 0 or size % SECTOR:
            raise BurnError("The image file that was made is not a whole number of disc sectors, so it cannot be trusted. It was removed.")
        made = size
    except JobCancelled:
        stopped = True
        status, message = "cancelled", "Stopped. The image file was removed."
    except DiscError as error:
        if error.code == "cancelled":
            stopped = True
            status, message = "cancelled", "Stopped. The image file was removed."
        else:
            status, message = "failed", f"{error} The image file was removed."
            problem = DiscError(error.code, message)
    except BurnError as error:
        status, message = "failed", str(error)
        problem = error
    except Exception as error:
        logger.error("Image %s failed", batch_id, exc_info=True)
        status, message = "failed", f"{type(error).__name__}: {error}. The image file was removed."
        problem = BurnError(message)
    finally:
        if status != "done":
            for leftover in (output, output.with_name(output.name + ".part")):
                with contextlib.suppress(OSError):
                    leftover.unlink(missing_ok=True)
        with db() as conn:
            store.finish_burn(conn, burn_id, status, message, sectors=(made // SECTOR) if made else None, output=str(output) if status == "done" else None)
    if stopped:
        raise JobCancelled()
    if problem is not None:
        raise problem
    return {"burn_id": burn_id, "batch_id": batch_id, "files": len(plan.going), "bytes": made, "output": str(output), "status": status}


def run_erase(*, db: Callable[[], Any], request: EraseRequest, batch_id: str, burn_id: int, handle: Any) -> dict[str, Any]:
    """The job: erase a rewritable disc. The recorder gives no way to stop an erase half way, so a stop only counts before it begins."""
    from musictoolkit.web.jobs import JobCancelled  # noqa: PLC0415

    status, message = "done", None
    problem: BaseException | None = None
    stopped = False
    state = _State()
    try:
        handle.check()
        with in_use(), DiscHelper() as helper:
            handle.update(message="Looking at the disc")
            media = helper.call("media", drive=request.drive)
            if not media.get("writer"):
                raise BurnError("This drive cannot write discs.")
            if media.get("state") == "none":
                raise BurnError("There is no disc in the drive.")
            if media.get("blank"):
                message = "The disc was already blank, so there was nothing to erase."
            elif not media.get("rewritable"):
                raise BurnError(f"This {media.get('type') or 'disc'} cannot be erased: only rewritable discs (CD-RW, DVD-RW, DVD+RW, BD-RE) can.")
            else:
                handle.check()
                state.wrote = True
                handle.update(message="Erasing the disc. A full erase can take many minutes, and this cannot be stopped once it has begun.")

                def tell(event: dict[str, Any]) -> None:
                    handle.update(message=str(event.get("message") or "Erasing the disc"))

                helper.call("erase", drive=request.drive, full=bool(request.full), timeout=ERASE_TIMEOUT, on_event=tell)
                message = "The disc was erased and is blank again."
    except JobCancelled:
        stopped = True
        status, message = "cancelled", "Stopped before anything was erased."
    except DiscError as error:
        if error.code == "cancelled":
            stopped = True
            status, message = "cancelled", "Stopped."
        else:
            status, message = "failed", f"{error}" + (" The disc may be half erased: try again." if state.wrote and error.code in ("failed", "timeout", "stopped") else "")
            problem = DiscError(error.code, message)
    except BurnError as error:
        status, message = "failed", str(error)
        problem = error
    except Exception as error:
        logger.error("Erase %s failed", batch_id, exc_info=True)
        status, message = "failed", f"{type(error).__name__}: {error}"
        problem = BurnError(message)
    finally:
        with db() as conn:
            store.finish_burn(conn, burn_id, status, message)
        disc_helper.forget_listing()
    if stopped:
        raise JobCancelled()
    if problem is not None:
        raise problem
    return {"burn_id": burn_id, "batch_id": batch_id, "status": status, "message": message}

