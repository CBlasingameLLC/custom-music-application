"""The corners of a burn: what the job does when Stop is pressed at each point of the preparation, what it tells the history, and what the
store and the routes keep. (tests/test_web_discs_burn.py has the main paths; these call the job directly where a test has to act at one
chosen moment.)"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from musictoolkit.discs import burn, burnjob, store
from musictoolkit.discs.burn import SongInput
from musictoolkit.discs.helper import DiscError
from musictoolkit.web.jobs import JobCancelled
from tests.conftest import ids_by_title
from tests.fake_disc_helper import add_drive, audio_disc, blank_disc, set_control
from tests import test_web_discs_burn as base
from tests.test_web_discs_burn import SCHEME, SONGS, _wait_until_writing, ask, begin, burn_it, media, preview, scratch

rig = base.rig  # (the same stand-in recorder, library and app as the main tests of burning)

STOPPED = "Stopped. Nothing had been written, so the disc is as it was."


class Handle:
    """What the job runner gives a job, for calling a job directly: remembers what it was told, and raises the stop when asked."""

    def __init__(self) -> None:
        self.cancelled = False
        self.updates: list[dict] = []
        self.logs: list[str] = []

    def check(self) -> None:
        if self.cancelled:
            raise JobCancelled()

    def update(self, **fields) -> None:
        self.updates.append(fields)

    def log(self, line: str) -> None:
        self.logs.append(line)

    def stop(self) -> None:
        self.cancelled = True


def songs_of(rig, *titles: str) -> list[SongInput]:
    return [SongInput(str(rig.music / "Burner" / "Mix" / f"{t}.flac"), t, "Burner", rig.ids[t]) for t in (titles or [s[0] for s in SONGS])]


def prepared(rig, *titles: str, batch: str = "more"):
    """A plan, a request and a history row for these songs, ready for burnjob.run_audio."""
    songs = songs_of(rig, *titles)
    found = media(rig)
    plan = burn.plan_audio(songs, found, scratch(rig))
    assert plan.problems == []
    request = burnjob.AudioRequest(drive="E:", eject=False)
    with rig.ctx.db() as conn:
        burn_id = burnjob.begin_audio(conn, plan, request, batch, found)
    return plan, request, burn_id


def run(rig, plan, request, burn_id, handle, batch: str = "more"):
    return burnjob.run_audio(db=rig.ctx.db, plan=plan, request=request, batch_id=batch, burn_id=burn_id, scratch=scratch(rig), handle=handle)


def history(rig, burn_id: int) -> dict:
    with rig.ctx.db() as conn:
        return store.get_burn(conn, burn_id)


@pytest.fixture
def decodes(monkeypatch: pytest.MonkeyPatch):
    """Notes each song the job starts to convert and each it finishes, and lets a test act just before or just after one."""
    note = SimpleNamespace(started=[], finished=[], before=None, after=None)
    real = burn.decode_to_pcm

    def spying(source, target, **kwargs):
        note.started.append(source)
        if note.before:
            note.before(len(note.started))
        sectors = real(source, target, **kwargs)
        note.finished.append(source)
        if note.after:
            note.after(len(note.finished))
        return sectors

    monkeypatch.setattr(burn, "decode_to_pcm", spying)
    return note


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch):
    """Notes how often the job opens the recorder and what it asks of it, and lets a test act when a command arrives."""
    note = SimpleNamespace(opened=0, commands=[], when={})
    real = burnjob.DiscHelper

    class Watching(real):
        def __init__(self, *args, **kwargs) -> None:
            note.opened += 1
            super().__init__(*args, **kwargs)

        def call(self, cmd, **kwargs):
            note.commands.append(cmd)
            if cmd in note.when:
                note.when[cmd]()
            return super().call(cmd, **kwargs)

    monkeypatch.setattr(burnjob, "DiscHelper", Watching)
    return note


# ---------------------------------------------------------------------------------------- a stop at each point of the preparation


def test_a_stop_pressed_while_a_song_is_being_converted_cuts_that_conversion_short(rig, decodes, recorder) -> None:
    plan, request, burn_id = prepared(rig)
    handle = Handle()
    decodes.before = lambda n: handle.stop() if n == 1 else None  # Stop is pressed as the first song starts
    with pytest.raises(JobCancelled):
        run(rig, plan, request, burn_id, handle)
    assert len(decodes.started) == 1 and decodes.finished == [], "the conversion noticed the stop itself, instead of finishing the song first"
    assert recorder.opened == 0
    shown = history(rig, burn_id)
    assert (shown["status"], shown["spoiled"], shown["message"]) == ("cancelled", 0, STOPPED)
    assert not scratch(rig).exists()


def test_a_stop_pressed_between_two_songs_converts_no_more_songs(rig, decodes, recorder) -> None:
    plan, request, burn_id = prepared(rig)
    handle = Handle()
    decodes.after = lambda n: handle.stop() if n == 1 else None  # Stop is pressed when the first song is done
    with pytest.raises(JobCancelled):
        run(rig, plan, request, burn_id, handle)
    assert len(decodes.started) == 1, "the second song was not started"
    assert recorder.opened == 0
    assert history(rig, burn_id)["message"] == STOPPED


def test_a_stop_pressed_when_the_last_song_is_converted_never_opens_the_recorder(rig, decodes, recorder) -> None:
    plan, request, burn_id = prepared(rig)
    handle = Handle()
    decodes.after = lambda n: handle.stop() if n == len(SONGS) else None
    with pytest.raises(JobCancelled):
        run(rig, plan, request, burn_id, handle)
    assert len(decodes.finished) == len(SONGS)
    assert recorder.opened == 0 and recorder.commands == []
    assert history(rig, burn_id)["message"] == STOPPED


def test_a_stop_pressed_after_the_disc_was_looked_at_is_the_last_chance_to_leave_it_alone(rig, decodes, recorder) -> None:
    plan, request, burn_id = prepared(rig)
    handle = Handle()
    recorder.when["media"] = handle.stop
    with pytest.raises(JobCancelled):
        run(rig, plan, request, burn_id, handle)
    assert recorder.commands == ["media"], "the recorder was not asked to write"
    shown = history(rig, burn_id)
    assert (shown["status"], shown["spoiled"], shown["message"]) == ("cancelled", 0, STOPPED)
    assert media(rig)["blank"] is True and not (rig.world / "E" / "disc.pcm").exists()


# ---------------------------------------------------------------------------------------- what the job reports


def test_the_progress_names_the_song_being_prepared_and_only_moves_forward(rig) -> None:
    plan, request, burn_id = prepared(rig)
    handle = Handle()
    result = run(rig, plan, request, burn_id, handle)
    preparing = [u for u in handle.updates if str(u.get("message", "")).startswith("Preparing song")]
    names = [u["message"].split(" (")[0] for u in preparing]
    assert names[0] == "Preparing song 1 of 3: One" and "Preparing song 2 of 3: Two" in names and "Preparing song 3 of 3: Three" in names
    second = next(u for u in preparing if u["message"] == "Preparing song 2 of 3: Two")
    assert second["done"] == 375, "the first song's sectors are counted as done when the second begins"
    third = next(u for u in preparing if u["message"] == "Preparing song 3 of 3: Three")
    assert third["done"] == 375 + 450
    done = [u["done"] for u in handle.updates if "done" in u]
    assert done == sorted(done), "the progress never goes backwards"
    last = handle.updates[-1]
    assert (last["done"], last["message"]) == (last["total"], "Finishing")
    assert last["total"] == 375 + 450 + 400 + plan.disc_sectors  # (the converting counts as work, then the writing)
    assert result["status"] == "done" and result["songs"] == 3 and result["ejected"] is False
    assert result["sectors"] == plan.disc_sectors


def test_the_job_result_says_how_it_ended_and_what_was_written(rig) -> None:
    job, shown = burn_it(rig)
    assert job.status == "done", job.error
    assert job.result["status"] == "done" and job.result["songs"] == 3 and job.result["burn_id"] == shown["id"]
    assert job.result["sectors"] == 375 + 450 + 400 + 300 and shown["sectors"] == job.result["sectors"]


def test_a_conversion_that_came_out_longer_than_planned_is_caught_by_the_fit_check(rig, monkeypatch: pytest.MonkeyPatch, recorder) -> None:
    real = burn.decode_to_pcm
    monkeypatch.setattr(burn, "decode_to_pcm", lambda source, target, **kw: real(source, target, **kw) + 400_000)
    plan, request, burn_id = prepared(rig)
    with pytest.raises(burn.BurnError, match="Take out about"):
        run(rig, plan, request, burn_id, Handle())
    assert recorder.commands == ["media"], "the recorder was never asked to write"
    shown = history(rig, burn_id)
    assert shown["status"] == "failed" and shown["spoiled"] == 0 and "Take out about" in shown["message"]


def test_the_history_keeps_the_sectors_that_were_written_not_the_ones_that_were_planned(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = burn.decode_to_pcm
    monkeypatch.setattr(burn, "decode_to_pcm", lambda source, target, **kw: real(source, target, **kw) + 10)  # (each song comes out a little longer)
    plan, request, burn_id = prepared(rig)
    run(rig, plan, request, burn_id, Handle())
    assert history(rig, burn_id)["sectors"] == plan.disc_sectors + 30


def test_a_burn_that_fails_before_writing_keeps_the_planned_size_in_the_history(rig) -> None:
    set_control(rig.world, write_protected=True)
    job, shown = burn_it(rig)
    assert job.status == "error"
    assert (shown["status"], shown["spoiled"], shown["sectors"]) == ("failed", 0, 375 + 450 + 400 + 300)


def test_a_refusal_from_the_recorder_reaches_the_caller_with_its_code_and_marks_the_disc(rig) -> None:
    set_control(rig.world, burn_fail_at=120)
    plan, request, burn_id = prepared(rig)
    with pytest.raises(DiscError) as caught:
        run(rig, plan, request, burn_id, Handle())
    assert caught.value.code == "underrun"
    assert "A CD-R cannot be written twice; put in a new disc." in str(caught.value)
    shown = history(rig, burn_id)
    assert (shown["status"], shown["spoiled"]) == ("failed", 1) and shown["message"] == str(caught.value)


# ---------------------------------------------------------------------------------------- the watcher that carries a stop to the recorder


def test_the_watcher_keeps_passing_a_stop_on_until_it_is_told_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(burnjob, "WATCH_INTERVAL", 0.01)
    calls: list[float] = []
    helper = SimpleNamespace(cancel=lambda: calls.append(time.monotonic()))
    ended = threading.Event()
    thread = threading.Thread(target=burnjob._pass_on_cancel, args=(SimpleNamespace(cancelled=True), helper, ended), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) >= 3, "a stop that was not heard the first time is passed on again"
    ended.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_the_watcher_does_nothing_while_nobody_has_pressed_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(burnjob, "WATCH_INTERVAL", 0.01)
    calls: list[int] = []
    ended = threading.Event()
    thread = threading.Thread(target=burnjob._pass_on_cancel, args=(SimpleNamespace(cancelled=False), SimpleNamespace(cancel=lambda: calls.append(1)), ended), daemon=True)
    thread.start()
    time.sleep(0.2)
    ended.set()
    thread.join(timeout=5)
    assert calls == []


def test_no_watcher_is_left_running_after_a_burn(rig) -> None:
    job, _ = burn_it(rig)
    assert job.status == "done", job.error
    assert [t.name for t in threading.enumerate() if t.name == "disc-stop-watcher"] == []


# ---------------------------------------------------------------------------------------- what the history is given to begin with


def test_a_burn_without_a_title_is_named_after_its_songs_and_a_given_title_is_trimmed() -> None:
    plan = SimpleNamespace(burnable=[1, 2, 3])
    assert burnjob.title_of(burnjob.AudioRequest(drive="E:", title="   "), plan) == "Music CD of 3 songs"
    assert burnjob.title_of(burnjob.AudioRequest(drive="E:"), SimpleNamespace(burnable=[1])) == "Music CD of 1 song"
    assert burnjob.title_of(burnjob.AudioRequest(drive="E:", title="  My mix \n"), plan) == "My mix"


def test_the_history_row_of_a_song_keeps_where_it_came_from_and_a_title_even_when_the_song_has_none(rig) -> None:
    path = str(rig.music / "Burner" / "Mix" / "One.flac")
    songs = [SongInput(path, "", "Burner", None), SongInput(str(rig.music / "Burner" / "Mix" / "Two.flac"), "Two", "Burner", rig.ids["Two"])]
    found = media(rig)
    plan = burn.plan_audio(songs, found, scratch(rig))
    request = burnjob.AudioRequest(drive="E:", eject=False)
    with rig.ctx.db() as conn:
        burn_id = burnjob.begin_audio(conn, plan, request, "titles", found)
    shown = history(rig, burn_id)
    assert [(t["title"], t["source"], t["track_id"], t["sectors"]) for t in shown["tracks"]] == [
        ("One", path, None, 375), ("Two", songs[1].path, rig.ids["Two"], 450),
    ]
    assert shown["sectors"] == plan.disc_sectors and shown["items"] == 2 and shown["disc_type"] == "CD-R"


def add_a_song_that_cannot_be_burned(rig) -> None:
    wma = rig.music / "Burner" / "Mix" / "Four.wma"
    wma.write_bytes(b"not audio")
    with rig.ctx.db() as conn:
        conn.execute("INSERT INTO tracks (file_path, title, artist) VALUES (?, 'Four', 'Burner')", (str(wma),))
        conn.commit()
    rig.ids = ids_by_title(rig.client)


def test_the_history_counts_only_the_songs_that_will_be_written_and_a_song_left_out_stays_left_out(rig) -> None:
    add_a_song_that_cannot_be_burned(rig)
    set_control(rig.world, write_protected=True)  # the burn fails, so every song that was waiting is marked "not written"
    job, shown = burn_it(rig, "One", "Four", "Two")
    assert job.status == "error"
    assert shown["items"] == 2
    assert [(t["title"], t["status"]) for t in shown["tracks"]] == [("One", "failed"), ("Four", "skipped"), ("Two", "failed")]
    assert shown["tracks"][0]["message"] == "Not written."
    assert shown["tracks"][1]["message"].startswith("WMA songs cannot be burned"), "the reason a song was left out is not overwritten"


def test_burns_are_listed_newest_first(rig) -> None:
    first, _ = burn_it(rig, "One")
    assert first.status == "done"
    add_drive(rig.world, "E", writer=True, disc=blank_disc())
    second, _ = burn_it(rig, "Two")
    assert second.status == "done"
    listed = rig.client.get("/api/discs/burns").json()["burns"]
    assert len(listed) == 2 and listed[0]["id"] > listed[1]["id"]


# ---------------------------------------------------------------------------------------- what the media route shows


def test_the_media_of_a_written_data_disc_does_not_show_where_its_files_are_read_from(rig) -> None:
    add_drive(rig.world, "E", writer=True, disc={"kind": "data", "label": "OLD", "seed": 0, "type": "CD-RW", "rewritable": True, "capacity": 359846,
                                                 "tracks": [{"number": 1, "audio": False, "sectors": 100}]})
    got = media(rig)
    assert got["state"] == "written" and got["rewritable"] is True
    assert "root" not in got and "ok" not in got


def test_a_disc_with_music_on_it_says_it_cannot_take_more(rig) -> None:
    add_drive(rig.world, "E", writer=True, disc={**audio_disc(seed=2, tracks=(60,)), "type": "CD-RW", "rewritable": True, "capacity": 359846})
    got = media(rig)
    assert got["audio_problems"] == ["This CD-RW already has something on it. Erase it first (Erase, on this screen), or put in a blank disc."]


# ---------------------------------------------------------------------------------------- where the work is done


def test_the_room_for_the_temporary_songs_is_looked_for_where_they_will_be_made(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []
    real = burn._scratch_free
    monkeypatch.setattr(burn, "_scratch_free", lambda folder: seen.append(Path(folder)) or real(folder))
    assert preview(rig).status_code == 200
    assert seen == [scratch(rig)]


def test_a_burn_runs_in_the_disc_lane_so_that_it_waits_for_disc_work_and_not_for_library_work(rig) -> None:
    started = begin(rig)
    assert rig.ctx.jobs.wait(started["job"]["id"], timeout=60).status == "done"
    assert "disc" in rig.ctx.jobs._workers


def test_a_request_that_makes_no_sense_to_the_planner_is_refused_in_its_own_words(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):
        raise ValueError("Choose some songs first.")

    monkeypatch.setattr(burn, "plan_audio", refuse)
    got = preview(rig)
    assert got.status_code == 422 and got.json()["detail"] == "Choose some songs first."


def test_a_rip_cannot_start_while_a_burn_has_the_drives_and_the_refusal_names_the_burn(rig) -> None:
    asked = {
        "drive": "F:", "root": str(rig.music), "scheme": SCHEME, "format": "flac", "quality": "standard", "accuracy": "fast", "album": "A", "album_artist": "B",
        "tracks": [{"number": 1, "title": "x"}, {"number": 2, "title": "y"}], "cover": False, "eject": False,
    }
    assert rig.client.post("/api/discs/rip/preview", json=asked).status_code == 200  # (the rip was looked at before the burn began)
    set_control(rig.world, burn_slow=0.004)
    job_id = begin(rig)["job"]["id"]
    _wait_until_writing(rig, job_id)
    try:
        refused = rig.client.post("/api/discs/rip/start", json=asked)
        assert refused.status_code == 409 and refused.json()["detail"] == "A disc is being burned already."
    finally:
        rig.client.post(f"/api/jobs/{job_id}/cancel")
        rig.ctx.jobs.wait(job_id, timeout=30)


def test_a_drive_the_helper_does_not_list_is_not_asked_about_its_disc(rig) -> None:
    set_control(rig.world, unlisted=["E"])  # (a careless helper still answers for it)
    assert rig.client.get("/api/discs/media/E:").status_code == 404


def test_a_burn_cannot_start_while_other_disc_work_is_going_on_and_the_refusal_names_it(rig) -> None:
    asked = ask(rig)
    assert rig.client.post("/api/discs/burn/preview", json=asked).status_code == 200
    release = threading.Event()
    blocker = rig.ctx.jobs.submit("erase", "Erasing", lambda _handle: (release.wait(30), {})[1], lane="disc")
    try:
        refused = rig.client.post("/api/discs/burn/start", json=asked)
        assert refused.status_code == 409 and refused.json()["detail"] == "A disc is being erased already."
    finally:
        release.set()
        rig.ctx.jobs.wait(blocker.id, timeout=30)
    assert rig.client.get("/api/discs/burns").json()["burns"] == []


def test_a_preview_is_used_up_by_the_burn_it_allowed(rig) -> None:
    asked = ask(rig)
    started = begin(rig)
    assert rig.ctx.jobs.wait(started["job"]["id"], timeout=60).status == "done"
    add_drive(rig.world, "E", writer=True, disc=blank_disc())  # a new blank disc, the same songs
    again = rig.client.post("/api/discs/burn/start", json=asked)
    assert again.status_code == 409 and again.json()["detail"].startswith("Look at the list of songs first.")
    assert len(rig.client.get("/api/discs/burns").json()["burns"]) == 1


def test_a_burn_goes_to_the_drive_that_was_asked_for(rig) -> None:
    add_drive(rig.world, "G", name="Second recorder", writer=True, disc=blank_disc())
    job, shown = burn_it(rig, drive="G:")
    assert job.status == "done", job.error
    assert shown["drive"] == "G:"
    assert json.loads((rig.world / "G" / "disc.json").read_text())["kind"] == "audio"
    assert json.loads((rig.world / "E" / "disc.json").read_text())["kind"] == "blank"
