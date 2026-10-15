"""Burning files onto a data disc, making an ISO image and erasing a rewritable disc, end to end: the real routes, the real helper protocol and a
stand-in recorder (tests/fake_disc_helper.py). A data disc is read back and compared, the way the app does it."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from musictoolkit.discs import burnjob, datadisc, datajob, store
from musictoolkit.discs import helper as disc_helper
from musictoolkit.discs.helper import DiscError
from tests.conftest import build_web, ids_by_title, make_track, scan
from tests.fake_disc_helper import add_drive, audio_disc, blank_disc, set_control

FAKE = Path(__file__).parent / "fake_disc_helper.py"
DVD = 2295104  # sectors on a blank DVD+R
SONGS = [("One", "Artist A", "Album 1"), ("Two", "Artist A", "Album 1"), ("Three", "Artist B", "Album 2")]


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    world = tmp_path / "drives"
    add_drive(world, "E", writer=True, disc=blank_disc(type="DVD+R", capacity=DVD))
    add_drive(world, "F", writer=False)
    music = tmp_path / "music"
    for number, (title, artist, album) in enumerate(SONGS, start=1):
        make_track(music / artist / album, f"{number:02d} {title}.mp3", title=title, artist=artist, album=album, tracknumber=number)
    web = build_web(tmp_path, music)
    scan(web)
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("MTK_DISC_HELPER", json.dumps([sys.executable, str(FAKE), str(world)]))
    disc_helper.forget_listing()
    yield SimpleNamespace(web=web, client=web.client, ctx=web.ctx, config=web.config, world=world, music=music, tmp=tmp_path, home=home, ids=ids_by_title(web.client))
    disc_helper.forget_listing()


def ask(rig, *titles: str, **changes) -> dict:
    return {"drive": "E:", "track_ids": [rig.ids[t] for t in (titles or [s[0] for s in SONGS])], "eject": False, "label": "Road Trip", **changes}


def begin(rig, kind: str, *titles: str, **changes) -> dict:
    asked = ask(rig, *titles, **changes) if kind == "data" else {k: v for k, v in ask(rig, *titles, **changes).items() if k not in ("drive", "eject")}
    shown = rig.client.post(f"/api/discs/{kind}/preview", json=asked)
    assert shown.status_code == 200, shown.text
    started = rig.client.post(f"/api/discs/{kind}/start", json=asked)
    assert started.status_code == 200, started.text
    return started.json()


def finish(rig, kind: str, *titles: str, **changes):
    started = begin(rig, kind, *titles, **changes)
    job = rig.ctx.jobs.wait(started["job"]["id"], timeout=60)
    return job, rig.client.get(f"/api/discs/burns/{started['burn_id']}").json(), started


def disc_files(rig) -> dict[str, bytes]:
    root = rig.world / "E" / "files"
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def original(rig, relative: str) -> bytes:
    return (rig.music / relative).read_bytes()


def wait_until(rig, job_id: str, words: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if str(rig.ctx.jobs.get(job_id).message or "").startswith(words):
            return
        time.sleep(0.02)
    raise AssertionError(f"never got to {words!r}: {rig.ctx.jobs.get(job_id).to_dict()}")


# ---------------------------------------------------------------------------------------- the options and the media


def test_the_options_say_what_data_discs_start_with_and_where_images_go(rig) -> None:
    got = rig.client.get("/api/discs/options").json()
    assert got["burn"] == {"eject": True, "verify": True} and got["iso_folder"] == str(rig.home / "Downloads")
    assert rig.client.put("/api/settings", json={"disc": {"burn_verify": False}}).json()["disc"]["burn_verify"] is False
    assert rig.client.get("/api/discs/options").json()["burn"]["verify"] is False


def test_without_a_downloads_folder_images_go_to_the_home_folder(rig) -> None:
    (rig.home / "Downloads").rmdir()
    assert rig.client.get("/api/discs/options").json()["iso_folder"] == str(rig.home)


def test_the_media_of_a_dvd_says_why_music_cannot_go_on_it_but_files_can(rig) -> None:
    got = rig.client.get("/api/discs/media/E:").json()
    assert (got["type"], got["blank"], got["can_audio"], got["free_sectors"]) == ("DVD+R", True, False, DVD)
    assert got["audio_problems"] == ["A music CD can only be made on a blank CD-R or CD-RW, and this is a DVD+R."] and got["data_problems"] == []


# ---------------------------------------------------------------------------------------- looking before burning


def test_the_preview_says_where_every_file_goes_and_what_it_takes(rig) -> None:
    shown = rig.client.post("/api/discs/data/preview", json=ask(rig)).json()
    assert [(f["position"], f["title"], f["dest"], f["problem"]) for f in shown["files"]] == [
        (1, "One", "Artist A/Album 1/01 One.mp3", ""), (2, "Two", "Artist A/Album 1/02 Two.mp3", ""), (3, "Three", "Artist B/Album 2/03 Three.mp3", ""),
    ]
    assert shown["label"] == "Road Trip" and shown["label_iso"] == "ROAD_TRIP" and shown["filesystems"] == ["iso9660", "joliet", "udf"]
    assert shown["ok"] is True and shown["problems"] == [] and shown["counts"] == {"going": 3, "left_out": 0}
    assert shown["bytes"] == sum(len(original(rig, f"{a}/{al}/{n:02d} {t}.mp3")) for n, (t, a, al) in enumerate(SONGS, start=1)) and shown["free_sectors"] == DVD
    assert shown["media"]["type"] == "DVD+R" and "root" not in shown["media"]


def test_a_cd_gets_the_file_systems_every_reader_has(rig) -> None:
    add_drive(rig.world, "E", writer=True, disc=blank_disc(type="CD-R"))
    assert rig.client.post("/api/discs/data/preview", json=ask(rig)).json()["filesystems"] == ["iso9660", "joliet"]


def test_the_flat_layout_numbers_the_songs_in_the_order_chosen(rig) -> None:
    shown = rig.client.post("/api/discs/data/preview", json=ask(rig, "Three", "One", layout="flat")).json()
    assert [f["dest"] for f in shown["files"]] == ["01 - Three.mp3", "02 - One.mp3"]


def test_a_layout_that_does_not_exist_and_a_speed_the_drive_lacks_are_refused(rig) -> None:
    assert rig.client.post("/api/discs/data/preview", json=ask(rig, layout="tree")).status_code == 422
    too_fast = rig.client.post("/api/discs/data/preview", json=ask(rig, speed=7))
    assert too_fast.status_code == 422 and too_fast.json()["detail"] == "This drive cannot write at that speed."
    assert rig.client.post("/api/discs/data/preview", json=ask(rig, speed=75 * 8)).status_code == 200


def test_a_file_that_is_gone_is_left_out_and_said_so(rig) -> None:
    (rig.music / "Artist A" / "Album 1" / "02 Two.mp3").unlink()
    shown = rig.client.post("/api/discs/data/preview", json=ask(rig)).json()
    assert [f["dest"] for f in shown["files"]] == ["Artist A/Album 1/01 One.mp3", None, "Artist B/Album 2/03 Three.mp3"]
    assert shown["files"][1]["problem"] == "The file is not there any more." and shown["warnings"] == ["1 file will be left out: see the list."]
    job, history, _ = finish(rig, "data")
    assert job.status == "done", job.error
    assert [(t["position"], t["status"]) for t in history["tracks"]] == [(1, "done"), (2, "skipped"), (3, "done")]
    assert history["tracks"][1]["message"] == "The file is not there any more." and history["items"] == 2
    assert sorted(disc_files(rig)) == ["Artist A/Album 1/01 One.mp3", "Artist B/Album 2/03 Three.mp3"]


def test_files_that_do_not_fit_are_said_so_before_anything_happens(rig) -> None:
    add_drive(rig.world, "E", writer=True, disc=blank_disc(type="CD-R", capacity=4100))
    shown = rig.client.post("/api/discs/data/preview", json=ask(rig)).json()
    assert shown["ok"] is False and shown["problems"][0].startswith("The files take ") and "Take out about" in shown["problems"][0]
    refused = rig.client.post("/api/discs/data/start", json=ask(rig))
    assert refused.status_code == 409 and refused.json()["detail"] == shown["problems"][0]
    assert rig.client.get("/api/discs/burns").json()["burns"] == []


def test_a_disc_that_is_not_blank_is_refused(rig) -> None:
    add_drive(rig.world, "E", writer=True, disc=audio_disc(seed=2, tracks=(60,)))
    shown = rig.client.post("/api/discs/data/preview", json=ask(rig)).json()
    assert shown["problems"] == ["This CD-ROM is not blank, and a disc that has been written cannot be used again. Put in a blank disc."]


def test_nothing_is_written_without_a_preview_of_the_same_request(rig) -> None:
    for kind in ("data", "iso"):
        none = rig.client.post(f"/api/discs/{kind}/start", json=ask(rig))
        assert none.status_code == 409 and none.json()["detail"] == "Look at the list first. Nothing is written until you have seen it."
    assert rig.client.post("/api/discs/data/preview", json=ask(rig)).status_code == 200
    assert rig.client.post("/api/discs/data/start", json=ask(rig, "One")).status_code == 409
    assert rig.client.get("/api/discs/burns").json()["burns"] == [] and not rig.ctx.jobs.is_busy()


# ---------------------------------------------------------------------------------------- a data disc


def test_the_files_are_burned_read_back_and_checked(rig) -> None:
    job, history, started = finish(rig, "data")
    assert job.status == "done", job.error
    assert job.result["files"] == 3 and job.result["verified"] is True and job.result["ejected"] is False
    assert (history["kind"], history["status"], history["verified"], history["spoiled"], history["items"], history["disc_type"], history["label"]) == (
        "data", "done", 1, 0, 3, "DVD+R", "Road Trip",
    )
    assert history["title"] == "Data disc Road Trip (3 files)" and history["message"] is None
    assert disc_files(rig) == {"Artist A/Album 1/01 One.mp3": original(rig, "Artist A/Album 1/01 One.mp3"),
                               "Artist A/Album 1/02 Two.mp3": original(rig, "Artist A/Album 1/02 Two.mp3"),
                               "Artist B/Album 2/03 Three.mp3": original(rig, "Artist B/Album 2/03 Three.mp3")}
    assert (job.done, job.total) == (job.total, job.total) and job.total > 0
    got = rig.client.get("/api/discs/media/E:").json()
    assert got["state"] == "written" and got["blank"] is False


def test_the_files_are_burned_without_a_check_when_asked(rig) -> None:
    job, history, _ = finish(rig, "data", verify=False)
    assert job.status == "done" and job.result["verified"] is None and history["verified"] is None


def test_a_disc_that_reads_back_wrong_is_not_trusted(rig) -> None:
    set_control(rig.world, corrupt_burn=True)
    job, history, _ = finish(rig, "data")
    assert job.status == "error"
    assert job.error == "The disc was written, but it cannot be trusted: Artist A/Album 1/01 One.mp3 does not match."
    assert (history["status"], history["verified"], history["spoiled"], history["message"]) == ("failed", 0, 1, job.error)


def test_a_disc_whose_files_never_show_is_said_so(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(datajob, "MOUNT_WAIT", 0.3)
    monkeypatch.setattr(datajob, "MOUNT_LOOK", 0.05)
    set_control(rig.world, no_root=True)
    job, history, _ = finish(rig, "data")
    assert job.status == "error" and job.error.startswith("The disc was written, but Windows did not show its files in time to check them.")
    assert history["status"] == "failed" and history["spoiled"] == 1


def after_the_write(monkeypatch: pytest.MonkeyPatch, refuse) -> list[str]:
    """Make the recorder refuse `media` with this error (a function of how many times it has been asked) once the files are written."""
    asked: list[str] = []
    state = {"written": False}
    real = disc_helper.DiscHelper.call

    def call(self, cmd, **kwargs):
        if cmd == "media" and state["written"]:
            asked.append(cmd)
            error = refuse(len(asked))
            if error is not None:
                raise error
        reply = real(self, cmd, **kwargs)
        if cmd == "burn_data":
            state["written"] = True
        return reply

    monkeypatch.setattr(disc_helper.DiscHelper, "call", call)
    return asked


@pytest.mark.parametrize("code", ["not_ready", "busy", "no_media", "media_changed"])
def test_a_drive_that_is_briefly_not_ready_after_the_write_is_waited_for(rig, monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    monkeypatch.setattr(datajob, "MOUNT_LOOK", 0.02)
    asked = after_the_write(monkeypatch, lambda n: DiscError(code) if n <= 2 else None)
    job, history, _ = finish(rig, "data")
    assert job.status == "done", job.error
    assert len(asked) >= 3 and history["verified"] == 1 and history["spoiled"] == 0


def test_a_refusal_while_the_files_are_being_checked_is_said_with_the_disc_marked_as_written(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(datajob, "MOUNT_LOOK", 0.02)
    after_the_write(monkeypatch, lambda _n: DiscError("unsupported", "The drive stopped answering."))
    job, history, _ = finish(rig, "data")
    assert job.status == "error" and job.error == "The drive stopped answering. The files had been written, but they were not all checked."
    assert (history["status"], history["spoiled"], history["verified"], history["message"]) == ("failed", 0, None, job.error)
    assert sorted(disc_files(rig))


def test_the_disc_is_ejected_after_the_check_when_asked(rig) -> None:
    job, history, _ = finish(rig, "data", eject=True)
    assert job.status == "done" and job.result["ejected"] is True and history["verified"] == 1
    assert [d["media"] for d in rig.client.get("/api/discs/drives?fresh=true").json()["drives"] if d["id"] == "E:"] == ["none"]


def test_a_write_protected_disc_is_refused_and_nothing_is_spoiled(rig) -> None:
    set_control(rig.world, write_protected=True)
    job, history, _ = finish(rig, "data")
    assert job.status == "error" and job.error == "The disc is write protected. Nothing had been written, so the disc is as it was."
    assert (history["status"], history["spoiled"]) == ("failed", 0) and [t["status"] for t in history["tracks"]] == ["failed"] * 3


def test_stopping_a_burn_while_it_writes_spoils_the_disc_and_says_so(rig) -> None:
    set_control(rig.world, burn_slow=1.0)
    started = begin(rig, "data")
    job_id = started["job"]["id"]
    wait_until(rig, job_id, "Writing to the disc")
    assert rig.client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    job = rig.ctx.jobs.wait(job_id, timeout=30)
    assert job.status == "cancelled"
    history = rig.client.get(f"/api/discs/burns/{started['burn_id']}").json()
    assert (history["status"], history["spoiled"]) == ("cancelled", 1)
    assert history["message"] == "Stopped. Writing had begun, so the disc is probably unusable now. A CD-R cannot be written twice; put in a new disc."
    assert rig.client.get("/api/discs/media/E:").json()["blank"] is False


def test_stopping_while_the_files_are_being_checked_leaves_a_good_disc(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = datadisc.verify_files

    def stop_then_read(plan, root, **kwargs):
        for running in rig.ctx.jobs.active():
            rig.ctx.jobs.cancel(running.id)  # the person presses Stop when the checking begins
        return real(plan, root, **kwargs)

    monkeypatch.setattr(datadisc, "verify_files", stop_then_read)
    job, history, _ = finish(rig, "data")
    assert job.status == "cancelled"
    assert (history["status"], history["spoiled"], history["verified"]) == ("cancelled", 0, None)
    assert history["message"] == "Stopped. The files had been written, but they were not all checked."
    assert sorted(disc_files(rig)) and rig.client.get("/api/discs/media/E:").json()["state"] == "written"


def test_a_file_that_changed_since_the_list_was_made_stops_the_burn_with_the_disc_untouched(rig) -> None:
    songs = [datadisc.DataSong(str(rig.music / "Artist A" / "Album 1" / "01 One.mp3"), "One", "A", 1, root=str(rig.music))]
    found = rig.client.get("/api/discs/media/E:").json()
    plan = datadisc.plan_data(songs, media=found)
    request = datajob.DataRequest(drive="E:", eject=False)
    with rig.ctx.db() as conn:
        burn_id = datajob.begin_data(conn, plan, request, "chg", found)
    Path(songs[0].path).write_bytes(b"changed after the list was made")
    handle = SimpleNamespace(cancelled=False, update=lambda **_k: None, log=lambda _line: None, check=lambda: None)
    with pytest.raises(burnjob.BurnError, match=r"^01 One\.mp3 has changed since the list was made\. Look at the list again\.$"):
        datajob.run_data(db=rig.ctx.db, plan=plan, request=request, batch_id="chg", burn_id=burn_id, handle=handle)
    with rig.ctx.db() as conn:
        shown = store.get_burn(conn, burn_id)
    assert (shown["status"], shown["spoiled"]) == ("failed", 0) and not (rig.world / "E" / "files").exists()


# ---------------------------------------------------------------------------------------- an image file


def image_header(path: Path) -> dict:
    blob = path.read_bytes()
    return json.loads(blob[: blob.index(b"\0")])


def test_an_image_is_made_in_the_downloads_folder_and_listed(rig) -> None:
    shown = rig.client.post("/api/discs/iso/preview", json={k: v for k, v in ask(rig).items() if k not in ("drive", "eject")}).json()
    assert shown["folder"] == str(rig.home / "Downloads") and shown["output"] == str(rig.home / "Downloads" / "Road Trip.iso")
    assert shown["filesystems"] == ["iso9660", "joliet"] and shown["free_sectors"] is None and shown["ok"] is True
    job, history, started = finish(rig, "iso")
    assert job.status == "done", job.error
    made = rig.home / "Downloads" / "Road Trip.iso"
    assert started["output"] == str(made) and made.is_file() and made.stat().st_size % 2048 == 0
    assert [f["dest"] for f in image_header(made)["files"]] == ["Artist A/Album 1/01 One.mp3", "Artist A/Album 1/02 Two.mp3", "Artist B/Album 2/03 Three.mp3"]
    assert image_header(made)["label"] == "Road Trip" and job.result["output"] == str(made) and job.result["bytes"] == made.stat().st_size
    assert (history["kind"], history["status"], history["output"], history["drive"], history["items"]) == ("iso", "done", str(made), None, 3)
    assert history["title"] == "Disc image Road Trip (3 files)" and not list((rig.home / "Downloads").glob("*.part"))


def test_an_image_never_replaces_a_file_that_is_there(rig) -> None:
    taken = rig.home / "Downloads" / "Road Trip.iso"
    taken.write_bytes(b"precious")
    job, history, _ = finish(rig, "iso")
    assert job.status == "done" and taken.read_bytes() == b"precious" and history["output"] == str(rig.home / "Downloads" / "Road Trip (2).iso")


def test_an_image_goes_where_it_is_told_if_the_folder_is_there(rig) -> None:
    elsewhere = rig.tmp / "images"
    elsewhere.mkdir()
    job, history, _ = finish(rig, "iso", folder=str(elsewhere), layout="flat")
    assert job.status == "done" and history["output"] == str(elsewhere / "Road Trip.iso")
    assert [f["dest"] for f in image_header(elsewhere / "Road Trip.iso")["files"]] == ["01 - One.mp3", "02 - Two.mp3", "03 - Three.mp3"]


@pytest.mark.parametrize("folder", ["relative/folder", "C:", "\\\\.\\PhysicalDrive0", "does-not-exist"])
def test_an_image_folder_that_is_not_a_real_full_path_is_refused(rig, folder: str, tmp_path: Path) -> None:
    asked = {k: v for k, v in ask(rig).items() if k not in ("drive", "eject")}
    for where in (folder, str(tmp_path / "nothing-here")):
        got = rig.client.post("/api/discs/iso/preview", json={**asked, "folder": where})
        assert got.status_code == 422, (where, got.text)
    assert rig.client.post("/api/discs/iso/start", json={**asked, "folder": folder}).status_code in (409, 422)


def test_an_image_that_fails_leaves_no_file_behind(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = disc_helper.DiscHelper.call
    made: list[Path] = []

    def breaks_after_writing(self, cmd, **args):
        if cmd == "build_iso":
            Path(args["out"]).write_bytes(b"half an image")
            made.append(Path(args["out"]))
            raise DiscError("failed", "The disk is full.")
        return real(self, cmd, **args)

    monkeypatch.setattr(disc_helper.DiscHelper, "call", breaks_after_writing)
    job, history, _ = finish(rig, "iso")
    assert job.status == "error" and job.error == "The disk is full. The image file was removed."
    assert made and not made[0].exists() and (history["status"], history["output"]) == ("failed", None)


def test_an_image_that_is_stopped_before_it_begins_is_not_made(rig) -> None:
    songs = [datadisc.DataSong(str(rig.music / "Artist A" / "Album 1" / "01 One.mp3"), "One", "A", 1, root=str(rig.music))]
    plan = datadisc.plan_data(songs)
    request = datajob.IsoRequest(folder=str(rig.home / "Downloads"))
    with rig.ctx.db() as conn:
        burn_id = datajob.begin_iso(conn, plan, request, "iso1")
    output = rig.home / "Downloads" / "x.iso"
    from musictoolkit.web.jobs import JobCancelled

    def stop():
        raise JobCancelled()

    handle = SimpleNamespace(cancelled=True, update=lambda **_k: None, log=lambda _line: None, check=stop)
    with pytest.raises(JobCancelled):
        datajob.run_iso(db=rig.ctx.db, plan=plan, request=request, batch_id="iso1", burn_id=burn_id, output=output, handle=handle)
    with rig.ctx.db() as conn:
        shown = store.get_burn(conn, burn_id)
    assert (shown["status"], shown["message"]) == ("cancelled", "Stopped. The image file was removed.") and not output.exists()


# ---------------------------------------------------------------------------------------- erasing


def used_disc(rig, **changes):
    add_drive(rig.world, "E", writer=True, disc=audio_disc(seed=2, tracks=(60,)) | {"type": "CD-RW", "rewritable": True, "capacity": 359846, **changes})


def test_a_rewritable_disc_is_erased_and_is_blank_again(rig) -> None:
    used_disc(rig)
    assert rig.client.get("/api/discs/media/E:").json()["audio_problems"][0].startswith("This CD-RW already has something on it")
    started = rig.client.post("/api/discs/erase", json={"drive": "E:", "full": True, "confirm": True}).json()
    job = rig.ctx.jobs.wait(started["job"]["id"], timeout=30)
    assert job.status == "done", job.error
    history = rig.client.get(f"/api/discs/burns/{started['burn_id']}").json()
    assert (history["kind"], history["status"], history["disc_type"], history["message"], history["title"]) == (
        "erase", "done", "CD-RW", "The disc was erased and is blank again.", "Erase CD-RW",
    )
    got = rig.client.get("/api/discs/media/E:").json()
    assert (got["state"], got["blank"], got["audio_problems"]) == ("blank", True, [])


def test_erasing_needs_to_be_said_to_be_meant(rig) -> None:
    used_disc(rig)
    for body in ({"drive": "E:"}, {"drive": "E:", "confirm": False}):
        got = rig.client.post("/api/discs/erase", json=body)
        assert got.status_code == 409 and got.json()["detail"] == "Erasing deletes everything on the disc. Say that this is what you want."
    assert rig.client.get("/api/discs/media/E:").json()["blank"] is False


@pytest.mark.parametrize(
    "disc,said",
    [
        (blank_disc(type="CD-RW"), "The disc is blank already: there is nothing to erase."),
        (audio_disc(seed=2, tracks=(60,)) | {"type": "CD-R", "rewritable": False}, "This CD-R cannot be erased: only rewritable discs (CD-RW, DVD-RW) can."),
        (None, "There is no disc in the drive."),
    ],
)
def test_a_disc_that_cannot_or_need_not_be_erased_says_so(rig, disc: dict | None, said: str) -> None:
    add_drive(rig.world, "E", writer=True, disc=disc)
    if disc is None:
        (rig.world / "E" / "disc.json").unlink()
    got = rig.client.post("/api/discs/erase", json={"drive": "E:", "confirm": True})
    assert got.status_code == 409 and got.json()["detail"] == said
    assert rig.client.get("/api/discs/burns").json()["burns"] == []


def test_a_drive_that_cannot_write_cannot_erase(rig) -> None:
    got = rig.client.post("/api/discs/erase", json={"drive": "F:", "confirm": True})
    assert got.status_code == 409 and got.json()["detail"] == "This drive cannot write discs."
    assert rig.client.post("/api/discs/erase", json={"drive": "Z:", "confirm": True}).status_code == 404
    assert rig.client.post("/api/discs/erase", json={"drive": "../..", "confirm": True}).status_code == 404


def test_an_erase_that_the_recorder_refuses_says_why(rig) -> None:
    used_disc(rig)
    set_control(rig.world, write_protected=True)
    started = rig.client.post("/api/discs/erase", json={"drive": "E:", "confirm": True}).json()
    job = rig.ctx.jobs.wait(started["job"]["id"], timeout=30)
    assert job.status == "error" and job.error == "The disc is write protected."
    history = rig.client.get(f"/api/discs/burns/{started['burn_id']}").json()
    assert (history["status"], history["message"]) == ("failed", "The disc is write protected.")


def test_erasing_cannot_be_stopped_once_begun_and_blocks_the_drives_meanwhile(rig) -> None:
    used_disc(rig)
    set_control(rig.world, burn_slow=0.02)
    started = rig.client.post("/api/discs/erase", json={"drive": "E:", "confirm": True}).json()
    job_id = started["job"]["id"]
    wait_until(rig, job_id, "Erasing the disc")
    try:
        refused = rig.client.get("/api/discs/media/E:")
        assert refused.status_code == 409 and refused.json()["detail"] == "A disc is being erased right now. Wait for it to finish, or stop it."
        assert rig.client.post("/api/discs/erase", json={"drive": "E:", "confirm": True}).status_code == 409
        rig.client.post(f"/api/jobs/{job_id}/cancel")  # (pressed anyway: the erase carries on, as the screen says it will)
    finally:
        job = rig.ctx.jobs.wait(job_id, timeout=30)
    assert job.status in ("done", "cancelled")
    assert rig.client.get("/api/discs/media/E:").json()["blank"] is True


def test_an_image_can_be_made_while_nothing_else_uses_the_drives_and_not_during_a_burn(rig) -> None:
    set_control(rig.world, burn_slow=1.0)
    started = begin(rig, "data")
    job_id = started["job"]["id"]
    wait_until(rig, job_id, "Writing to the disc")
    try:
        asked = {k: v for k, v in ask(rig).items() if k not in ("drive", "eject")}
        refused = rig.client.post("/api/discs/iso/preview", json=asked)
        assert refused.status_code == 409 and refused.json()["detail"] == "A disc is being burned right now. Wait for it to finish, or stop it."
    finally:
        rig.client.post(f"/api/jobs/{job_id}/cancel")
        rig.ctx.jobs.wait(job_id, timeout=30)


def test_history_lists_every_kind_newest_first(rig) -> None:
    finish(rig, "iso")
    add_drive(rig.world, "E", writer=True, disc=blank_disc(type="DVD+R", capacity=DVD))
    finish(rig, "data")
    used_disc(rig)
    erased = rig.client.post("/api/discs/erase", json={"drive": "E:", "confirm": True}).json()
    rig.ctx.jobs.wait(erased["job"]["id"], timeout=30)
    listed = rig.client.get("/api/discs/burns").json()["burns"]
    assert [b["kind"] for b in listed] == ["erase", "data", "iso"] and all(b["status"] == "done" for b in listed)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT.*", listed[0]["started_at"])
