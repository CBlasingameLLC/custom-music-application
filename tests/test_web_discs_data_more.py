"""The corners of writing files to a disc: what the job asks of the recorder, what it reports while it works, and what it tells the history when it is
stopped at one point or another. (tests/test_web_discs_data.py has the main paths; these call the job directly where a test has to act at one
chosen moment.)"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from musictoolkit.discs import burnjob, datadisc, datajob, store
from musictoolkit.discs import helper as disc_helper
from musictoolkit.discs.helper import DiscError
from musictoolkit.web.jobs import JobCancelled
from tests import test_web_discs_data as base
from tests.fake_disc_helper import add_drive, blank_disc, set_control
from tests.test_web_discs_data import DVD, ONE, THREE, TWO, Handle, begin, data_songs, finish, wait_until

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
