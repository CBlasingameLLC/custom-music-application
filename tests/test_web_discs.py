"""The Discs API, end to end: the real routes, the real helper protocol and a stand-in drive (tests/fake_disc_helper.py) holding a disc of known audio."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import soundfile as sf
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

from musictoolkit.discs import helper as disc_helper
from musictoolkit.discs import reader
from musictoolkit.discs import rip as ripping
from musictoolkit.discs.tagging import TagError
from musictoolkit.discs.toc import Toc, Track
from musictoolkit.net import guard
from musictoolkit.web.routers import discs as api
from tests.conftest import build_web
from tests.fake_disc_helper import add_drive, audio, audio_disc, set_control
from tests.fake_musicbrainz import coverart_script, ident, musicbrainz_script, release_xml
from tests.fake_streams import png_bytes, serve_script

FAKE = Path(__file__).parent / "fake_disc_helper.py"
SEED = 6
SECTORS = (60, 80, 70)
STARTS = (0, 60, 140)
TOC = Toc((Track(1, 0), Track(2, 60), Track(3, 140)), 210)
SCHEME = "{album_artist}/{album}/{track:02d} - {title}.{ext}"


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    world = tmp_path / "drives"
    add_drive(world, "E", disc=audio_disc(seed=SEED, tracks=SECTORS, label="TEST DISC"))
    add_drive(world, "F")
    music = tmp_path / "music"
    music.mkdir()
    web = build_web(tmp_path, music)
    web.config.musicbrainz.contact = "tests@example.com"
    monkeypatch.setenv("MTK_DISC_HELPER", json.dumps([sys.executable, str(FAKE), str(world)]))
    disc_helper.forget_listing()
    yield SimpleNamespace(web=web, client=web.client, ctx=web.ctx, config=web.config, world=world, music=music, tmp=tmp_path)
    disc_helper.forget_listing()


def body(rig, **changes) -> dict:
    base = {
        "drive": "E:", "root": str(rig.music), "scheme": SCHEME, "format": "flac", "quality": "standard", "accuracy": "verified",
        "album": "Test Album", "album_artist": "Test Artist", "year": 2001, "date": "2001-03-04", "genre": "Rock", "disc_number": 1, "disc_total": 1,
        "tracks": [{"number": 1, "title": "First"}, {"number": 2, "title": "Second"}, {"number": 3, "title": "Third"}],
        "cover": False, "eject": False,
    }
    return {**base, **changes}


def start(rig, **changes):
    asked = body(rig, **changes)
    shown = rig.client.post("/api/discs/rip/preview", json=asked)
    assert shown.status_code == 200, shown.text
    started = rig.client.post("/api/discs/rip/start", json=asked)
    assert started.status_code == 200, started.text
    return started.json()


def rip_it(rig, **changes):
    started = start(rig, **changes)
    job = rig.ctx.jobs.wait(started["job"]["id"], timeout=60)
    return job, rig.client.get(f"/api/discs/rips/{started['rip_id']}").json()


def decoded(path: Path) -> bytes:
    return sf.read(str(path), dtype="int16", always_2d=True)[0].tobytes()


def songs_in_library(rig) -> dict[str, dict]:
    return {t["title"]: t for t in rig.client.get("/api/tracks", params={"limit": 100}).json()["items"]}


def album(rig) -> Path:
    return rig.music / "Test Artist" / "Test Album"


@contextlib.contextmanager
def cover_archive(monkeypatch: pytest.MonkeyPatch, release: str | None = None):
    """A stand-in for the Cover Art Archive that has a picture for `release` (or for nothing, when none is named)."""
    pictures = {f"release/{release}": png_bytes((500, 500), (20, 90, 200))} if release else {}
    with guard.local_urls_allowed(), serve_script(coverart_script(pictures)) as server:
        monkeypatch.setenv("MTK_COVERART_URL", server.url)
        yield server


# ---------------------------------------------------------------------------------------- what is there


def test_the_options_say_what_can_be_chosen_and_what_is_chosen_unless_changed(rig) -> None:
    got = rig.client.get("/api/discs/options").json()
    assert got["contact_set"] is True and got["roots"] == [{"path": str(rig.music), "available": True}]
    assert got["scheme"] == SCHEME and any(p["scheme"] == SCHEME for p in got["presets"])
    assert got["qualities"] == {"flac": ["standard"], "mp3": ["320", "256", "192", "128"]}
    assert got["defaults"] == {"format": "flac", "quality": "standard", "mp3_quality": "256", "accuracy": "verified", "root": str(rig.music), "cover": True, "eject": True}
    rig.config.disc.rip_format = "mp3"
    rig.config.disc.rip_scheme = "{album_artist}/{title}.{ext}"
    changed = rig.client.get("/api/discs/options").json()
    assert changed["defaults"]["format"] == "mp3" and changed["defaults"]["quality"] == "256" and changed["scheme"] == "{album_artist}/{title}.{ext}"


def test_the_drives_and_what_is_in_them(rig) -> None:
    got = rig.client.get("/api/discs/drives?fresh=true").json()
    assert got["available"] is True and got["error"] is None and got["busy"] is False
    assert [(d["id"], d["media"]) for d in got["drives"]] == [("E:", "audio"), ("F:", "none")]


def test_without_a_helper_the_drives_say_why_instead_of_failing(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MTK_DISC_HELPER")
    monkeypatch.setattr(sys, "platform", "linux")
    disc_helper.forget_listing()
    got = rig.client.get("/api/discs/drives?fresh=true")
    assert got.status_code == 200 and got.json()["available"] is False and "only supported on Windows" in got.json()["reason"] and got.json()["drives"] == []


def test_the_disc_in_a_drive_is_described_without_asking_the_internet(rig) -> None:
    got = rig.client.get("/api/discs/drives/E:/disc").json()
    assert got["disc_id"] == TOC.disc_id() and got["attach_url"] == TOC.attach_url()
    assert [(t["number"], t["seconds"], t["start"]) for t in got["tracks"]] == [(1, 0.8, 0), (2, 1.1, 60), (3, 0.9, 140)]
    assert [t["title"] for t in got["manual"]["tracks"]] == ["Track 01", "Track 02", "Track 03"]


def test_a_drive_with_no_disc_or_a_data_disc_says_what_is_wrong(rig) -> None:
    empty = rig.client.get("/api/discs/drives/F:/disc")
    assert empty.status_code == 409 and "no disc" in empty.json()["detail"]
    add_drive(rig.world, "G", disc={"kind": "data", "label": "BACKUP", "seed": 0, "tracks": [{"number": 1, "audio": False, "sectors": 500}]})
    data = rig.client.get("/api/discs/drives/G:/disc")
    assert data.status_code == 409 and "no music" in data.json()["detail"]


@pytest.mark.parametrize("drive", ["Z:", "z:", "C:%5CWindows", "%5C%5C.%5CPhysicalDrive0", "..", "E"])
def test_the_page_cannot_make_anything_open_a_device_of_its_choosing(rig, drive: str) -> None:
    got = rig.client.get(f"/api/discs/drives/{drive}/disc")
    assert got.status_code == 404 and "Traceback" not in got.text
    asked = body(rig, drive=drive.replace("%5C", "\\"))
    assert rig.client.post("/api/discs/rip/preview", json=asked).status_code == 404
    assert rig.client.post("/api/discs/eject", json={"drive": drive.replace("%5C", "\\")}).status_code == 404


# ---------------------------------------------------------------------------------------- which album


def test_looking_the_disc_up_needs_a_contact_to_give_musicbrainz(rig) -> None:
    rig.config.musicbrainz.contact = ""
    got = rig.client.post("/api/discs/lookup", json={"drive": "E:"})
    assert got.status_code == 409 and "contact email" in got.json()["detail"]


def test_looking_the_disc_up_finds_the_album(rig, mb, monkeypatch: pytest.MonkeyPatch) -> None:
    here = release_xml(key="found", title="The Real Name", artist="Real Artist", disc_ids=(TOC.disc_id(),), tracks=(("One", 1000), ("Two", 2000), ("Three", 3000)))
    with serve_script(musicbrainz_script(known={TOC.disc_id(): [here]})) as server:
        monkeypatch.setenv("MTK_MUSICBRAINZ_URL", server.url)
        job = rig.ctx.jobs.wait(rig.client.post("/api/discs/lookup", json={"drive": "E:"}).json()["job"]["id"], timeout=30)
    assert job.status == "done", job.error
    assert job.result["disc_id"] == TOC.disc_id()
    found = job.result["candidates"]
    assert [(c["title"], c["artist"], c["exact"]) for c in found] == [("The Real Name", "Real Artist", True)]
    assert [t["title"] for t in found[0]["tracks"]] == ["One", "Two", "Three"]
    assert "tests@example.com" in server.requests[0]["headers"]["User-Agent"]


def test_a_lookup_that_cannot_be_done_says_why_in_the_job(rig, mb, monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(musicbrainz_script(status=503)) as server:
        monkeypatch.setenv("MTK_MUSICBRAINZ_URL", server.url)
        job = rig.ctx.jobs.wait(rig.client.post("/api/discs/lookup", json={"drive": "E:"}).json()["job"]["id"], timeout=30)
    assert job.status == "error" and "busy" in job.error


# ---------------------------------------------------------------------------------------- the preview, then the rip


def test_the_preview_shows_where_every_song_goes_and_writes_nothing(rig) -> None:
    got = rig.client.post("/api/discs/rip/preview", json=body(rig))
    assert got.status_code == 200
    plan = got.json()
    assert plan["ok"] is True and plan["folder"] == "Test Artist/Test Album" and plan["counts"]["rip"] == 3
    assert [t["path"] for t in plan["tracks"]] == [f"Test Artist/Test Album/0{n} - {name}.flac" for n, name in ((1, "First"), (2, "Second"), (3, "Third"))]
    assert not any(rig.music.rglob("*")) and rig.client.get("/api/discs/rips").json()["rips"] == []


def test_a_rip_is_refused_until_the_same_request_has_been_previewed(rig) -> None:
    asked = body(rig)
    assert rig.client.post("/api/discs/rip/start", json=asked).status_code == 409
    assert rig.client.post("/api/discs/rip/preview", json=asked).status_code == 200
    changed = body(rig, album="Another Album")
    refused = rig.client.post("/api/discs/rip/start", json=changed)
    assert refused.status_code == 409 and "first" in refused.json()["detail"]
    assert rig.client.post("/api/discs/rip/start", json=asked).status_code == 200
    assert rig.client.post("/api/discs/rip/start", json=asked).status_code == 409, "a preview is used up by the rip it allowed"


def test_two_requests_cannot_both_start_from_the_same_preview(rig) -> None:
    asked = body(rig)
    assert rig.client.post("/api/discs/rip/preview", json=asked).status_code == 200

    class TakenByAnother(dict):
        def pop(self, key, *default):  # the other request got there between this one looking and taking
            super().pop(key, None)
            return None

    rig.ctx.previews = TakenByAnother(rig.ctx.previews)
    refused = rig.client.post("/api/discs/rip/start", json=asked)
    assert refused.status_code == 409 and "being ripped already" in refused.json()["detail"]
    assert rig.client.get("/api/discs/rips").json()["rips"] == [], "nothing was started"


def test_a_preview_that_cannot_be_acted_on_says_why(rig) -> None:
    short = rig.client.post("/api/discs/rip/preview", json=body(rig, tracks=[{"number": 1, "title": "Only"}]))
    assert short.status_code == 422 and "does not match the disc" in short.json()["detail"]
    elsewhere = rig.client.post("/api/discs/rip/preview", json=body(rig, root=str(rig.tmp / "elsewhere")))
    assert elsewhere.status_code == 422 and "not in your library" in elsewhere.json()["detail"]
    odd = rig.client.post("/api/discs/rip/preview", json=body(rig, release_id="../../x"))
    assert odd.status_code == 422 and "MusicBrainz id" in odd.json()["detail"]


def test_a_rip_makes_exactly_the_audio_that_was_on_the_disc_tagged_and_in_the_library(rig) -> None:
    job, found = rip_it(rig)
    assert job.status == "done", job.error
    assert (job.result["ripped"], job.result["failed"], job.result["status"]) == (3, 0, "done")
    for number, name in ((1, "First"), (2, "Second"), (3, "Third")):
        path = album(rig) / f"0{number} - {name}.flac"
        assert decoded(path) == audio(SEED, STARTS[number - 1], SECTORS[number - 1]), f"{name} is not what was on the disc"
        tags = FLAC(path)
        assert tags["title"] == [name] and tags["tracknumber"] == [str(number)] and tags["tracktotal"] == ["3"] and tags["album"] == ["Test Album"]
        assert tags["albumartist"] == ["Test Artist"] and tags["date"] == ["2001-03-04"] and tags["genre"] == ["Rock"]
    assert not list(album(rig).glob("*.part")) and not list((rig.tmp / "data").rglob("*.pcm")), "no half-written song, no temporary copy left"
    library = songs_in_library(rig)
    assert set(library) == {"First", "Second", "Third"} and library["Second"]["artist"] == "Test Artist" and library["Second"]["album"] == "Test Album"
    assert found["status"] == "done" and found["problems"] == 0 and found["undone_at"] is None and found["album"] == "Test Album"
    assert [(t["number"], t["status"], t["track_id"] is not None) for t in found["tracks"]] == [(1, "done", True), (2, "done", True), (3, "done", True)]
    assert all(t["size"] == (album(rig) / f"0{t['number']} - {t['title']}.flac").stat().st_size for t in found["tracks"])
    listed = rig.client.get("/api/discs/rips").json()["rips"]
    assert [(r["album"], r["songs"], r["tracks"]) for r in listed] == [("Test Album", 3, 3)]


def test_an_mp3_rip_has_the_bitrate_asked_for_and_the_right_length(rig) -> None:
    job, found = rip_it(rig, format="mp3", quality="192", tracks=[{"number": 1, "title": "First"}, {"number": 2, "title": "Second", "rip": False}, {"number": 3, "title": "Third", "rip": False}])
    assert job.status == "done", job.error
    info = MP3(album(rig) / "01 - First.mp3").info
    assert abs(info.bitrate / 1000 - 192) <= 1 and info.length == pytest.approx(SECTORS[0] / 75, abs=0.05)
    assert [t["status"] for t in found["tracks"]] == ["done", "skipped", "skipped"]


def test_the_cover_goes_into_the_songs_and_next_to_them(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    release, group = ident("rel"), ident("grp")
    with cover_archive(monkeypatch, release):
        job, _ = rip_it(rig, cover=True, release_id=release, release_group_id=group, artist_ids=[ident("artist")])
    assert job.status == "done", job.error
    pictures = FLAC(album(rig) / "01 - First.flac").pictures
    assert len(pictures) == 1 and pictures[0].mime == "image/jpeg" and (pictures[0].width, pictures[0].height) == (500, 500)
    assert (album(rig) / "cover.jpg").read_bytes() == pictures[0].data
    assert FLAC(album(rig) / "01 - First.flac")["musicbrainz_albumid"] == [release]
    track = songs_in_library(rig)["First"]
    with rig.ctx.db() as conn:
        row = conn.execute("SELECT musicbrainz_release_id, musicbrainz_artist_id FROM tracks WHERE id = ?", (track["id"],)).fetchone()
    assert (row[0], row[1]) == (release, ident("artist"))


def test_a_cover_that_cannot_be_had_does_not_stop_the_rip(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    with cover_archive(monkeypatch):
        job, _ = rip_it(rig, cover=True, release_id=ident("rel"))
    assert job.status == "done" and FLAC(album(rig) / "01 - First.flac").pictures == [] and not (album(rig) / "cover.jpg").exists()


def test_a_picture_already_in_the_folder_is_not_replaced(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    album(rig).mkdir(parents=True)
    (album(rig) / "cover.jpg").write_bytes(b"mine")
    release = ident("rel")
    with cover_archive(monkeypatch, release):
        job, found = rip_it(rig, cover=True, release_id=release)
    assert job.status == "done", job.error
    assert (album(rig) / "cover.jpg").read_bytes() == b"mine" and len(FLAC(album(rig) / "01 - First.flac").pictures) == 1, "the songs still carry the cover"
    assert found["cover_path"] is None


def test_a_song_left_out_is_not_ripped_and_a_file_that_is_there_is_never_replaced(rig) -> None:
    album(rig).mkdir(parents=True)
    (album(rig) / "02 - Second.flac").write_bytes(b"mine")
    job, found = rip_it(rig, tracks=[{"number": 1, "title": "First"}, {"number": 2, "title": "Second"}, {"number": 3, "title": "Third", "rip": False}])
    assert job.status == "done", job.error
    assert [t["status"] for t in found["tracks"]] == ["done", "skipped", "skipped"]
    assert (album(rig) / "02 - Second.flac").read_bytes() == b"mine" and not (album(rig) / "03 - Third.flac").exists()
    assert set(songs_in_library(rig)) == {"First"}


def test_the_disc_is_ejected_when_asked_and_everything_went_well(rig) -> None:
    job, _ = rip_it(rig, eject=True)
    assert job.status == "done" and job.result["ejected"] is True
    assert (rig.world / "E" / "eject.log").exists()
    assert rig.client.get("/api/discs/drives?fresh=true").json()["drives"][0]["media"] == "none"


def test_a_disc_with_trouble_stays_in_the_drive_and_the_trouble_is_in_the_history(rig) -> None:
    set_control(rig.world, flaky=[{"from": 70, "to": 70, "times": 100, "varies": True}])
    job, found = rip_it(rig, eject=True)
    assert job.status == "done" and job.result["ejected"] is False and not (rig.world / "E" / "eject.log").exists()
    second = found["tracks"][1]
    assert second["status"] == "problem" and second["suspect"] == 1 and second["rereads"] == 1 and "could not be confirmed" in second["message"]
    assert found["problems"] == 1 and found["status"] == "done"


def test_a_sector_that_comes_wrong_the_first_time_is_put_right_without_anyone_noticing(rig) -> None:
    set_control(rig.world, cache=32, flaky=[{"from": 100, "to": 100, "times": 1}])
    job, found = rip_it(rig)
    assert job.status == "done" and found["problems"] == 0
    assert decoded(album(rig) / "02 - Second.flac") == audio(SEED, 60, 80)
    assert found["tracks"][1]["rereads"] == 1 and found["tracks"][1]["status"] == "done"


def test_a_disc_with_a_data_track_after_the_music_is_ripped_without_touching_the_data(rig) -> None:
    add_drive(rig.world, "E", disc=audio_disc(seed=SEED, tracks=SECTORS, label="CD EXTRA", data_sectors=4000))
    set_control(rig.world, strict_data=True, flaky=[{"from": 10, "to": 10, "times": 1}])  # (a read of the data track as music would fail the rip)
    asked = body(rig, tracks=[{"number": 1, "title": "First"}, {"number": 2, "title": "Second"}, {"number": 3, "title": "Third"}, {"number": 4, "title": "Data", "rip": False}])
    shown = rig.client.post("/api/discs/rip/preview", json=asked)
    assert shown.status_code == 422, "the list of songs has to match the music on the disc: the data track is not a song"
    job, found = rip_it(rig)
    assert job.status == "done", job.error
    assert decoded(album(rig) / "01 - First.flac") == audio(SEED, 0, SECTORS[0]) and found["tracks"][0]["rereads"] == 1 and found["problems"] == 0


# ---------------------------------------------------------------------------------------- when it goes wrong


def test_a_song_that_cannot_be_read_is_marked_and_the_others_are_still_ripped(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "MAX_UNREADABLE", 1)
    monkeypatch.setattr(reader, "MAX_ATTEMPTS", 2)
    set_control(rig.world, bad=[{"from": 60, "to": 139}])
    job, found = rip_it(rig)
    assert job.status == "done", job.error
    assert [t["status"] for t in found["tracks"]] == ["done", "failed", "done"] and "too scratched" in found["tracks"][1]["message"]
    assert found["status"] == "partial" and job.result["ripped"] == 2 and job.result["failed"] == 1
    assert set(songs_in_library(rig)) == {"First", "Third"} and not (album(rig) / "02 - Second.flac").exists()


def test_every_song_failing_is_an_error_not_a_success(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "MAX_UNREADABLE", 0)
    monkeypatch.setattr(reader, "MAX_ATTEMPTS", 1)
    set_control(rig.world, bad=[{"from": 0, "to": 209}])
    job, found = rip_it(rig)
    assert job.status == "error" and "None of the songs could be ripped" in job.error
    assert found["status"] == "failed" and not list(rig.music.rglob("*.flac"))


def test_a_disc_taken_out_half_way_ends_the_rip_and_keeps_what_was_done(rig) -> None:
    set_control(rig.world, change_after=10)  # the first song takes nine reads; the second song's first block is the tenth
    job, found = rip_it(rig)
    assert job.status == "error" and "taken out or changed" in job.error
    assert [t["status"] for t in found["tracks"]] == ["done", "failed", "skipped"] and found["status"] == "partial"
    assert (album(rig) / "01 - First.flac").exists() and not list(rig.music.rglob("*.part")) and set(songs_in_library(rig)) == {"First"}


def test_a_different_disc_put_in_after_the_preview_is_noticed_before_anything_is_written(rig) -> None:
    asked = body(rig)
    assert rig.client.post("/api/discs/rip/preview", json=asked).status_code == 200
    add_drive(rig.world, "E", disc=audio_disc(seed=9, tracks=(70, 70, 70), label="ANOTHER"))
    job = rig.ctx.jobs.wait(rig.client.post("/api/discs/rip/start", json=asked).json()["job"]["id"], timeout=30)
    assert job.status == "error" and "not the one this rip was planned for" in job.error
    assert not list(rig.music.rglob("*.flac"))


def test_a_rip_can_be_stopped_and_leaves_no_half_written_song(rig) -> None:
    set_control(rig.world, slow=0.02)
    started = start(rig)
    job_id = started["job"]["id"]
    deadline = time.time() + 30
    while rig.ctx.jobs.get(job_id).done == 0:
        assert time.time() < deadline, "the rip never began"
        time.sleep(0.02)
    assert rig.client.get("/api/discs/drives").json()["busy"] is True
    assert rig.client.get("/api/discs/drives/E:/disc").status_code == 409, "the drive is busy"
    assert rig.client.post("/api/discs/rip/preview", json=body(rig)).status_code == 409
    ejected = rig.client.post("/api/discs/eject", json={"drive": "E:"})
    assert ejected.status_code == 409 and "ripped right now" in ejected.json()["detail"]
    asked = body(rig)  # (a preview cannot be made while a rip is going, so this plants one, as if the page had asked an instant before the rip began)
    rig.ctx.previews["rip"] = {"key": api._key(api.RipBody(**asked)), "toc": TOC}
    refused = rig.client.post("/api/discs/rip/start", json=asked)
    assert refused.status_code == 409 and "being ripped already" in refused.json()["detail"]
    assert rig.ctx.previews.get("rip") is not None, "a request that was refused does not use the preview up"
    assert rig.client.post("/api/discs/lookup", json={"drive": "E:"}).status_code == 409
    rig.ctx.jobs.cancel(job_id)
    job = rig.ctx.jobs.wait(job_id, timeout=30)
    assert job.status == "cancelled"
    found = rig.client.get(f"/api/discs/rips/{started['rip_id']}").json()
    assert found["status"] == "cancelled" and found["finished_at"]
    assert not list(rig.music.rglob("*.part")) and not list((rig.tmp / "data").rglob("*.pcm"))
    for track in found["tracks"]:
        assert track["status"] in ("done", "failed", "skipped")
        assert (track["status"] == "done") == Path(track["file_path"]).exists(), "a song exists exactly when the rip says it was finished"


# ---------------------------------------------------------------------------------------- taking a rip back


@pytest.fixture
def recycle(monkeypatch: pytest.MonkeyPatch):
    """A stand-in for the Recycle Bin: the file is removed and noted."""
    import send2trash

    binned: list[str] = []

    def fake(path: str) -> None:
        binned.append(path)
        Path(path).unlink()

    monkeypatch.setattr(send2trash, "send2trash", fake)
    return binned


def test_a_rip_is_taken_back_by_sending_its_songs_to_the_recycle_bin(rig, recycle) -> None:
    _, found = rip_it(rig, cover=False)
    assert set(songs_in_library(rig)) == {"First", "Second", "Third"}
    undone = rig.client.post(f"/api/discs/rips/{found['id']}/undo")
    assert undone.status_code == 200 and undone.json()["songs"] == 3
    job = rig.ctx.jobs.wait(undone.json()["job"]["id"], timeout=30)
    assert job.status == "done", job.error
    assert job.result["trashed"] == 3 and job.result["changed"] == [] and sorted(Path(p).name for p in recycle) == ["01 - First.flac", "02 - Second.flac", "03 - Third.flac"]
    assert songs_in_library(rig) == {} and not (rig.music / "Test Artist").exists(), "the empty folders go too"
    assert rig.client.get(f"/api/discs/rips/{found['id']}").json()["undone_at"]
    again = rig.client.post(f"/api/discs/rips/{found['id']}/undo")
    assert again.status_code == 409 and "already" in again.json()["detail"]


def test_a_song_that_was_changed_since_is_left_alone(rig, recycle) -> None:
    _, found = rip_it(rig)
    edited = album(rig) / "02 - Second.flac"
    tags = FLAC(edited)
    tags["title"] = ["Second, retitled"]
    tags.save()
    job = rig.ctx.jobs.wait(rig.client.post(f"/api/discs/rips/{found['id']}/undo").json()["job"]["id"], timeout=30)
    assert job.status == "done" and job.result["trashed"] == 2 and job.result["changed"] == [str(edited)]
    assert edited.exists() and set(songs_in_library(rig)) == {"Second"}
    assert rig.client.get(f"/api/discs/rips/{found['id']}").json()["undone_at"] is None, "not everything was put back"


def test_taking_back_a_rip_removes_the_picture_it_made_and_leaves_no_folder_behind(rig, recycle, monkeypatch: pytest.MonkeyPatch) -> None:
    release = ident("rel")
    with cover_archive(monkeypatch, release):
        _, found = rip_it(rig, cover=True, release_id=release)
    assert (album(rig) / "cover.jpg").exists() and found["cover_path"] == str(album(rig) / "cover.jpg")
    job = rig.ctx.jobs.wait(rig.client.post(f"/api/discs/rips/{found['id']}/undo").json()["job"]["id"], timeout=30)
    assert job.status == "done" and job.result["cover"] is True
    assert sorted(Path(p).name for p in recycle) == ["01 - First.flac", "02 - Second.flac", "03 - Third.flac", "cover.jpg"]
    assert not (rig.music / "Test Artist").exists()


def test_a_picture_that_was_there_before_the_rip_or_was_replaced_since_is_left_alone(rig, recycle, monkeypatch: pytest.MonkeyPatch) -> None:
    album(rig).mkdir(parents=True)
    (album(rig) / "cover.jpg").write_bytes(b"mine")
    _, before = rip_it(rig, cover=False)
    job = rig.ctx.jobs.wait(rig.client.post(f"/api/discs/rips/{before['id']}/undo").json()["job"]["id"], timeout=30)
    assert job.status == "done" and job.result["cover"] is False and (album(rig) / "cover.jpg").read_bytes() == b"mine", "it was not this rip's picture"
    (album(rig) / "cover.jpg").unlink()
    release = ident("rel")
    with cover_archive(monkeypatch, release):
        _, found = rip_it(rig, cover=True, release_id=release)
    (album(rig) / "cover.jpg").write_bytes(b"replaced by the person")
    job = rig.ctx.jobs.wait(rig.client.post(f"/api/discs/rips/{found['id']}/undo").json()["job"]["id"], timeout=30)
    assert job.status == "done" and job.result["cover"] is False and (album(rig) / "cover.jpg").read_bytes() == b"replaced by the person"
    assert not list(album(rig).glob("*.flac"))


def test_a_rip_that_is_still_going_or_unknown_cannot_be_taken_back(rig) -> None:
    assert rig.client.post("/api/discs/rips/999/undo").status_code == 404
    assert rig.client.get("/api/discs/rips/999").status_code == 404
    from musictoolkit.discs import store

    with rig.ctx.db() as conn:
        running = store.create_rip(conn, batch_id="r", disc_id=None, release_id=None, album="A", artist="B", drive="E:", fmt="flac", quality="standard",
                                   accuracy="verified", root=str(rig.music), folder=None, tracks=[])
    assert rig.client.post(f"/api/discs/rips/{running}/undo").status_code == 409


def test_a_rip_left_running_by_a_closed_app_is_marked_when_the_app_starts_again(rig) -> None:
    from musictoolkit.discs import store
    from musictoolkit.web.app import create_app

    with rig.ctx.db() as conn:
        rip_id = store.create_rip(conn, batch_id="old", disc_id=None, release_id=None, album="A", artist="B", drive="E:", fmt="flac", quality="standard",
                                  accuracy="verified", root=str(rig.music), folder=None, tracks=[{"number": 1, "title": "One", "file_path": str(rig.music / "one.flac")}])
    half = rig.music / "one.flac.part"
    half.write_bytes(b"half")
    create_app(db_path=rig.ctx.db_path, config_path=rig.ctx.config_path, config=rig.config)  # the app starts again over the same library
    with rig.ctx.db() as conn:
        found = store.get_rip(conn, rip_id)
    assert found["status"] == "interrupted" and not half.exists()


# ---------------------------------------------------------------------------------------- ejecting, settings, diagnostics


def test_a_disc_can_be_ejected_from_the_screen(rig) -> None:
    assert rig.client.get("/api/discs/drives").json()["drives"][0]["media"] == "audio"  # (the answer is remembered for a few seconds)
    assert rig.client.post("/api/discs/eject", json={"drive": "E:"}).json() == {"ok": True}
    assert rig.client.get("/api/discs/drives").json()["drives"][0]["media"] == "none", "the remembered answer was dropped"
    assert rig.client.get("/api/discs/drives?fresh=true").json()["drives"][0]["media"] == "none"
    again = rig.client.post("/api/discs/eject", json={"drive": "Q:"})
    assert again.status_code == 404


def test_the_ripping_settings_are_kept_and_checked(rig) -> None:
    ok = rig.client.put("/api/settings", json={"disc": {"rip_format": "mp3", "rip_mp3_quality": "320", "rip_accuracy": "fast", "rip_cover": False,
                                                       "eject_after_rip": False, "rip_root": str(rig.music), "rip_scheme": "{album_artist}/{title}.{ext}"}})
    assert ok.status_code == 200 and ok.json()["disc"]["rip_format"] == "mp3" and ok.json()["disc"]["rip_scheme"] == "{album_artist}/{title}.{ext}"
    assert rig.config.disc.rip_mp3_quality == "320" and rig.config.disc.rip_cover is False
    assert rig.client.put("/api/settings", json={"disc": {"rip_scheme": ""}}).status_code == 200, "empty means the library's own layout"
    assert rig.client.put("/api/settings", json={"disc": {"rip_root": ""}}).status_code == 200
    for bad in ({"rip_format": "wav"}, {"rip_mp3_quality": "100"}, {"rip_accuracy": "slow"}, {"rip_scheme": "{nonsense}"},
                {"rip_root": str(rig.tmp / "elsewhere")}, {"rip_cover": "yes"}, {"nonsense": 1}):
        rejected = rig.client.put("/api/settings", json={"disc": bad})
        assert rejected.status_code == 422, bad
    assert rig.config.disc.rip_format == "mp3", "a refusal changes nothing"


def test_diagnostics_say_what_the_disc_helper_found(rig) -> None:
    got = rig.client.get("/api/system/diagnostics").json()["discs"]
    assert got["available"] is True and got["error"] is None and got["reason"] is None
    assert [(d["id"], d["media"]) for d in got["drives"]] == [("E:", "audio"), ("F:", "none")]


def test_only_the_song_being_ripped_has_a_temporary_copy_and_none_is_left_afterwards(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = rig.ctx.data_dir / "cache" / "discs"
    copies: list[int] = []
    real = ripping.write_tags

    def count_then_tag(path, fmt, tags, cover=None):
        copies.append(len(list(scratch.glob("*.pcm"))))
        real(path, fmt, tags, cover)

    monkeypatch.setattr(ripping, "write_tags", count_then_tag)
    job, _ = rip_it(rig)
    assert job.status == "done" and copies == [1, 1, 1], "the room check counts one temporary copy, the longest, so a long album must not keep one per song"
    assert not list(scratch.glob("*.pcm"))


def test_a_name_that_is_taken_while_a_song_is_being_ripped_is_never_replaced(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = ripping.write_tags

    def tag_then_someone_takes_the_name(path, fmt, tags, cover=None):
        real(path, fmt, tags, cover)
        if tags.track_number == 2:
            Path(str(path)[: -len(".part")]).write_bytes(b"mine")

    monkeypatch.setattr(ripping, "write_tags", tag_then_someone_takes_the_name)
    job, found = rip_it(rig)
    assert job.status == "done" and found["status"] == "partial" and job.result["ripped"] == 2 and job.result["failed"] == 1
    assert [t["status"] for t in found["tracks"]] == ["done", "failed", "done"] and "appeared while the song was being ripped" in found["tracks"][1]["message"]
    assert (album(rig) / "02 - Second.flac").read_bytes() == b"mine"
    assert not list(rig.music.rglob("*.part")), "the half-finished copy does not stay behind"
    assert set(songs_in_library(rig)) == {"First", "Third"}


def test_a_song_that_cannot_be_tagged_ends_the_rip_and_leaves_no_half_written_file(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = ripping.write_tags

    def refuse_the_second(path, fmt, tags, cover=None):
        if tags.track_number == 2:
            raise TagError("The tags could not be written.")
        real(path, fmt, tags, cover)

    monkeypatch.setattr(ripping, "write_tags", refuse_the_second)
    job, found = rip_it(rig)
    assert job.status == "error" and "tags could not be written" in job.error
    assert found["status"] == "partial" and [t["status"] for t in found["tracks"]] == ["done", "failed", "skipped"]
    assert not list(rig.music.rglob("*.part")) and not list(rig.ctx.data_dir.rglob("*.pcm")) and set(songs_in_library(rig)) == {"First"}


def test_a_disc_is_not_ejected_when_a_song_on_it_could_not_be_ripped(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "MAX_UNREADABLE", 1)
    monkeypatch.setattr(reader, "MAX_ATTEMPTS", 2)
    set_control(rig.world, bad=[{"from": 60, "to": 139}])
    job, found = rip_it(rig, eject=True)
    assert job.status == "done" and job.result["failed"] == 1 and job.result["ejected"] is False
    assert not (rig.world / "E" / "eject.log").exists(), "the person may want to try the damaged song again"


def test_a_temporary_copy_that_cannot_be_deleted_at_once_does_not_stop_the_rip_and_is_removed_at_the_end(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = rig.ctx.data_dir / "cache" / "discs"
    unlink = Path.unlink
    refused: set[Path] = set()

    def busy_the_first_time(self, missing_ok=False):
        if self.suffix == ".pcm" and self not in refused:
            refused.add(self)
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", busy_the_first_time)
    job, found = rip_it(rig)
    assert job.status == "done" and found["status"] == "done" and len(refused) == 3
    assert not list(scratch.glob("*.pcm"))


def test_copies_left_in_the_cache_by_an_earlier_rip_are_cleared_when_the_next_one_starts(rig) -> None:
    scratch = rig.ctx.data_dir / "cache" / "discs"
    scratch.mkdir(parents=True)
    (scratch / "0123456789ab-01.pcm").write_bytes(b"left behind")
    (scratch / "keep.txt").write_bytes(b"not a temporary copy")
    job, _ = rip_it(rig)
    assert job.status == "done" and not list(scratch.glob("*.pcm")) and (scratch / "keep.txt").exists()


def test_a_file_that_a_scanner_still_holds_is_tagged_and_renamed_once_it_is_let_go(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ripping, "PATIENCE", (0, 0, 0))
    real_tags, real_replace, real_rename = ripping.write_tags, os.replace, os.rename
    refused = {"tags": 0, "rename": 0}

    def tags_busy(path, fmt, tags, cover=None):
        if tags.track_number == 2 and refused["tags"] < 2:
            refused["tags"] += 1
            raise TagError("The file is read-only or in use by another program.", busy=True)
        real_tags(path, fmt, tags, cover)

    def rename_busy(source, target, *args, **kwargs):
        if str(source).endswith("03 - Third.flac.part") and refused["rename"] < 2:
            refused["rename"] += 1
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return (real_rename if os.name == "nt" else real_replace)(source, target, *args, **kwargs)

    monkeypatch.setattr(ripping, "write_tags", tags_busy)
    monkeypatch.setattr(os, "rename" if os.name == "nt" else "replace", rename_busy)
    job, found = rip_it(rig)
    assert job.status == "done" and found["status"] == "done" and refused == {"tags": 2, "rename": 2}
    assert [t["status"] for t in found["tracks"]] == ["done", "done", "done"] and not list(rig.music.rglob("*.part"))


def test_a_song_that_stays_locked_is_marked_and_the_others_are_still_ripped(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ripping, "PATIENCE", (0, 0))
    real_replace, real_rename = os.replace, os.rename

    def locked(source, target, *args, **kwargs):
        if str(source).endswith("02 - Second.flac.part"):
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return (real_rename if os.name == "nt" else real_replace)(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "rename" if os.name == "nt" else "replace", locked)
    job, found = rip_it(rig)
    assert job.status == "done" and found["status"] == "partial"
    assert [t["status"] for t in found["tracks"]] == ["done", "failed", "done"] and "could not be saved" in found["tracks"][1]["message"]
    assert not list(rig.music.rglob("*.part")) and set(songs_in_library(rig)) == {"First", "Third"}


def test_a_song_that_is_already_gone_is_forgotten_by_the_library_when_the_rip_is_taken_back(rig, recycle) -> None:
    _, found = rip_it(rig, cover=False)
    (album(rig) / "02 - Second.flac").unlink()
    job = rig.ctx.jobs.wait(rig.client.post(f"/api/discs/rips/{found['id']}/undo").json()["job"]["id"], timeout=30)
    assert job.status == "done" and job.result["gone"] == 1 and job.result["trashed"] == 2
    assert songs_in_library(rig) == {} and rig.client.get(f"/api/discs/rips/{found['id']}").json()["undone_at"]


def test_a_song_the_recycle_bin_refuses_is_reported_and_the_rip_is_not_marked_as_taken_back(rig, recycle, monkeypatch: pytest.MonkeyPatch) -> None:
    import send2trash

    _, found = rip_it(rig, cover=False)
    stuck = album(rig) / "02 - Second.flac"
    works = send2trash.send2trash

    def refuse_the_second(path):
        if Path(path) == stuck:
            raise OSError("The Recycle Bin is not available")
        works(path)

    monkeypatch.setattr(send2trash, "send2trash", refuse_the_second)
    job = rig.ctx.jobs.wait(rig.client.post(f"/api/discs/rips/{found['id']}/undo").json()["job"]["id"], timeout=30)
    assert job.status == "done" and job.result["trashed"] == 2 and job.result["failed"] == [{"path": str(stuck), "reason": "The Recycle Bin is not available"}]
    assert stuck.exists() and set(songs_in_library(rig)) == {"Second"}
    assert rig.client.get(f"/api/discs/rips/{found['id']}").json()["undone_at"] is None


def test_a_file_that_was_there_before_the_rip_is_not_touched_when_the_rip_is_taken_back(rig, recycle) -> None:
    album(rig).mkdir(parents=True)
    (album(rig) / "02 - Second.flac").write_bytes(b"mine")
    _, found = rip_it(rig, cover=False)
    job = rig.ctx.jobs.wait(rig.client.post(f"/api/discs/rips/{found['id']}/undo").json()["job"]["id"], timeout=30)
    assert job.status == "done" and job.result["trashed"] == 2 and job.result["changed"] == [] and job.result["failed"] == []
    assert (album(rig) / "02 - Second.flac").read_bytes() == b"mine" and [p.name for p in album(rig).iterdir()] == ["02 - Second.flac"]
    assert rig.client.get(f"/api/discs/rips/{found['id']}").json()["undone_at"]


def test_a_song_whose_size_changed_is_left_alone_even_when_its_date_was_put_back(rig, recycle) -> None:
    _, found = rip_it(rig, cover=False)
    edited = album(rig) / "02 - Second.flac"
    stamp = edited.stat()
    with edited.open("ab") as handle:
        handle.write(b"\0" * 8)
    os.utime(edited, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    job = rig.ctx.jobs.wait(rig.client.post(f"/api/discs/rips/{found['id']}/undo").json()["job"]["id"], timeout=30)
    assert job.status == "done" and job.result["trashed"] == 2 and job.result["changed"] == [str(edited)] and edited.exists()


def test_a_drive_the_helper_does_not_list_is_not_opened_even_if_it_would_answer(rig) -> None:
    add_drive(rig.world, "G", disc=audio_disc(seed=3, tracks=(70, 70), label="HIDDEN"))
    set_control(rig.world, unlisted=["G"])  # (the stand-in still answers for it, as a careless helper might)
    assert [d["id"] for d in rig.client.get("/api/discs/drives?fresh=true").json()["drives"]] == ["E:", "F:"]
    assert rig.client.get("/api/discs/drives/G:/disc").status_code == 404
    assert rig.client.get("/api/discs/drives/E:/disc").status_code == 200


def test_a_rip_is_refused_when_the_folder_changed_since_the_preview(rig) -> None:
    asked = body(rig)
    assert rig.client.post("/api/discs/rip/preview", json=asked).status_code == 200
    album(rig).mkdir(parents=True)
    for name in ("01 - First", "02 - Second", "03 - Third"):
        (album(rig) / f"{name}.flac").write_bytes(b"mine")
    refused = rig.client.post("/api/discs/rip/start", json=asked)
    assert refused.status_code == 409 and "nothing to rip" in refused.json()["detail"]
    assert rig.client.get("/api/discs/rips").json()["rips"] == []


def test_temporary_files_that_cannot_be_deleted_do_not_stop_the_rip(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = rig.ctx.data_dir / "cache" / "discs"
    scratch.mkdir(parents=True)
    (scratch / "0123456789ab-01.pcm").write_bytes(b"left behind by an earlier rip")
    unlink = Path.unlink

    def held_by_another_program(self, missing_ok=False):
        if self.suffix == ".pcm":
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", held_by_another_program)
    job, found = rip_it(rig)
    assert job.status == "done" and found["status"] == "done" and [t["status"] for t in found["tracks"]] == ["done", "done", "done"]
    assert set(songs_in_library(rig)) == {"First", "Second", "Third"} and not list(rig.music.rglob("*.part"))


def test_a_half_written_song_that_cannot_be_deleted_at_once_is_removed_at_the_end(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = ripping.write_tags

    def tag_then_someone_takes_the_name(path, fmt, tags, cover=None):
        real(path, fmt, tags, cover)
        if tags.track_number == 2:
            Path(str(path)[: -len(".part")]).write_bytes(b"mine")

    unlink = Path.unlink
    refused: set[Path] = set()

    def busy_the_first_time(self, missing_ok=False):
        if self.suffix == ".part" and self not in refused:
            refused.add(self)
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(ripping, "write_tags", tag_then_someone_takes_the_name)
    monkeypatch.setattr(Path, "unlink", busy_the_first_time)
    job, found = rip_it(rig)
    assert job.status == "done" and [t["status"] for t in found["tracks"]] == ["done", "failed", "done"]
    assert (album(rig) / "02 - Second.flac").read_bytes() == b"mine" and not list(rig.music.rglob("*.part"))


def test_tags_are_tried_again_a_few_times_while_the_file_is_in_use_and_then_the_rip_ends(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ripping, "PATIENCE", (0, 0))
    attempts: list[int] = []

    def always_in_use(path, fmt, tags, cover=None):
        attempts.append(tags.track_number)
        raise TagError("The file is read-only or in use by another program.", busy=True)

    monkeypatch.setattr(ripping, "write_tags", always_in_use)
    job, found = rip_it(rig)
    assert job.status == "error" and "in use by another program" in job.error
    assert attempts == [1, 1, 1], "the first try and two more"
    assert found["status"] == "failed" and not list(rig.music.rglob("*.part")) and not list(rig.music.rglob("*.flac"))


def test_a_tag_error_that_waiting_cannot_cure_is_not_tried_again(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ripping, "PATIENCE", (0, 0))
    attempts: list[int] = []

    def broken(path, fmt, tags, cover=None):
        attempts.append(tags.track_number)
        raise TagError("The tags could not be written (No space left on device).")

    monkeypatch.setattr(ripping, "write_tags", broken)
    job, _ = rip_it(rig)
    assert job.status == "error" and "No space left" in job.error and attempts == [1]


def test_a_name_taken_between_the_check_and_the_rename_is_not_replaced(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = ripping._move_into_place

    def refuse_the_second(source, target):
        if target.name.startswith("02"):
            raise FileExistsError(17, "File exists")
        real(source, target)

    monkeypatch.setattr(ripping, "_move_into_place", refuse_the_second)
    job, found = rip_it(rig)
    assert job.status == "done" and [t["status"] for t in found["tracks"]] == ["done", "failed", "done"]
    assert "appeared while the song was being ripped" in found["tracks"][1]["message"] and not list(rig.music.rglob("*.part"))
