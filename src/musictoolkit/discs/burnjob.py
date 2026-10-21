"""The jobs that write to a disc, in the order that keeps a disc safe.

A disc cannot be taken back once it is written, and a burn that stops half way spoils a CD-R for good. So everything that can go wrong without
touching the disc happens first: every song is turned into CD audio in a temporary file, then the disc is looked at again (it may have been
changed since the preview), and only then is it written. Whatever happens, the temporary files are removed and the history says what became
of the disc.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from musictoolkit.discs import burn, store
from musictoolkit.discs import helper as disc_helper
from musictoolkit.discs.burn import AudioPlan, BurnError
from musictoolkit.discs.helper import DiscError, DiscHelper, in_use

logger = logging.getLogger("musictoolkit")

BURN_TIMEOUT = 600.0  # seconds the recorder may stay silent (preparing the disc and closing it can take a while) before it is given up on
WATCH_INTERVAL = 0.25  # seconds between looks at whether the person pressed Stop


@dataclass
class _State:
    wrote: bool = False  # the recorder reported that sectors were written
    rewritable: bool = False
    ejected: bool = False


@dataclass
class AudioRequest:
    drive: str
    speed: int | None = None  # one of the speeds the recorder listed; None lets it choose
    eject: bool = True
    title: str = ""  # what the history calls this
    gapless: bool = False  # the songs run on from one another (the whole disc is written in one go); otherwise the recorder leaves two seconds


def title_of(request: AudioRequest, plan: AudioPlan) -> str:
    count = len(plan.burnable)
    return request.title.strip() or f"Music CD of {count} song{'s' if count != 1 else ''}"


def begin_audio(conn: Any, plan: AudioPlan, request: AudioRequest, batch_id: str, media: dict[str, Any]) -> int:
    """Put the burn in the history, with every song as a row (those that will not be written say why)."""
    items = [
        {
            "title": s.song.title or Path(s.song.path).stem, "artist": s.song.artist, "source": s.song.path, "track_id": s.song.track_id,
            "sectors": s.check.sectors if s.burnable else None, "status": "waiting" if s.burnable else "skipped",
            "message": None if s.burnable else " ".join(s.check.notes),
        }
        for s in plan.songs
    ]
    return store.create_burn(
        conn, batch_id=batch_id, kind="audio", title=title_of(request, plan), drive=request.drive, disc_type=str(media.get("type") or "") or None,
        speed=request.speed, sectors=plan.disc_sectors, items=items, gaps="none" if request.gapless else "2s",
    )


def after_a_stop(wrote: bool, rewritable: bool) -> str:
    """What became of the disc when a burn was stopped or failed, in words for the person."""
    if not wrote:
        return "Nothing had been written, so the disc is as it was."
    if rewritable:
        return "Writing had begun, so the disc is probably unusable now. It is rewritable: erase it (Erase, on this screen) and it can be used again."
    return "Writing had begun, so the disc is probably unusable now. A CD-R cannot be written twice; put in a new disc."


def _pass_on_cancel(handle: Any, helper: DiscHelper, stop: threading.Event) -> None:
    """While the recorder is busy, the helper is not heard from in between its progress events; this carries a press of Stop to it at once."""
    while not stop.wait(WATCH_INTERVAL):
        if handle.cancelled:
            helper.cancel()  # (does nothing if no request is running just now; the next look tries again)


def run_audio(
    *, db: Callable[[], Any], plan: AudioPlan, request: AudioRequest, batch_id: str, burn_id: int, scratch: Path, handle: Any,
) -> dict[str, Any]:
    """The job: turn every song into CD audio, then write the disc. `db` gives a connection as a context manager; `handle` reports progress and
    notices a stop. Raises an error written for the person when the burn ends without finishing, and JobCancelled when it was stopped."""
    from musictoolkit.web.jobs import JobCancelled  # noqa: PLC0415 - the job runner's own signal for a stop

    songs = plan.burnable
    prepare_units = sum(s.check.sectors for s in songs)
    overall = prepare_units + plan.disc_sectors
    state = _State()
    status, message = "done", None
    problem: BaseException | None = None
    stopped = False
    written = None
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        files: list[tuple[Path, int]] = []
        before = 0
        for index, planned in enumerate(songs, start=1):
            handle.check()
            name = planned.song.title or Path(planned.song.path).stem
            label = f"Preparing song {index} of {len(songs)}: {name}"
            handle.update(done=before, total=overall, message=label)

            def progress(done: int, total: int, planned=planned, before=before, label=label) -> None:
                handle.update(done=before + planned.check.sectors * done // max(total, 1), total=overall, message=f"{label} ({100 * done // max(total, 1)}%)")

            target = scratch / f"{batch_id}-{index:02d}.pcm"
            files.append((target, burn.decode_to_pcm(planned.song.path, target, check=handle.check, on_progress=progress)))
            before += planned.check.sectors
        handle.check()

        with in_use(), DiscHelper() as helper:
            media = helper.call("media", drive=request.drive)
            state.rewritable = bool(media.get("rewritable"))
            actual = sum(sectors for _, sectors in files) + (0 if request.gapless else burn.GAP_SECTORS * (len(files) - 1))
            trouble = burn.media_problems(media)
            if not trouble and request.gapless and not media.get("gapless"):
                trouble = [burn.NO_GAPLESS]
            if not trouble:
                tight = burn.fit_problem(actual, int(media.get("free_sectors") or 0))
                trouble = [tight] if tight else []
            if trouble:
                raise BurnError(trouble[0])
            handle.check()  # (a last chance to stop with the disc untouched)

            def tell(event: dict[str, Any]) -> None:
                done = int(event.get("done") or 0)
                if done > 0 and not state.wrote:
                    state.wrote = True
                    with db() as conn:
                        store.mark_spoiled(conn, burn_id)
                total = int(event.get("total") or 0) or plan.disc_sectors
                words = f"Writing to the disc: {100 * min(done, total) // max(total, 1)}%"
                if event.get("message"):
                    words += f" ({str(event['message']).lower()})"
                handle.update(done=prepare_units + min(done, plan.disc_sectors), total=overall, message=words)

            handle.update(done=prepare_units, total=overall, message="Preparing the disc")
            stop_watching = threading.Event()
            watcher = threading.Thread(target=_pass_on_cancel, args=(handle, helper, stop_watching), name="disc-stop-watcher", daemon=True)
            watcher.start()
            try:
                helper.call(
                    "burn_audio", drive=request.drive, tracks=[{"path": str(path), "sectors": sectors} for path, sectors in files],
                    **({"speed": request.speed} if request.speed else {}), **({"gapless": True} if request.gapless else {}), timeout=BURN_TIMEOUT, on_event=tell,
                )
            finally:
                stop_watching.set()
                watcher.join(timeout=2)
            written = actual
            handle.update(done=overall, total=overall, message="Finishing")
            if request.eject:
                try:
                    helper.call("eject", drive=request.drive)
                    state.ejected = True
                except DiscError as error:
                    handle.log(f"The disc could not be ejected: {error}")
    except JobCancelled:
        stopped = True
        status, message = "cancelled", "Stopped. " + after_a_stop(state.wrote, state.rewritable)
    except DiscError as error:
        if error.code == "cancelled":
            stopped = True
            status, message = "cancelled", "Stopped. " + after_a_stop(state.wrote, state.rewritable)
        else:
            status, message = "failed", f"{error} {after_a_stop(state.wrote, state.rewritable)}"
            problem = DiscError(error.code, message)
    except BurnError as error:
        status, message = "failed", str(error)
        problem = error
    except Exception as error:  # a slip in this code: say what it was
        logger.error("Burn %s failed", batch_id, exc_info=True)
        status, message = "failed", f"{type(error).__name__}: {error}. {after_a_stop(state.wrote, state.rewritable)}"
        problem = BurnError(message)
    finally:
        with db() as conn:
            store.finish_burn(conn, burn_id, status, message, sectors=written)
        burn.forget_scratch(scratch, batch_id)
        disc_helper.forget_listing()  # what is in the drive has changed
    if stopped:
        raise JobCancelled()
    if problem is not None:
        raise problem
    return {"burn_id": burn_id, "batch_id": batch_id, "songs": len(songs), "sectors": written, "ejected": state.ejected, "status": status}

