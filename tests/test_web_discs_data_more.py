"""The corners of writing files to a disc: what the job asks of the recorder, what it reports while it works, and what it tells the history when it is
stopped at one point or another. (tests/test_web_discs_data.py has the main paths; these call the job directly where a test has to act at one
chosen moment.)"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from musictoolkit.discs import burnjob, datadisc, datajob, store
from musictoolkit.discs import helper as disc_helper
from musictoolkit.discs.helper import DiscError
from musictoolkit.web.jobs import JobCancelled
from musictoolkit.web.routers import discs as routes
from tests import test_web_discs_data as base
from tests.conftest import ids_by_title, make_track, scan
from tests.fake_disc_helper import add_drive, audio_disc, blank_disc, set_control
from tests.test_web_discs_data import DVD, ONE, THREE, TWO, Handle, ask, begin, data_songs, finish, wait_until

rig = base.rig  # (the same stand-in recorder, library and app as the main tests of data discs)
STOPPED = "Stopped. Nothing had been written, so the disc is as it was."


def media_of(rig) -> dict:
    return rig.client.get("/api/discs/media/E:").json()


def prepared(rig, *relative: str, batch: str = "more", label: str = "Road Trip", **changes):
    """A plan, a request and a history row for these files, ready for datajob.run_data."""
    found = media_of(rig)
    plan = datadisc.plan_data(data_songs(rig, *(relative or (ONE, TWO, THREE))), label=label, media=found)
    assert plan.problems == []
    request = datajob.DataRequest(drive="E:", **{"eject": False, **changes})
    with rig.ctx.db() as conn:
        burn_id = datajob.begin_data(conn, plan, request, batch, found)
    return plan, request, burn_id


def run(rig, plan, request, burn_id, handle, batch: str = "more"):
    return datajob.run_data(db=rig.ctx.db, plan=plan, request=request, batch_id=batch, burn_id=burn_id, handle=handle)


def history(rig, burn_id: int) -> dict:
    with rig.ctx.db() as conn:
        return store.get_burn(conn, burn_id)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch):
    """Notes how often the job opens the recorder and what it asks of it (with the words of each request), and lets a test act when a command arrives."""
    note = SimpleNamespace(opened=0, commands=[], asked={}, when={})
    real = datajob.DiscHelper

    class Watching(real):
        def __init__(self, *args, **kwargs) -> None:
            note.opened += 1
            super().__init__(*args, **kwargs)

        def call(self, cmd, **kwargs):
            note.commands.append(cmd)
            note.asked[cmd] = kwargs
            if cmd in note.when:
                note.when[cmd]()
            return super().call(cmd, **kwargs)

    monkeypatch.setattr(datajob, "DiscHelper", Watching)
    return note


# ---------------------------------------------------------------------------------------- what the recorder is asked

def test_the_recorder_is_given_the_names_the_files_and_the_file_systems_of_the_plan(rig, recorder) -> None:
    plan, request, burn_id = prepared(rig)
    run(rig, plan, request, burn_id, Handle())
    asked = recorder.asked["burn_data"]
    assert (asked["drive"], asked["label"], asked["label_iso"], asked["filesystems"]) == ("E:", "Road Trip", "ROAD_TRIP", ["iso9660", "joliet", "udf"])
    assert asked["files"] == [{"src": str(rig.music / r), "dest": r} for r in (ONE, TWO, THREE)]
    assert asked["timeout"] == datajob.BURN_TIMEOUT and callable(asked["on_event"]) and "speed" not in asked


def test_a_speed_is_asked_for_only_when_one_was_chosen(rig, recorder) -> None:
    plan, request, burn_id = prepared(rig, speed=75 * 4)
    run(rig, plan, request, burn_id, Handle())
    assert recorder.asked["burn_data"]["speed"] == 300


def image_of(rig, *relative: str, batch: str = "image", label: str = "Road Trip"):
    """A plan, a request, a history row and a place for an image of these files, ready for datajob.run_iso."""
    plan = datadisc.plan_data(data_songs(rig, *(relative or (ONE, TWO, THREE))), label=label)
    assert plan.problems == []
    request = datajob.IsoRequest()
    with rig.ctx.db() as conn:
        burn_id = datajob.begin_iso(conn, plan, request, batch)
    return plan, request, burn_id, rig.tmp / "Road Trip.iso"


def run_image(rig, plan, request, burn_id, output, handle, batch: str = "image"):
    return datajob.run_iso(db=rig.ctx.db, plan=plan, request=request, batch_id=batch, burn_id=burn_id, output=output, handle=handle)


def test_the_recorder_is_given_the_names_the_files_and_the_file_systems_of_the_plan_for_an_image_too(rig, recorder) -> None:
    plan, request, burn_id, output = image_of(rig)
    run_image(rig, plan, request, burn_id, output, Handle())
    asked = recorder.asked["build_iso"]
    assert (asked["label"], asked["label_iso"], asked["filesystems"], asked["out"]) == ("Road Trip", "ROAD_TRIP", plan.filesystems, str(output))
    assert asked["files"] == [{"src": str(rig.music / r), "dest": r} for r in (ONE, TWO, THREE)]
    assert asked["timeout"] == datajob.BURN_TIMEOUT and callable(asked["on_event"]) and "drive" not in asked


# ---------------------------------------------------------------------------------------- a stop at each point

def test_a_stop_pressed_after_the_disc_was_looked_at_is_the_last_chance_to_leave_it_alone(rig, recorder) -> None:
    plan, request, burn_id = prepared(rig)
    handle = Handle()
    recorder.when["media"] = lambda: setattr(handle, "cancelled", True)
    with pytest.raises(JobCancelled):
        run(rig, plan, request, burn_id, handle)
    assert recorder.commands == ["media"], "the recorder was not asked to write"
    shown = history(rig, burn_id)
    assert (shown["status"], shown["spoiled"], shown["message"]) == ("cancelled", 0, STOPPED)
    assert media_of(rig)["blank"] is True


def test_a_disc_changed_after_the_preview_is_looked_at_again_before_it_is_written(rig, recorder) -> None:
    plan, request, burn_id = prepared(rig)
    add_drive(rig.world, "E", writer=True, disc={**blank_disc(type="DVD+R", capacity=DVD), "kind": "unknown"})  # somebody changed the disc in between
    with pytest.raises(burnjob.BurnError, match="not blank"):
        run(rig, plan, request, burn_id, Handle())
    assert recorder.commands == ["media"]
    shown = history(rig, burn_id)
    assert (shown["status"], shown["spoiled"]) == ("failed", 0) and "not blank" in shown["message"]


def test_a_disc_with_less_room_than_the_plan_counted_on_is_refused_with_the_reserve_in_mind(rig, recorder) -> None:
    plan, request, burn_id = prepared(rig)
    needed = plan.disc_sectors + datadisc.RESERVE_SECTORS
    add_drive(rig.world, "E", writer=True, disc=blank_disc(type="DVD+R", capacity=needed - 1))  # one sector short, reserve included
    with pytest.raises(burnjob.BurnError, match=r"^The files take \d+ MB and the disc has \d+ MB free\.$"):
        run(rig, plan, request, burn_id, Handle())
    assert recorder.commands == ["media"]
    add_drive(rig.world, "E", writer=True, disc=blank_disc(type="DVD+R", capacity=needed))  # exactly enough
    run(rig, plan, request, burn_id, Handle(), batch="more2")
    assert "burn_data" in recorder.commands, "and exactly enough is enough"


def test_a_disc_that_shows_its_drive_before_its_files_is_waited_for(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(datajob, "MOUNT_LOOK", 0.02)
    empty = rig.tmp / "not-yet"
    empty.mkdir()
    seen = {"written": False, "asked": 0}
    real = disc_helper.DiscHelper.call

    def call(self, cmd, **kwargs):
        reply = real(self, cmd, **kwargs)
        if cmd == "burn_data":
            seen["written"] = True
        elif cmd == "media" and seen["written"]:
            seen["asked"] += 1
            if seen["asked"] <= 2:
                return {**reply, "root": str(empty)}  # (the drive has its letter back, but Windows does not show the files yet)
        return reply

    monkeypatch.setattr(disc_helper.DiscHelper, "call", call)
    job, history, _ = finish(rig, "data")
    assert job.status == "done", job.error
    assert seen["asked"] >= 3 and history["verified"] == 1 and history["spoiled"] == 0


def test_a_rewritable_disc_that_was_stopped_is_said_to_be_erasable(rig) -> None:
    add_drive(rig.world, "E", writer=True, disc=blank_disc(type="CD-RW"))
    set_control(rig.world, burn_slow=0.5)
    started = begin(rig, "data")
    wait_until(rig, started["job"]["id"], "Writing to the disc")
    rig.client.post(f"/api/jobs/{started['job']['id']}/cancel")
    rig.ctx.jobs.wait(started["job"]["id"], timeout=30)
    shown = rig.client.get(f"/api/discs/burns/{started['burn_id']}").json()
    assert (shown["status"], shown["spoiled"]) == ("cancelled", 1)
    assert shown["message"] == "Stopped. Writing had begun, so the disc is probably unusable now. It is rewritable: erase it (Erase, on this screen) and it can be used again."


# ---------------------------------------------------------------------------------------- what the job reports

def test_the_progress_names_what_is_happening_and_only_moves_forward(rig) -> None:
    plan, request, burn_id = prepared(rig)
    handle = Handle()
    result = run(rig, plan, request, burn_id, handle)
    burn_units = plan.disc_sectors
    verify_units = -(-plan.total_bytes // datadisc.SECTOR)
    assert handle.updates[0] == {"done": 0, "total": burn_units + verify_units, "message": "Looking at the disc"}
    words = [str(u.get("message", "")) for u in handle.updates]
    assert "Preparing the disc" in words and "Waiting for the disc" in words
    writing = [u for u in handle.updates if str(u.get("message", "")).startswith("Writing to the disc")]
    assert writing and all(u["done"] <= burn_units for u in writing) and writing[0]["message"].endswith("(file 1 of 3)")
    checking = [u for u in handle.updates if str(u.get("message", "")).startswith("Checking the disc: ")]
    assert checking and all(burn_units <= u["done"] <= burn_units + verify_units for u in checking)
    done = [u["done"] for u in handle.updates if "done" in u]
    assert done == sorted(done), "the progress never goes backwards"
    assert handle.updates[-1] == {"done": burn_units + verify_units, "total": burn_units + verify_units, "message": "Finishing"}
    assert result["sectors"] == burn_units and result["files"] == 3 and result["verified"] is True and result["status"] == "done"


def test_an_image_is_refused_before_the_recorder_is_started_when_a_file_has_changed_since_the_list_was_made(rig, recorder) -> None:
    plan, request, burn_id, output = image_of(rig)
    (rig.music / ONE).write_bytes(b"changed")
    with pytest.raises(burnjob.BurnError, match=r"^01 One\.mp3 has changed since the list was made\. Look at the list again\.$"):
        run_image(rig, plan, request, burn_id, output, Handle())
    assert recorder.opened == 0 and not output.exists()
    shown = history(rig, burn_id)
    assert (shown["status"], shown["output"]) == ("failed", None) and "has changed since the list was made" in shown["message"]


def test_the_progress_of_an_image_names_what_is_happening_and_never_goes_past_the_end(rig) -> None:
    plan, request, burn_id, output = image_of(rig)
    handle = Handle()
    result = run_image(rig, plan, request, burn_id, output, handle)
    assert handle.updates[0] == {"done": 0, "total": plan.disc_sectors, "message": "Making the image"}
    making = handle.updates[1:]
    assert making and all(re.fullmatch(r"Making the image: \d{1,3}%", str(u["message"])) for u in making)
    assert all(u["done"] <= u["total"] for u in making) and [u["done"] for u in making] == sorted(u["done"] for u in making)
    assert result["status"] == "done" and result["bytes"] == output.stat().st_size and history(rig, burn_id)["output"] == str(output)
    assert history(rig, burn_id)["sectors"] == output.stat().st_size // datadisc.SECTOR


@pytest.mark.parametrize("size", [None, 0, 1000, 2049], ids=["no file", "empty file", "not whole sectors", "one byte over"])
def test_an_image_that_is_empty_or_not_whole_sectors_is_refused_and_removed(rig, monkeypatch: pytest.MonkeyPatch, size) -> None:
    real = datajob.DiscHelper

    class Careless(real):
        def call(self, cmd, **kwargs):
            if cmd == "build_iso":
                if size is not None:
                    Path(kwargs["out"]).write_bytes(b"x" * size)
                return {"ok": True}
            return super().call(cmd, **kwargs)

    monkeypatch.setattr(datajob, "DiscHelper", Careless)
    plan, request, burn_id, output = image_of(rig)
    with pytest.raises(burnjob.BurnError, match="not a whole number of disc sectors"):
        run_image(rig, plan, request, burn_id, output, Handle())
    assert not output.exists()
    shown = history(rig, burn_id)
    assert (shown["status"], shown["output"]) == ("failed", None) and "whole number of disc sectors" in shown["message"]


@pytest.mark.parametrize("code", ["failed", "cancelled"])
def test_an_image_that_failed_or_was_stopped_leaves_neither_the_file_nor_the_half_written_one(rig, monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    real = datajob.DiscHelper

    class Dying(real):
        def call(self, cmd, **kwargs):
            if cmd == "build_iso":
                out = Path(kwargs["out"])
                out.with_name(out.name + ".part").write_bytes(b"half of it")
                out.write_bytes(b"x" * 2048)
                raise DiscError(code, "The disc image maker stopped.")
            return super().call(cmd, **kwargs)

    monkeypatch.setattr(datajob, "DiscHelper", Dying)
    plan, request, burn_id, output = image_of(rig)
    with pytest.raises(JobCancelled if code == "cancelled" else DiscError):
        run_image(rig, plan, request, burn_id, output, Handle())
    assert not output.exists() and not output.with_name(output.name + ".part").exists()
    shown = history(rig, burn_id)
    assert (shown["status"], shown["output"]) == ("cancelled" if code == "cancelled" else "failed", None)


def test_a_recorder_that_reports_more_than_the_disc_holds_cannot_push_the_progress_into_the_checking(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = datajob.DiscHelper

    class Overreporting(real):
        def call(self, cmd, **kwargs):
            if cmd == "burn_data":
                kwargs["on_event"]({"event": "progress", "done": 10**9, "total": 10**9, "message": "Writing"})
            return super().call(cmd, **kwargs)

    monkeypatch.setattr(datajob, "DiscHelper", Overreporting)
    plan, request, burn_id = prepared(rig)
    handle = Handle()
    run(rig, plan, request, burn_id, handle)
    overall = plan.disc_sectors + -(-plan.total_bytes // datadisc.SECTOR)
    writing = [u for u in handle.updates if str(u.get("message", "")).startswith("Writing to the disc")]
    assert writing[0] == {"done": plan.disc_sectors, "total": overall, "message": "Writing to the disc: 100% (writing)"}
    assert all(u["done"] <= plan.disc_sectors for u in writing)


def test_without_the_check_the_progress_counts_only_the_writing(rig) -> None:
    plan, request, burn_id = prepared(rig, verify=False)
    handle = Handle()
    result = run(rig, plan, request, burn_id, handle)
    assert handle.updates[-1] == {"done": plan.disc_sectors, "total": plan.disc_sectors, "message": "Finishing"}
    assert result["verified"] is None and not any(str(u.get("message", "")).startswith(("Checking", "Waiting")) for u in handle.updates)


def test_a_disc_that_cannot_be_ejected_is_said_in_the_log_and_the_burn_still_ends_well(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = disc_helper.DiscHelper.call

    def call(self, cmd, **kwargs):
        if cmd == "eject":
            raise DiscError("busy")
        return real(self, cmd, **kwargs)

    monkeypatch.setattr(disc_helper.DiscHelper, "call", call)
    plan, request, burn_id = prepared(rig, eject=True)
    handle = Handle()
    result = run(rig, plan, request, burn_id, handle)
    assert result["ejected"] is False and result["status"] == "done"
    assert handle.logs == ["The disc could not be ejected: The drive is busy with something else."]
    assert history(rig, burn_id)["status"] == "done"


def test_no_watcher_is_left_running_after_a_data_disc_an_image_or_an_erase(rig) -> None:
    job, _, _ = finish(rig, "data")
    assert job.status == "done", job.error
    job, _, _ = finish(rig, "iso")
    assert job.status == "done", job.error
    assert [t.name for t in threading.enumerate() if t.name == "disc-stop-watcher"] == []


def test_the_job_total_counts_the_writing_and_the_reading_back(rig) -> None:
    job, _, _ = finish(rig, "data")
    plan = datadisc.plan_data(data_songs(rig, ONE, TWO, THREE), label="Road Trip")
    assert job.total == plan.disc_sectors + -(-plan.total_bytes // datadisc.SECTOR)
    assert job.done == job.total


# ---------------------------------------------------------------------------------------- an erase, called directly


def erase_of(rig, drive: str = "E:", batch: str = "erase", **changes):
    """A request and a history row for erasing this drive, ready for datajob.run_erase."""
    request = datajob.EraseRequest(drive=drive, **changes)
    with rig.ctx.db() as conn:
        burn_id = datajob.begin_erase(conn, request, batch, {"type": "CD-RW"})
    return request, burn_id


def run_the_erase(rig, request, burn_id, handle, batch: str = "erase"):
    return datajob.run_erase(db=rig.ctx.db, request=request, batch_id=batch, burn_id=burn_id, handle=handle)


def test_an_erase_of_a_drive_that_cannot_write_is_refused_by_the_job_too(rig, recorder) -> None:
    request, burn_id = erase_of(rig, "F:")
    with pytest.raises(burnjob.BurnError, match=r"^This drive cannot write discs\.$"):
        run_the_erase(rig, request, burn_id, Handle())
    assert "erase" not in recorder.commands
    shown = history(rig, burn_id)
    assert (shown["status"], shown["message"]) == ("failed", "This drive cannot write discs.")


def a_written_disc(rig, kind: str = "CD-RW", rewritable: bool = True) -> None:
    add_drive(rig.world, "E", writer=True, disc={**audio_disc(), "type": kind, "rewritable": rewritable})


def no_disc(rig) -> None:
    (rig.world / "E" / "disc.json").unlink()


def test_an_erase_of_a_drive_with_no_disc_in_it_is_refused_by_the_job_too(rig, recorder) -> None:
    no_disc(rig)
    request, burn_id = erase_of(rig)
    with pytest.raises(burnjob.BurnError, match=r"^There is no disc in the drive\.$"):
        run_the_erase(rig, request, burn_id, Handle())
    assert "erase" not in recorder.commands
    assert history(rig, burn_id)["status"] == "failed"


def test_erasing_a_disc_that_is_blank_already_says_so_and_asks_nothing_of_the_recorder(rig, recorder) -> None:
    add_drive(rig.world, "E", writer=True, disc=blank_disc(type="CD-RW"))
    request, burn_id = erase_of(rig)
    result = run_the_erase(rig, request, burn_id, Handle())
    assert result["status"] == "done" and result["message"] == "The disc was already blank, so there was nothing to erase."
    assert "erase" not in recorder.commands
    shown = history(rig, burn_id)
    assert (shown["status"], shown["message"]) == ("done", result["message"])


def test_a_disc_that_cannot_be_erased_is_refused_with_its_kind_named(rig, recorder) -> None:
    a_written_disc(rig, "CD-R", rewritable=False)
    request, burn_id = erase_of(rig)
    words = "This CD-R cannot be erased: only rewritable discs (CD-RW, DVD-RW, DVD+RW, BD-RE) can."
    with pytest.raises(burnjob.BurnError, match=re.escape(words)):
        run_the_erase(rig, request, burn_id, Handle())
    assert "erase" not in recorder.commands
    shown = history(rig, burn_id)
    assert (shown["status"], shown["message"]) == ("failed", words)


def test_a_stop_pressed_before_the_erase_begins_leaves_the_disc_alone(rig, recorder) -> None:
    a_written_disc(rig)
    request, burn_id = erase_of(rig)
    handle = Handle()
    recorder.when["media"] = lambda: setattr(handle, "cancelled", True)
    with pytest.raises(JobCancelled):
        run_the_erase(rig, request, burn_id, handle)
    assert "erase" not in recorder.commands and media_of(rig)["blank"] is False
    shown = history(rig, burn_id)
    assert (shown["status"], shown["message"]) == ("cancelled", "Stopped before anything was erased.")


@pytest.mark.parametrize("full", [False, True])
def test_the_recorder_is_asked_to_erase_quickly_or_fully_with_a_long_wait_allowed(rig, recorder, full: bool) -> None:
    a_written_disc(rig)
    request, burn_id = erase_of(rig, full=full)
    result = run_the_erase(rig, request, burn_id, Handle())
    asked = recorder.asked["erase"]
    assert (asked["drive"], asked["full"], asked["timeout"]) == ("E:", full, datajob.ERASE_TIMEOUT) and callable(asked["on_event"])
    assert result == {"burn_id": burn_id, "batch_id": "erase", "status": "done", "message": "The disc was erased and is blank again."}
    shown = history(rig, burn_id)
    assert (shown["status"], shown["message"]) == ("done", result["message"]) and media_of(rig)["blank"] is True


def test_what_the_recorder_says_while_it_erases_is_passed_on_in_order(rig) -> None:
    a_written_disc(rig)
    request, burn_id = erase_of(rig)
    handle = Handle()
    run_the_erase(rig, request, burn_id, handle)
    assert [u["message"] for u in handle.updates] == [
        "Looking at the disc",
        "Erasing the disc. A full erase can take many minutes, and this cannot be stopped once it has begun.",
        "Erasing the disc (1 seconds so far)",
    ]


@pytest.mark.parametrize("code", ["failed", "timeout", "stopped"])
def test_an_erase_that_fails_after_it_began_says_the_disc_may_be_half_erased(rig, recorder, code: str) -> None:
    a_written_disc(rig)
    request, burn_id = erase_of(rig)

    def refuse() -> None:
        raise DiscError(code, "The drive stopped answering.")

    recorder.when["erase"] = refuse
    with pytest.raises(DiscError) as caught:
        run_the_erase(rig, request, burn_id, Handle())
    words = "The drive stopped answering. The disc may be half erased: try again."
    assert str(caught.value) == words and caught.value.code == code
    shown = history(rig, burn_id)
    assert (shown["status"], shown["message"]) == ("failed", words)


@pytest.mark.parametrize("code", ["busy", "unsupported", "not_ready"])
def test_an_erase_the_recorder_refused_other_ways_does_not_claim_the_disc_is_half_erased(rig, recorder, code: str) -> None:
    a_written_disc(rig)
    request, burn_id = erase_of(rig)

    def refuse() -> None:
        raise DiscError(code, "The drive would not.")

    recorder.when["erase"] = refuse
    with pytest.raises(DiscError, match=r"^The drive would not\.$"):
        run_the_erase(rig, request, burn_id, Handle())
    assert history(rig, burn_id)["message"] == "The drive would not."


def test_a_failure_before_the_erase_began_does_not_claim_the_disc_is_half_erased(rig, recorder) -> None:
    a_written_disc(rig)
    request, burn_id = erase_of(rig)

    def refuse() -> None:
        raise DiscError("failed", "The drive reported a problem.")

    recorder.when["media"] = refuse
    with pytest.raises(DiscError, match=r"^The drive reported a problem\.$"):
        run_the_erase(rig, request, burn_id, Handle())
    assert "erase" not in recorder.commands and media_of(rig)["blank"] is False


def test_a_stop_the_recorder_reports_is_a_stop_and_not_a_failure(rig, recorder) -> None:
    a_written_disc(rig)
    request, burn_id = erase_of(rig)

    def stop() -> None:
        raise DiscError("cancelled")

    recorder.when["erase"] = stop
    with pytest.raises(JobCancelled):
        run_the_erase(rig, request, burn_id, Handle())
    shown = history(rig, burn_id)
    assert (shown["status"], shown["message"]) == ("cancelled", "Stopped.")


def test_the_remembered_list_of_drives_is_dropped_after_an_erase_however_it_ends(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    dropped: list[str] = []
    monkeypatch.setattr(disc_helper, "forget_listing", lambda: dropped.append("forgotten"))
    a_written_disc(rig)
    request, burn_id = erase_of(rig)
    run_the_erase(rig, request, burn_id, Handle())
    no_disc(rig)  # (this one fails)
    request, burn_id = erase_of(rig, batch="erase2")
    with pytest.raises(burnjob.BurnError):
        run_the_erase(rig, request, burn_id, Handle(), batch="erase2")
    assert dropped == ["forgotten", "forgotten"]


def test_the_drive_counts_as_in_use_while_it_is_being_erased(rig, recorder) -> None:
    a_written_disc(rig)
    request, burn_id = erase_of(rig)
    seen: list[int] = []
    recorder.when["erase"] = lambda: seen.append(disc_helper._in_use)
    run_the_erase(rig, request, burn_id, Handle())
    assert seen == [1] and disc_helper._in_use == 0


# ---------------------------------------------------------------------------------------- the routes, at the moments the page can meet them


def asked_for(rig, kind: str, **changes) -> dict:
    """The request a page makes for this kind of write (an image has no drive to eject)."""
    asked = ask(rig, **changes)
    return asked if kind == "data" else {k: v for k, v in asked.items() if k not in ("drive", "eject")}


@pytest.mark.parametrize("kind", ["data", "iso"])
def test_nothing_is_previewed_or_started_while_other_disc_work_is_going_on_and_what_was_shown_is_kept(rig, kind: str) -> None:
    asked = asked_for(rig, kind)
    assert rig.client.post(f"/api/discs/{kind}/preview", json=asked).status_code == 200
    release = threading.Event()
    blocker = rig.ctx.jobs.submit("erase", "Erasing", lambda _handle: (release.wait(30), {})[1], lane="disc")
    try:
        started = rig.client.post(f"/api/discs/{kind}/start", json=asked)
        assert started.status_code == 409 and started.json()["detail"] == "A disc is being erased already."
        previewed = rig.client.post(f"/api/discs/{kind}/preview", json=asked)
        assert previewed.status_code == 409 and previewed.json()["detail"] == "A disc is being erased right now. Wait for it to finish, or stop it."
    finally:
        release.set()
        rig.ctx.jobs.wait(blocker.id, timeout=30)
    assert rig.client.get("/api/discs/burns").json()["burns"] == []
    again = rig.client.post(f"/api/discs/{kind}/start", json=asked)  # (the refusal did not use up what was shown)
    assert again.status_code == 200, again.text
    assert rig.ctx.jobs.wait(again.json()["job"]["id"], timeout=60).status == "done"


@pytest.mark.parametrize("kind", ["data", "iso"])
def test_what_was_shown_is_used_up_by_the_write_it_allowed(rig, kind: str) -> None:
    asked = asked_for(rig, kind)
    assert rig.client.post(f"/api/discs/{kind}/preview", json=asked).status_code == 200
    first = rig.client.post(f"/api/discs/{kind}/start", json=asked)
    assert first.status_code == 200, first.text
    assert rig.ctx.jobs.wait(first.json()["job"]["id"], timeout=60).status == "done"
    add_drive(rig.world, "E", writer=True, disc=blank_disc(type="DVD+R", capacity=DVD))  # a new blank disc, the same files
    again = rig.client.post(f"/api/discs/{kind}/start", json=asked)
    assert again.status_code == 409 and again.json()["detail"].startswith("Look at the list first.")
    assert len(rig.client.get("/api/discs/burns").json()["burns"]) == 1


def test_a_speed_the_disc_put_in_after_the_preview_cannot_be_written_at_is_refused_at_the_start(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    asked = ask(rig, speed=75 * 8)
    assert rig.client.post("/api/discs/data/preview", json=asked).status_code == 200
    real = routes._media_of
    monkeypatch.setattr(routes, "_media_of", lambda drive: {**real(drive), "speeds": [{"speed": 75 * 4, "label": "4x"}]})  # (another disc: slower writing only)
    refused = rig.client.post("/api/discs/data/start", json=asked)
    assert refused.status_code == 422 and refused.json()["detail"] == "This drive cannot write at that speed."
    assert rig.client.get("/api/discs/burns").json()["burns"] == []


def test_the_speed_chosen_on_the_page_reaches_the_recorder_and_the_history(rig, recorder) -> None:
    job, shown, _ = finish(rig, "data", speed=75 * 8)
    assert job.status == "done", job.error
    assert recorder.asked["burn_data"]["speed"] == 600 and shown["speed"] == 600


@pytest.mark.parametrize("kind", ["data", "iso"])
def test_a_write_does_not_wait_for_other_work_in_the_library_lane(rig, kind: str) -> None:
    release = threading.Event()
    blocker = rig.ctx.jobs.submit("tags", "Writing tags", lambda _handle: (release.wait(120), {})[1])  # (the default lane: anything that writes the library)
    try:
        started = begin(rig, kind)
        job = rig.ctx.jobs.wait(started["job"]["id"], timeout=15)
        assert job.status == "done", (job.status, job.error)
    finally:
        release.set()
        rig.ctx.jobs.wait(blocker.id, timeout=30)


@pytest.mark.parametrize(("kind", "words", "begun"), [("iso", "made into an image", "Making the image"), ("data", "burned", "Writing to the disc")])
def test_the_drives_are_in_use_while_files_are_burned_or_an_image_is_made_and_the_refusal_says_which(rig, kind: str, words: str, begun: str) -> None:
    set_control(rig.world, burn_slow=1.0)
    started = begin(rig, kind)
    job_id = started["job"]["id"]
    wait_until(rig, job_id, begun)
    try:
        refused = rig.client.get("/api/discs/media/E:")
        assert refused.status_code == 409 and refused.json()["detail"] == f"A disc is being {words} right now. Wait for it to finish, or stop it."
    finally:
        rig.client.post(f"/api/jobs/{job_id}/cancel")
        rig.ctx.jobs.wait(job_id, timeout=30)


@pytest.mark.parametrize("kind", ["data", "iso"])
def test_the_title_given_on_the_page_names_the_job_and_the_history(rig, kind: str) -> None:
    job, shown, _ = finish(rig, kind, title="  My road mix ")
    assert job.status == "done", job.error
    assert job.title == "My road mix" and shown["title"] == "My road mix"
    add_drive(rig.world, "E", writer=True, disc=blank_disc(type="DVD+R", capacity=DVD))  # (a new blank disc for the next one)
    job, shown, _ = finish(rig, kind, title="")
    assert job.title == shown["title"] and job.title.startswith("Data disc Road Trip" if kind == "data" else "Disc image Road Trip")


@pytest.mark.parametrize("kind", ["data", "iso"])
def test_nothing_is_started_when_every_file_has_gone_since_the_list_was_shown_and_the_refusal_says_why(rig, kind: str) -> None:
    asked = asked_for(rig, kind)
    shown = rig.client.post(f"/api/discs/{kind}/preview", json=asked)
    assert shown.status_code == 200 and shown.json()["problems"] == []
    for relative in (ONE, TWO, THREE):
        (rig.music / relative).unlink()
    refused = rig.client.post(f"/api/discs/{kind}/start", json=asked)
    assert refused.status_code == 409 and refused.json()["detail"] == rig.client.post(f"/api/discs/{kind}/preview", json=asked).json()["problems"][0]
    assert rig.client.get("/api/discs/burns").json()["burns"] == []


FULL_PATH = "Give the full path of a folder, like C:\\Users\\you\\Downloads."


@pytest.mark.parametrize("folder", ["relative/folder", "..", "\\\\.\\PhysicalDrive0", "\\\\?\\C:\\x"], ids=["relative", "parent", "device", "extended"])
def test_a_folder_for_an_image_that_is_not_a_full_path_is_refused_in_the_preview_and_the_start(rig, folder: str) -> None:
    asked = {**asked_for(rig, "iso"), "folder": folder}
    previewed = rig.client.post("/api/discs/iso/preview", json=asked)
    assert previewed.status_code == 422 and previewed.json()["detail"] == FULL_PATH
    started = rig.client.post("/api/discs/iso/start", json=asked)
    assert started.status_code == 409  # (nothing was shown for it: the preview was refused)


def test_a_folder_for_an_image_that_is_not_there_is_refused_and_one_that_is_there_is_used(rig) -> None:
    asked = {**asked_for(rig, "iso"), "folder": str(rig.tmp / "no" / "such" / "folder")}
    refused = rig.client.post("/api/discs/iso/preview", json=asked)
    assert refused.status_code == 422 and refused.json()["detail"] == "That folder is not there."
    chosen = rig.tmp / "images"
    chosen.mkdir()
    asked = {**asked_for(rig, "iso"), "folder": f"  {chosen}  "}  # (spaces round it are not part of the name)
    shown = rig.client.post("/api/discs/iso/preview", json=asked)
    assert shown.status_code == 200 and shown.json()["folder"] == str(chosen) and shown.json()["output"] == str(chosen / "Road Trip.iso")
    started = rig.client.post("/api/discs/iso/start", json=asked)
    assert started.status_code == 200, started.text
    assert rig.ctx.jobs.wait(started.json()["job"]["id"], timeout=60).status == "done"
    assert (chosen / "Road Trip.iso").is_file()


def test_a_song_is_placed_relative_to_the_library_folder_it_is_in_and_by_its_name_alone_when_it_is_in_none(rig) -> None:
    second = rig.tmp / "more music"
    make_track(second / "Artist C" / "Album 3", "01 Four.mp3", title="Four", artist="Artist C", album="Album 3", tracknumber=1)
    rig.ctx.config.library.roots = [str(rig.music), str(second)]
    scan(rig.web)
    asked = {**ask(rig), "track_ids": [rig.ids["One"], ids_by_title(rig.client)["Four"]]}
    shown = rig.client.post("/api/discs/data/preview", json=asked).json()
    assert [f["dest"] for f in shown["files"]] == ["Artist A/Album 1/01 One.mp3", "Artist C/Album 3/01 Four.mp3"]
    rig.ctx.config.library.roots = [str(rig.music)]  # (the folder the second song is in is not a library folder any more)
    shown = rig.client.post("/api/discs/data/preview", json=asked).json()
    assert [f["dest"] for f in shown["files"]] == ["Artist A/Album 1/01 One.mp3", "01 Four.mp3"]


# ---------------------------------------------------------------------------------------- when something goes wrong in the middle


def test_a_refusal_from_the_recorder_is_passed_on_with_its_code_and_with_what_became_of_the_disc(rig, recorder) -> None:
    plan, request, burn_id = prepared(rig)

    def refuse() -> None:
        raise DiscError("busy", "The drive is busy with something else.")

    recorder.when["burn_data"] = refuse
    with pytest.raises(DiscError) as caught:
        run(rig, plan, request, burn_id, Handle())
    words = "The drive is busy with something else. Nothing had been written, so the disc is as it was."
    assert (caught.value.code, str(caught.value)) == ("busy", words)
    shown = history(rig, burn_id)
    assert (shown["status"], shown["message"], shown["spoiled"]) == ("failed", words, 0)


@pytest.mark.parametrize(
    ("where", "tail"),
    [("check_sources", "Nothing had been written, so the disc is as it was."), ("verify_files", "The files had been written, but they were not all checked.")],
)
def test_a_slip_in_the_code_is_said_as_it_is_with_what_became_of_the_disc_and_a_finished_disc_is_not_spoiled(rig, monkeypatch: pytest.MonkeyPatch, where: str, tail: str) -> None:
    def slip(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(datajob if where == "check_sources" else datadisc, where, slip)
    plan, request, burn_id = prepared(rig)
    with pytest.raises(burnjob.BurnError) as caught:
        run(rig, plan, request, burn_id, Handle())
    words = f"RuntimeError: boom. {tail}"
    assert str(caught.value) == words
    shown = history(rig, burn_id)
    assert (shown["status"], shown["message"], shown["spoiled"]) == ("failed", words, 0)


@pytest.mark.parametrize("full", [False, True])
def test_the_kind_of_erase_chosen_on_the_page_reaches_the_recorder(rig, recorder, full: bool) -> None:
    a_written_disc(rig)
    started = rig.client.post("/api/discs/erase", json={"drive": "E:", "full": full, "confirm": True})
    assert started.status_code == 200, started.text
    assert rig.ctx.jobs.wait(started.json()["job"]["id"], timeout=30).status == "done"
    assert recorder.asked["erase"]["full"] is full


@pytest.mark.parametrize("fails", [False, True])
def test_the_remembered_list_of_drives_is_dropped_after_a_data_burn_however_it_ends(rig, monkeypatch: pytest.MonkeyPatch, fails: bool) -> None:
    dropped: list[str] = []
    monkeypatch.setattr(disc_helper, "forget_listing", lambda: dropped.append("forgotten"))
    if fails:
        set_control(rig.world, write_protected=True)
    plan, request, burn_id = prepared(rig)
    if fails:
        with pytest.raises(DiscError):
            run(rig, plan, request, burn_id, Handle())
    else:
        run(rig, plan, request, burn_id, Handle())
    assert dropped == ["forgotten"]


def test_an_erase_does_not_wait_for_other_work_in_the_library_lane(rig) -> None:
    a_written_disc(rig)
    release = threading.Event()
    blocker = rig.ctx.jobs.submit("tags", "Writing tags", lambda _handle: (release.wait(120), {})[1])
    try:
        started = rig.client.post("/api/discs/erase", json={"drive": "E:", "confirm": True})
        assert started.status_code == 200, started.text
        job = rig.ctx.jobs.wait(started.json()["job"]["id"], timeout=15)
        assert job.status == "done", (job.status, job.error)
    finally:
        release.set()
        rig.ctx.jobs.wait(blocker.id, timeout=30)


def test_the_media_of_a_disc_that_has_been_written_says_why_files_cannot_go_on_it(rig) -> None:
    a_written_disc(rig, "CD-R", rewritable=False)
    got = media_of(rig)
    assert got["data_problems"] == ["This CD-R is not blank, and a disc that has been written cannot be used again. Put in a blank disc."]
    assert got["audio_problems"] and got["blank"] is False


def test_a_stop_pressed_while_waiting_for_the_files_to_show_ends_the_wait_and_says_the_disc_had_been_written(rig, recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(datajob, "MOUNT_WAIT", 3.0)
    monkeypatch.setattr(datajob, "MOUNT_LOOK", 0.02)
    set_control(rig.world, no_root=True)  # (the files never show)
    plan, request, burn_id = prepared(rig)
    handle = Handle()
    looks: list[int] = []

    def pressed_after_a_few_looks() -> None:
        looks.append(1)
        if len(looks) >= 3:  # (the first look is before the writing, the others are the wait)
            handle.cancelled = True

    recorder.when["media"] = pressed_after_a_few_looks
    with pytest.raises(JobCancelled):
        run(rig, plan, request, burn_id, handle)
    shown = history(rig, burn_id)
    assert (shown["status"], shown["spoiled"], shown["message"]) == ("cancelled", 0, "Stopped. The files had been written, but they were not all checked.")
