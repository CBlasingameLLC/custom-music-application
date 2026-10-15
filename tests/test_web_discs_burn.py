"""Burning music onto a disc, end to end: the real routes, the real helper protocol and a stand-in recorder (tests/fake_disc_helper.py) with a
blank disc in it. What goes in comes back out: a burned disc is ripped again and compared with the songs it was made from."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC

from musictoolkit.discs import burn, burnjob, store
from musictoolkit.discs import helper as disc_helper
from musictoolkit.discs.burn import SongInput
from tests.conftest import build_web, ids_by_title, scan
from tests.fake_disc_helper import CD_BLANK_SECTORS, add_drive, audio, audio_disc, blank_disc, set_control

FAKE = Path(__file__).parent / "fake_disc_helper.py"
SONGS = [("One", 11, 375), ("Two", 12, 450), ("Three", 13, 400)]  # title, the stand-in's seed for its sound, length in sectors
SCHEME = "{album_artist}/{album}/{track:02d} - {title}.{ext}"


def make_song(folder: Path, title: str, seed: int, sectors: int) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{title}.flac"
    sf.write(str(path), np.frombuffer(audio(seed, 0, sectors), dtype="<i2").reshape(-1, 2), 44100, subtype="PCM_16", format="FLAC")
    tags = FLAC(path)
    tags["title"], tags["artist"], tags["album"] = title, "Burner", "Mix"
    tags.save()
    return path


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    world = tmp_path / "drives"
    add_drive(world, "E", writer=True, disc=blank_disc(type="CD-R"))
    add_drive(world, "F", writer=False, disc=audio_disc(seed=3, tracks=(60, 80)))
    music = tmp_path / "music"
    for title, seed, sectors in SONGS:
        make_song(music / "Burner" / "Mix", title, seed, sectors)
    web = build_web(tmp_path, music)
    scan(web)
    monkeypatch.setenv("MTK_DISC_HELPER", json.dumps([sys.executable, str(FAKE), str(world)]))
    disc_helper.forget_listing()
    yield SimpleNamespace(web=web, client=web.client, ctx=web.ctx, config=web.config, world=world, music=music, tmp=tmp_path, ids=ids_by_title(web.client))
    disc_helper.forget_listing()


def ask(rig, *titles: str, **changes) -> dict:
    return {"drive": "E:", "track_ids": [rig.ids[t] for t in (titles or [s[0] for s in SONGS])], "eject": False, **changes}


def preview(rig, *titles: str, **changes):
    return rig.client.post("/api/discs/burn/preview", json=ask(rig, *titles, **changes))


def begin(rig, *titles: str, **changes) -> dict:
    asked = ask(rig, *titles, **changes)
    shown = rig.client.post("/api/discs/burn/preview", json=asked)
    assert shown.status_code == 200, shown.text
    started = rig.client.post("/api/discs/burn/start", json=asked)
    assert started.status_code == 200, started.text
    return started.json()


def burn_it(rig, *titles: str, **changes):
    started = begin(rig, *titles, **changes)
    job = rig.ctx.jobs.wait(started["job"]["id"], timeout=60)
    return job, rig.client.get(f"/api/discs/burns/{started['burn_id']}").json()


def scratch(rig) -> Path:
    return rig.ctx.data_dir / "cache" / "discs" / "burn"


def media(rig, drive: str = "E:") -> dict:
    got = rig.client.get(f"/api/discs/media/{drive}")
    assert got.status_code == 200, got.text
    return got.json()


def rip_back(rig, count: int) -> list[np.ndarray]:
    """Rip the disc in drive E: into the library like any other disc, and give the samples of each track."""
    asked = {
        "drive": "E:", "root": str(rig.music), "scheme": SCHEME, "format": "flac", "quality": "standard", "accuracy": "verified", "album": "Burned",
        "album_artist": "Back", "disc_number": 1, "disc_total": 1, "cover": False, "eject": False,
        "tracks": [{"number": n, "title": f"Track {n}"} for n in range(1, count + 1)],
    }
    assert rig.client.post("/api/discs/rip/preview", json=asked).status_code == 200
    started = rig.client.post("/api/discs/rip/start", json=asked).json()
    job = rig.ctx.jobs.wait(started["job"]["id"], timeout=60)
    assert job.status == "done", job.error
    folder = rig.music / "Back" / "Burned"
    return [sf.read(str(folder / f"{n:02d} - Track {n}.flac"), dtype="int16", always_2d=True)[0] for n in range(1, count + 1)]


def original(seed: int, sectors: int) -> np.ndarray:
    return np.frombuffer(audio(seed, 0, sectors), dtype="<i2").reshape(-1, 2)


# ---------------------------------------------------------------------------------------- the disc in the recorder


def test_a_blank_cd_says_what_it_is_and_that_music_can_go_on_it(rig) -> None:
    got = media(rig)
    assert (got["state"], got["type"], got["blank"], got["writer"], got["can_audio"]) == ("blank", "CD-R", True, True, True)
    assert got["free_sectors"] == CD_BLANK_SECTORS and got["audio_problems"] == [] and "root" not in got and "ok" not in got
    assert [s["label"] for s in got["speeds"]] == ["8x", "16x", "24x", "32x", "48x"]


def test_a_drive_that_cannot_write_says_so_in_words(rig) -> None:
    got = media(rig, "F:")
    assert got["writer"] is False and got["audio_problems"] == ["This drive cannot write discs."]


def test_the_media_of_a_drive_that_is_not_there_is_a_404_not_a_crash(rig) -> None:
    assert rig.client.get("/api/discs/media/Z:").status_code == 404
    assert rig.client.get("/api/discs/media/not-a-drive").status_code == 404


def test_diagnostics_say_which_drives_can_write_and_what(rig) -> None:
    got = rig.client.get("/api/system/diagnostics").json()["discs"]
    assert [(d["id"], d["writer"], d["writes"], d["media"]) for d in got["drives"]] == [("E:", True, ["CD-R", "CD-RW"], "blank"), ("F:", False, [], "audio")]


def test_the_options_say_what_burning_starts_with(rig) -> None:
    assert rig.client.get("/api/discs/options").json()["burn"] == {"eject": True, "verify": True}
    assert rig.client.put("/api/settings", json={"disc": {"eject_after_burn": False}}).json()["disc"]["eject_after_burn"] is False
    assert rig.client.get("/api/discs/options").json()["burn"] == {"eject": False, "verify": True}


# ---------------------------------------------------------------------------------------- looking before burning


def test_the_preview_lists_the_songs_and_what_they_take_of_the_disc(rig) -> None:
    got = preview(rig).json()
    assert [(s["position"], s["title"], s["verdict"], s["sectors"]) for s in got["songs"]] == [(1, "One", "ok", 375), (2, "Two", "ok", 450), (3, "Three", "ok", 400)]
    assert got["disc_sectors"] == 375 + 450 + 400 + 2 * 150 and got["free_sectors"] == CD_BLANK_SECTORS
    assert got["ok"] is True and got["problems"] == [] and got["counts"] == {"ok": 3, "converted": 0, "cannot": 0}
    assert got["media"]["type"] == "CD-R" and "root" not in got["media"]


def test_a_song_that_cannot_be_burned_is_listed_and_left_out_of_the_burn(rig) -> None:
    wma = rig.music / "Burner" / "Mix" / "Four.wma"
    wma.write_bytes(b"not audio")
    with rig.ctx.db() as conn:
        conn.execute("INSERT INTO tracks (file_path, title, artist) VALUES (?, 'Four', 'Burner')", (str(wma),))
        conn.commit()
    rig.ids = ids_by_title(rig.client)
    shown = preview(rig, "One", "Four", "Two").json()
    assert [s["verdict"] for s in shown["songs"]] == ["ok", "cannot", "ok"] and shown["counts"]["cannot"] == 1
    assert shown["songs"][1]["notes"] == ["WMA songs cannot be burned to a music CD yet. MP3, FLAC, WAV, Ogg and Opus can."]
    assert shown["disc_sectors"] == 375 + 450 + 150 and shown["ok"] is True
    job, history = burn_it(rig, "One", "Four", "Two")
    assert job.status == "done", job.error
    assert [(t["position"], t["title"], t["status"]) for t in history["tracks"]] == [(1, "One", "done"), (2, "Four", "skipped"), (3, "Two", "done")]
    assert history["tracks"][1]["message"].startswith("WMA songs cannot be burned") and history["tracks"][1]["sectors"] is None
    assert len(rip_back(rig, 2)) == 2


def test_songs_that_are_not_in_the_library_any_more_are_refused(rig) -> None:
    got = rig.client.post("/api/discs/burn/preview", json={"drive": "E:", "track_ids": [rig.ids["One"], 999999]})
    assert got.status_code == 422 and got.json()["detail"] == "Some of those songs are not in your library any more."


def test_a_burn_needs_a_drive_that_exists_and_a_speed_it_offers(rig) -> None:
    assert preview(rig, drive="Z:").status_code == 404
    assert preview(rig, drive="/dev/sr0").status_code == 404
    too_fast = preview(rig, speed=7)
    assert too_fast.status_code == 422 and too_fast.json()["detail"] == "This drive cannot write at that speed."
    assert preview(rig, speed=75 * 16).status_code == 200
    assert rig.client.post("/api/discs/burn/preview", json={"drive": "E:", "track_ids": []}).status_code == 422


def test_songs_that_do_not_fit_are_said_so_before_anything_happens(rig) -> None:
    add_drive(rig.world, "E", writer=True, disc=blank_disc(capacity=1000))
    shown = preview(rig).json()
    assert shown["ok"] is False
    assert shown["problems"] == ["The songs take 0:20 and the disc has 0:13 free. Take out about 11 seconds of music (a few seconds are kept in reserve)."]
    refused = rig.client.post("/api/discs/burn/start", json=ask(rig))
    assert refused.status_code == 409 and refused.json()["detail"] == shown["problems"][0]
    assert rig.client.get("/api/discs/burns").json()["burns"] == []


@pytest.mark.parametrize(
    "disc,said",
    [
        (audio_disc(seed=2, tracks=(60,)), "This CD-ROM is not blank, and a disc that has been written cannot be used again. Put in a blank CD-R."),
        (None, "There is no disc in the drive."),
    ],
)
def test_a_disc_that_cannot_take_music_is_said_so_in_the_preview_and_refused_at_the_start(rig, disc: dict | None, said: str) -> None:
    add_drive(rig.world, "E", writer=True, disc=disc)
    if disc is None:
        (rig.world / "E" / "disc.json").unlink()
    assert preview(rig).json()["problems"] == [said]
    assert rig.client.post("/api/discs/burn/start", json=ask(rig)).status_code == 409


def test_nothing_is_written_without_a_preview_of_the_same_request(rig) -> None:
    none = rig.client.post("/api/discs/burn/start", json=ask(rig))
    assert none.status_code == 409 and none.json()["detail"] == "Look at the list of songs first. Nothing is written until you have seen it."
    assert preview(rig).status_code == 200
    changed = rig.client.post("/api/discs/burn/start", json=ask(rig, "One"))
    assert changed.status_code == 409
    assert rig.client.get("/api/discs/burns").json()["burns"] == [] and not rig.ctx.jobs.is_busy()


def test_a_preview_can_be_used_once(rig) -> None:
    begin(rig)
    again = rig.client.post("/api/discs/burn/start", json=ask(rig))
    assert again.status_code == 409


# ---------------------------------------------------------------------------------------- burning


def test_the_songs_are_burned_and_ripping_the_disc_gives_them_back_sample_for_sample(rig) -> None:
    job, history = burn_it(rig)
    assert job.status == "done", job.error
    assert job.result["songs"] == 3 and job.result["sectors"] == 375 + 450 + 400 + 300 and job.result["ejected"] is False
    assert (history["kind"], history["status"], history["spoiled"], history["items"], history["sectors"], history["disc_type"], history["drive"]) == (
        "audio", "done", 0, 3, 1525, "CD-R", "E:",
    )
    assert history["title"] == "Music CD of 3 songs" and history["message"] is None and history["finished_at"]
    assert [(t["position"], t["title"], t["status"], t["sectors"]) for t in history["tracks"]] == [(1, "One", "done", 375), (2, "Two", "done", 450), (3, "Three", "done", 400)]
    assert not scratch(rig).exists()  # the temporary songs are gone
    got = media(rig)
    assert got["state"] == "written" and got["blank"] is False and got["free_sectors"] == 0
    heard = rip_back(rig, 3)
    for (title, seed, sectors), samples, gap in zip(SONGS, heard, (150, 150, 0)):
        assert len(samples) == (sectors + gap) * 588
        assert np.array_equal(samples[: sectors * 588], original(seed, sectors)), title
        assert not samples[sectors * 588:].any()  # the two seconds between songs are silence


def test_the_progress_covers_preparing_the_songs_and_then_writing_the_disc(rig) -> None:
    job, _ = burn_it(rig)
    prepare, disc = 375 + 450 + 400, 375 + 450 + 400 + 300
    assert (job.done, job.total) == (prepare + disc, prepare + disc) and job.message == "Finishing"


def test_while_the_disc_is_written_the_progress_says_how_far_and_which_song(rig) -> None:
    set_control(rig.world, burn_slow=0.004)
    started = begin(rig)
    job_id = started["job"]["id"]
    _wait_until_writing(rig, job_id)
    job = rig.ctx.jobs.get(job_id)
    assert re.fullmatch(r"Writing to the disc: \d+% \(song [123] of 3\)", job.message), job.message
    assert 1225 <= job.done <= 2750 and job.total == 2750  # (the songs are ready; the rest is the disc)
    rig.client.post(f"/api/jobs/{job_id}/cancel")
    rig.ctx.jobs.wait(job_id, timeout=30)


def test_the_helper_is_asked_for_exactly_the_songs_the_speed_and_the_eject_that_were_chosen(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict]] = []
    real = disc_helper.DiscHelper.call

    def spy(self, cmd, **args):
        calls.append((cmd, args))
        return real(self, cmd, **args)

    monkeypatch.setattr(disc_helper.DiscHelper, "call", spy)
    job, _ = burn_it(rig, speed=75 * 24, eject=True)
    assert job.status == "done", job.error
    sent = [(cmd, args) for cmd, args in calls if cmd in ("media", "burn_audio", "eject")]
    assert [cmd for cmd, _ in sent if cmd != "media"] == ["burn_audio", "eject"]
    burned = next(args for cmd, args in sent if cmd == "burn_audio")
    assert burned["drive"] == "E:" and burned["speed"] == 1800 and burned["timeout"] == burnjob.BURN_TIMEOUT
    assert [t["sectors"] for t in burned["tracks"]] == [375, 450, 400] and all(t["path"].endswith(".pcm") for t in burned["tracks"])
    assert next(args for cmd, args in sent if cmd == "eject")["drive"] == "E:"


def test_without_a_chosen_speed_the_recorder_is_left_to_choose(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []
    real = disc_helper.DiscHelper.call

    def spy(self, cmd, **args):
        if cmd == "burn_audio":
            seen.append(args)
        return real(self, cmd, **args)

    monkeypatch.setattr(disc_helper.DiscHelper, "call", spy)
    burn_it(rig)
    assert len(seen) == 1 and "speed" not in seen[0] and "eject" not in seen[0]


def test_what_is_in_the_drive_is_seen_again_as_soon_as_the_burn_is_over(rig) -> None:
    before = {d["id"]: d["media"] for d in rig.client.get("/api/discs/drives").json()["drives"]}
    job, _ = burn_it(rig)
    assert job.status == "done"
    after = {d["id"]: d["media"] for d in rig.client.get("/api/discs/drives").json()["drives"]}  # (not the list from a moment ago)
    assert (before["E:"], after["E:"]) == ("blank", "audio")


def test_while_a_disc_is_written_nothing_else_starts_a_second_helper_to_look_at_the_drives(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.client.get("/api/discs/drives")  # (the screen has looked once)
    set_control(rig.world, burn_slow=0.004)
    started = begin(rig)
    job_id = started["job"]["id"]
    _wait_until_writing(rig, job_id)
    starts: list[int] = []
    real = disc_helper.DiscHelper.start
    monkeypatch.setattr(disc_helper.DiscHelper, "start", lambda self: (starts.append(1), real(self))[1])
    try:
        for _ in range(3):
            assert rig.client.get("/api/discs/drives?fresh=true").status_code == 200
        assert starts == []
    finally:
        rig.client.post(f"/api/jobs/{job_id}/cancel")
        rig.ctx.jobs.wait(job_id, timeout=30)


def test_a_short_song_is_given_its_four_seconds_and_a_song_in_another_format_is_converted(rig) -> None:
    short = make_song(rig.music / "Burner" / "Mix", "Short", 14, 100)
    wave = np.stack([np.sin(2 * np.pi * 440 * np.arange(48000 * 6) / 48000) * 0.4] * 2, axis=1)
    odd = rig.music / "Burner" / "Mix" / "Odd.wav"
    sf.write(str(odd), wave, 48000, subtype="PCM_24")
    scan(rig.web)
    rig.ids = ids_by_title(rig.client)
    shown = preview(rig, "Short", "Odd").json()
    assert [(s["verdict"], s["sectors"]) for s in shown["songs"]] == [("ok", 300), ("converted", 450)]
    assert shown["songs"][0]["notes"] == ["Shorter than four seconds: silence is added to make four, as a music CD needs."]
    assert shown["songs"][1]["notes"] == ["Converted from 48 kHz to 44.1 kHz.", "24-bit sound is reduced to 16 bits."]
    job, _ = burn_it(rig, "Short", "Odd")
    assert job.status == "done", job.error
    first, second = rip_back(rig, 2)
    assert len(first) == (300 + 150) * 588 and np.array_equal(first[: 100 * 588], original(14, 100)) and not first[100 * 588:].any()
    middle = second[44100:-44100, 0].astype(np.float64) / 32768.0
    assert np.abs(middle).max() == pytest.approx(0.4, abs=0.01)
    assert short.exists() and odd.exists()  # the songs in the library are not touched


def test_the_same_song_may_be_on_the_disc_twice(rig) -> None:
    job, history = burn_it(rig, "One", "One")
    assert job.status == "done", job.error
    one, two = rip_back(rig, 2)
    assert np.array_equal(one[: 375 * 588], original(11, 375)) and np.array_equal(two[: 375 * 588], original(11, 375))
    assert [t["track_id"] for t in history["tracks"]] == [rig.ids["One"]] * 2


def test_the_disc_is_ejected_when_asked_and_only_then(rig) -> None:
    kept, _ = burn_it(rig, eject=False)
    assert kept.status == "done" and kept.result["ejected"] is False
    assert [d["media"] for d in rig.client.get("/api/discs/drives?fresh=true").json()["drives"] if d["id"] == "E:"] == ["audio"]
    add_drive(rig.world, "E", writer=True, disc=blank_disc())
    gone, _ = burn_it(rig, eject=True)
    assert gone.status == "done" and gone.result["ejected"] is True
    assert [d["media"] for d in rig.client.get("/api/discs/drives?fresh=true").json()["drives"] if d["id"] == "E:"] == ["none"]


def test_the_speed_asked_for_is_in_the_history(rig) -> None:
    _, history = burn_it(rig, speed=75 * 16, title="Road trip")
    assert history["speed"] == 1200 and history["title"] == "Road trip"


def test_the_history_lists_burns_newest_first(rig) -> None:
    burn_it(rig, "One")
    add_drive(rig.world, "E", writer=True, disc=blank_disc())
    burn_it(rig, "Two", "Three")
    listed = rig.client.get("/api/discs/burns").json()["burns"]
    assert [(b["title"], b["items"], b["status"]) for b in listed] == [("Music CD of 2 songs", 2, "done"), ("Music CD of 1 song", 1, "done")]
    assert rig.client.get("/api/discs/burns/99999").status_code == 404


# ---------------------------------------------------------------------------------------- when it goes wrong


def test_a_disc_changed_after_the_preview_is_refused_at_the_start(rig) -> None:
    add_drive(rig.world, "E", writer=True, disc=blank_disc())
    asked = ask(rig)
    assert rig.client.post("/api/discs/burn/preview", json=asked).status_code == 200
    add_drive(rig.world, "E", writer=True, disc=audio_disc(seed=2, tracks=(60,)))  # somebody changed the disc in between
    refused = rig.client.post("/api/discs/burn/start", json=asked)
    assert refused.status_code == 409 and "not blank" in refused.json()["detail"]
    assert rig.client.get("/api/discs/burns").json()["burns"] == []


def test_the_job_looks_at_the_disc_again_before_it_writes(rig) -> None:
    found = media(rig)
    songs = [SongInput(str(rig.music / "Burner" / "Mix" / "One.flac"), "One", "Burner", rig.ids["One"])]
    plan = burn.plan_audio(songs, found, scratch(rig))
    assert plan.problems == []
    request = burnjob.AudioRequest(drive="E:", eject=False)
    with rig.ctx.db() as conn:
        burn_id = burnjob.begin_audio(conn, plan, request, "abc123", found)
    add_drive(rig.world, "E", writer=True, disc=audio_disc(seed=2, tracks=(60,)))  # the disc was changed after the plan was made
    handle = SimpleNamespace(cancelled=False, update=lambda **_k: None, log=lambda _line: None, check=lambda: None)
    with pytest.raises(burn.BurnError, match="not blank"):
        burnjob.run_audio(db=rig.ctx.db, plan=plan, request=request, batch_id="abc123", burn_id=burn_id, scratch=scratch(rig), handle=handle)
    with rig.ctx.db() as conn:
        shown = store.get_burn(conn, burn_id)
    assert shown["status"] == "failed" and shown["spoiled"] == 0 and "not blank" in shown["message"]
    assert [t["status"] for t in shown["tracks"]] == ["failed"] and not scratch(rig).exists()
    assert (rig.world / "E" / "disc.pcm").exists() is False  # nothing was written


def test_a_disc_with_less_room_than_the_plan_counted_on_is_refused_before_it_is_written(rig) -> None:
    found = media(rig)
    songs = [SongInput(str(rig.music / "Burner" / "Mix" / f"{t}.flac"), t, "Burner", rig.ids[t]) for t, _, _ in SONGS]
    plan = burn.plan_audio(songs, found, scratch(rig))
    assert plan.problems == []
    request = burnjob.AudioRequest(drive="E:", eject=False)
    with rig.ctx.db() as conn:
        burn_id = burnjob.begin_audio(conn, plan, request, "tight", found)
    add_drive(rig.world, "E", writer=True, disc=blank_disc(capacity=1200))  # a smaller disc than the plan was made for
    handle = SimpleNamespace(cancelled=False, update=lambda **_k: None, log=lambda _line: None, check=lambda: None)
    with pytest.raises(burn.BurnError, match=r"^The songs take 0:20 and the disc has 0:16 free\. Take out about "):
        burnjob.run_audio(db=rig.ctx.db, plan=plan, request=request, batch_id="tight", burn_id=burn_id, scratch=scratch(rig), handle=handle)
    with rig.ctx.db() as conn:
        assert store.get_burn(conn, burn_id)["status"] == "failed"
    assert not scratch(rig).exists() and not (rig.world / "E" / "disc.pcm").exists()


def test_a_song_that_cannot_be_decoded_stops_the_burn_with_the_disc_untouched(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = burn.decode_to_pcm
    calls = []

    def second_fails(source, target, **kwargs):
        calls.append(source)
        if len(calls) == 2:
            raise burn.BurnError("Two.flac: the song could not be converted (boom).")
        return real(source, target, **kwargs)

    monkeypatch.setattr(burn, "decode_to_pcm", second_fails)
    job, history = burn_it(rig)
    assert job.status == "error" and job.error == "Two.flac: the song could not be converted (boom)."
    assert (history["status"], history["spoiled"], history["message"]) == ("failed", 0, "Two.flac: the song could not be converted (boom).")
    assert not scratch(rig).exists()
    assert media(rig)["blank"] is True  # the disc was never touched


def test_a_write_protected_disc_is_refused_and_nothing_is_spoiled(rig) -> None:
    set_control(rig.world, write_protected=True)
    job, history = burn_it(rig)
    assert job.status == "error" and job.error == "The disc is write protected. Nothing had been written, so the disc is as it was."
    assert (history["status"], history["spoiled"]) == ("failed", 0) and not scratch(rig).exists()
    assert [t["status"] for t in history["tracks"]] == ["failed"] * 3 and history["tracks"][0]["message"] == "Not written."
    set_control(rig.world)
    assert media(rig)["blank"] is True


def test_a_buffer_underrun_spoils_a_cd_r_and_says_so(rig) -> None:
    set_control(rig.world, burn_fail_at=600)
    job, history = burn_it(rig)
    assert job.status == "error"
    assert job.error == (
        "The drive did not get its data fast enough, and the disc is spoiled. Writing had begun, so the disc is probably unusable now. "
        "A CD-R cannot be written twice; put in a new disc."
    )
    assert (history["status"], history["spoiled"], history["message"]) == ("failed", 1, job.error)
    assert not scratch(rig).exists()
    got = media(rig)
    assert got["blank"] is False and got["audio_problems"][0].startswith("This CD-R is not blank")


def test_a_failed_burn_on_a_rewritable_disc_says_it_can_be_erased(rig) -> None:
    add_drive(rig.world, "E", writer=True, disc=blank_disc(type="CD-RW"))
    set_control(rig.world, burn_fail_at=600)
    job, history = burn_it(rig)
    assert job.error.endswith("It is rewritable: erase it (Erase, on this screen) and it can be used again.")
    assert media(rig)["audio_problems"] == ["This CD-RW already has something on it. Erase it first (Erase, on this screen), or put in a blank disc."]


def _wait_until_writing(rig, job_id: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if str(rig.ctx.jobs.get(job_id).message or "").startswith("Writing to the disc"):
            return
        time.sleep(0.02)
    raise AssertionError(f"the burn never began writing: {rig.ctx.jobs.get(job_id).to_dict()}")


def test_stopping_a_burn_while_it_writes_stops_it_and_says_what_became_of_the_disc(rig) -> None:
    set_control(rig.world, burn_slow=0.004)
    started = begin(rig)
    job_id = started["job"]["id"]
    _wait_until_writing(rig, job_id)
    assert rig.client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    job = rig.ctx.jobs.wait(job_id, timeout=30)
    assert job.status == "cancelled"
    history = rig.client.get(f"/api/discs/burns/{started['burn_id']}").json()
    assert (history["status"], history["spoiled"]) == ("cancelled", 1)
    assert history["message"] == "Stopped. Writing had begun, so the disc is probably unusable now. A CD-R cannot be written twice; put in a new disc."
    assert not scratch(rig).exists() and not rig.ctx.jobs.is_busy()
    assert media(rig)["blank"] is False


def test_stopping_while_the_songs_are_still_being_prepared_leaves_the_disc_as_it_was(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = burn.decode_to_pcm

    def stop_after_the_first(source, target, **kwargs):
        done = real(source, target, **kwargs)
        for running in rig.ctx.jobs.active():
            rig.ctx.jobs.cancel(running.id)  # the person presses Stop while the first song is ready
        return done

    monkeypatch.setattr(burn, "decode_to_pcm", stop_after_the_first)
    asked = ask(rig)
    rig.client.post("/api/discs/burn/preview", json=asked)
    started = rig.client.post("/api/discs/burn/start", json=asked).json()
    job = rig.ctx.jobs.wait(started["job"]["id"], timeout=30)
    assert job.status == "cancelled"
    history = rig.client.get(f"/api/discs/burns/{started['burn_id']}").json()
    assert (history["status"], history["spoiled"], history["message"]) == ("cancelled", 0, "Stopped. Nothing had been written, so the disc is as it was.")
    assert not scratch(rig).exists() and media(rig)["blank"] is True


def test_nothing_else_uses_the_drives_while_a_disc_is_being_burned(rig) -> None:
    set_control(rig.world, burn_slow=0.004)
    started = begin(rig)
    job_id = started["job"]["id"]
    _wait_until_writing(rig, job_id)
    try:
        assert rig.client.get("/api/discs/drives").json()["busy"] is True
        assert rig.client.get("/api/discs/media/E:").status_code == 409
        refused = rig.client.post("/api/discs/burn/preview", json=ask(rig))
        assert refused.status_code == 409 and refused.json()["detail"] == "A disc is being burned right now. Wait for it to finish, or stop it."
        assert rig.client.get("/api/discs/drives/F:/disc").json()["detail"] == "A disc is being burned right now. Wait for it to finish, or stop it."
        assert rig.client.post("/api/discs/eject", json={"drive": "E:"}).status_code == 409
    finally:
        rig.client.post(f"/api/jobs/{job_id}/cancel")
        rig.ctx.jobs.wait(job_id, timeout=30)
    assert rig.client.get("/api/discs/drives").json()["busy"] is False


def test_a_rip_in_progress_keeps_a_burn_from_starting(rig) -> None:
    add_drive(rig.world, "F", writer=False, disc=audio_disc(seed=3, tracks=(600, 600)))
    set_control(rig.world, slow=0.01)
    asked = {
        "drive": "F:", "root": str(rig.music), "scheme": SCHEME, "format": "flac", "quality": "standard", "accuracy": "fast", "album": "A", "album_artist": "B",
        "tracks": [{"number": 1, "title": "x"}, {"number": 2, "title": "y"}], "cover": False, "eject": False,
    }
    assert rig.client.post("/api/discs/rip/preview", json=asked).status_code == 200
    ripping = rig.client.post("/api/discs/rip/start", json=asked).json()["job"]["id"]
    try:
        assert preview(rig).status_code == 409
        assert rig.client.get("/api/discs/media/E:").json()["detail"] == "A disc is being ripped right now. Wait for it to finish, or stop it."
    finally:
        rig.client.post(f"/api/jobs/{ripping}/cancel")
        rig.ctx.jobs.wait(ripping, timeout=30)


# ---------------------------------------------------------------------------------------- the app was closed in the middle


def test_a_burn_that_was_running_when_the_app_stopped_is_marked_and_its_temporary_songs_removed(rig) -> None:
    folder = scratch(rig)
    folder.mkdir(parents=True)
    (folder / "dead-01.pcm").write_bytes(b"1234")
    (folder / "live-01.pcm").write_bytes(b"1234")
    with rig.ctx.db() as conn:
        quiet = store.create_burn(conn, batch_id="dead", kind="audio", title="Mix", drive="E:", items=[{"title": "A"}, {"title": "B"}])
        wrote = store.create_burn(conn, batch_id="wrote", kind="audio", title="Mix 2", drive="E:", items=[{"title": "C"}])
        store.mark_spoiled(conn, wrote)
        assert store.recover_burns(conn, folder) == 2
        shown, spoiled = store.get_burn(conn, quiet), store.get_burn(conn, wrote)
    assert shown["status"] == "interrupted" and shown["message"] == "The app was closed before the burn was finished." and shown["finished_at"]
    assert [t["status"] for t in shown["tracks"]] == ["failed", "failed"]
    assert spoiled["message"].endswith("Writing had begun, so the disc cannot be trusted. A rewritable disc can be erased and used again.")
    assert not (folder / "dead-01.pcm").exists() and (folder / "live-01.pcm").exists()  # (only that burn's songs)
    with rig.ctx.db() as conn:
        assert store.recover_burns(conn, folder) == 0


def test_the_app_tidies_up_after_a_burn_that_was_running_when_it_was_last_closed(rig, tmp_path: Path) -> None:
    folder = scratch(rig)
    folder.mkdir(parents=True)
    (folder / "gone-01.pcm").write_bytes(b"1234")
    with rig.ctx.db() as conn:
        burn_id = store.create_burn(conn, batch_id="gone", kind="audio", title="Mix", drive="E:", items=[{"title": "A"}])
    again = build_web(tmp_path, rig.music)  # the app starts again over the same library
    shown = again.client.get(f"/api/discs/burns/{burn_id}").json()
    assert shown["status"] == "interrupted" and shown["message"] == "The app was closed before the burn was finished."
    assert not (folder / "gone-01.pcm").exists()


def test_a_finished_burn_is_not_marked_spoiled_even_though_writing_began(rig) -> None:
    with rig.ctx.db() as conn:
        burn_id = store.create_burn(conn, batch_id="ok", kind="audio", title="Mix", drive="E:", items=[{"title": "A"}])
        store.mark_spoiled(conn, burn_id)
        store.finish_burn(conn, burn_id, "done", None, sectors=1234)
        shown = store.get_burn(conn, burn_id)
    assert (shown["status"], shown["spoiled"], shown["sectors"]) == ("done", 0, 1234) and [t["status"] for t in shown["tracks"]] == ["done"]
