"""Burning songs only Windows can decode (M4A, AAC, WMA): the disc helper is asked what each is like, decodes it to a temporary WAV, and from there
it is a song like any other. The stand-in recorder has a Windows of its own for this (tests/fake_disc_helper.py): a "song" it can decode is eight
bytes saying what it is and then a plain WAV file with the sound, so what goes in can be compared with what comes out of the disc."""

from __future__ import annotations

import io
import re
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from musictoolkit.discs import burn, burnjob, store
from musictoolkit.discs import helper as disc_helper
from musictoolkit.discs.burn import SongInput
from musictoolkit.discs.helper import DiscUnavailable
from tests import test_web_discs_burn as base
from tests.conftest import ids_by_title
from tests.fake_disc_helper import audio, fake_song, set_control
from tests.test_web_discs_burn import begin, burn_it, media, original, preview, rip_back, scratch

rig = base.rig  # (the same stand-in recorder, library and app as the main tests of burning)


def wav_bytes(pcm: bytes, rate: int = 44100, channels: int = 2) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as sound:
        sound.setnchannels(channels)
        sound.setsampwidth(2)
        sound.setframerate(rate)
        sound.writeframes(pcm)
    return out.getvalue()


def windows_song(rig, title: str, seed: int, sectors: int, *, kind: bytes = b"FAKEAAC1", extension: str = "m4a", pcm: bytes | None = None, rate: int = 44100,
                 channels: int = 2) -> Path:
    """A song in the library that the audio library cannot read and the stand-in's Windows can: its sound is the stand-in's own audio for this seed."""
    path = fake_song(rig.music / "Burner" / "Mix" / f"{title}.{extension}", wav_bytes(pcm if pcm is not None else audio(seed, 0, sectors), rate, channels), kind)
    add_track(rig, path, title)
    return path


def add_track(rig, path: Path, title: str) -> None:
    with rig.ctx.db() as conn:
        conn.execute("INSERT INTO tracks (file_path, title, artist) VALUES (?, ?, 'Burner')", (str(path), title))
        conn.commit()
    rig.ids = ids_by_title(rig.client)


class Handle:
    def __init__(self) -> None:
        self.cancelled = False
        self.updates: list[dict] = []

    def check(self) -> None:
        if self.cancelled:
            from musictoolkit.web.jobs import JobCancelled
            raise JobCancelled()

    def update(self, **fields) -> None:
        self.updates.append(fields)

    def log(self, line: str) -> None:
        pass


# ---------------------------------------------------------------------------------------- looking before burning


def test_a_song_windows_decodes_is_shown_as_what_windows_said_and_goes_on_the_disc_as_it_sounds(rig) -> None:
    windows_song(rig, "Four", 14, 400)
    shown = preview(rig, "One", "Four").json()
    four = shown["songs"][1]
    assert (four["verdict"], four["sectors"], four["notes"]) == ("converted", 400, ["Decoded by Windows (AAC) and made into CD sound."])
    assert shown["ok"] is True and shown["counts"] == {"ok": 1, "converted": 1, "cannot": 0} and shown["disc_sectors"] == 375 + 400 + 150
    job, history = burn_it(rig, "One", "Four")
    assert job.status == "done", job.error
    assert [(t["position"], t["title"], t["status"], t["sectors"]) for t in history["tracks"]] == [(1, "One", "done", 375), (2, "Four", "done", 400)]
    first, second = rip_back(rig, 2)
    assert np.array_equal(first[: 375 * 588], original(11, 375)) and np.array_equal(second[: 400 * 588], original(14, 400))  # (bit for bit: Windows gave CD sound)
    assert not scratch(rig).exists()  # the decoded copy and the CD sound made from it are both gone


def test_songs_of_every_kind_go_on_in_the_order_they_were_chosen(rig) -> None:
    windows_song(rig, "Wma", 15, 380, kind=b"FAKEWMA1", extension="wma")
    windows_song(rig, "Aac", 16, 420, extension="aac")
    shown = preview(rig, "Aac", "One", "Wma").json()
    assert [(s["title"], s["verdict"], s["notes"][:1]) for s in shown["songs"]] == [
        ("Aac", "converted", ["Decoded by Windows (AAC) and made into CD sound."]), ("One", "ok", []), ("Wma", "converted", ["Decoded by Windows (WMA) and made into CD sound."]),
    ]
    job, _ = burn_it(rig, "Aac", "One", "Wma", gapless=True)
    assert job.status == "done", job.error
    heard = rip_back(rig, 3)
    for (seed, sectors), samples in zip(((16, 420), (11, 375), (15, 380)), heard):
        assert np.array_equal(samples, original(seed, sectors))


def test_a_song_windows_decodes_at_another_rate_in_mono_is_made_into_cd_sound(rig) -> None:
    rate, seconds = 22050, 6
    tone = (0.4 * 32767 * np.sin(2 * np.pi * 440 * np.arange(rate * seconds) / rate)).astype("<i2")
    windows_song(rig, "Mono", 0, 0, pcm=tone.tobytes(), rate=rate, channels=1)
    shown = preview(rig, "Mono").json()["songs"][0]
    assert shown["notes"] == ["Decoded by Windows (AAC) and made into CD sound.", "Converted from 22.05 kHz to 44.1 kHz.", "Mono: both speakers get the same sound."]
    assert shown["sectors"] == 450
    job, _ = burn_it(rig, "Mono")
    assert job.status == "done", job.error
    (heard,) = rip_back(rig, 1)
    assert np.abs(heard[:, 0].astype(int) - heard[:, 1]).max() <= 2 and len(heard) == 450 * 588  # (the same sound in both; each channel has its own dither, a bit of noise a thousandth of a decibel)
    middle = heard[44100:-44100, 0].astype(np.float64) / 32768.0
    assert np.abs(middle).max() == pytest.approx(0.4, abs=0.01)


def test_the_helper_is_asked_once_about_all_the_songs_only_windows_decodes_and_not_at_all_when_there_are_none(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    four, five = windows_song(rig, "Four", 14, 400), windows_song(rig, "Five", 15, 400, extension="wma", kind=b"FAKEWMA1")
    asked: list[list[str]] = []
    real = disc_helper.DiscHelper.call

    def spy(self, cmd, **args):
        if cmd == "inspect":
            asked.append(args["paths"])
        return real(self, cmd, **args)

    monkeypatch.setattr(disc_helper.DiscHelper, "call", spy)
    assert preview(rig, "One", "Four", "Five", "Four", "Two").status_code == 200
    assert len(asked) == 1 and sorted(asked[0]) == sorted([str(four), str(five)])  # (the same song twice is asked about once)
    asked.clear()
    assert preview(rig, "One", "Two").status_code == 200
    assert asked == []


def test_songs_windows_will_not_decode_are_listed_in_its_words_and_left_off_the_disc(rig) -> None:
    locked = windows_song(rig, "Locked", 14, 400, kind=b"FAKEDRM1")
    junk = rig.music / "Burner" / "Mix" / "Junk.wma"
    junk.write_bytes(b"this is no song")
    add_track(rig, junk, "Junk")
    shown = preview(rig, "One", "Locked", "Junk", "Two").json()
    assert [s["verdict"] for s in shown["songs"]] == ["ok", "cannot", "cannot", "ok"] and shown["ok"] is True
    assert shown["songs"][1]["notes"] == ["Windows cannot decode Locked.m4a: it is copy-protected."]
    assert shown["songs"][2]["notes"] == ["Windows cannot read this kind of file (Junk.wma)."]
    job, history = burn_it(rig, "One", "Locked", "Junk", "Two")
    assert job.status == "done", job.error
    assert [(t["title"], t["status"]) for t in history["tracks"]] == [("One", "done"), ("Locked", "skipped"), ("Junk", "skipped"), ("Two", "done")]
    assert history["tracks"][1]["message"] == "Windows cannot decode Locked.m4a: it is copy-protected." and locked.exists() and junk.exists()
    assert [len(s) // 588 for s in rip_back(rig, 2)] == [375 + 150, 450]


def test_a_windows_without_the_decoders_says_so_for_each_song_and_the_others_still_go(rig) -> None:
    windows_song(rig, "Four", 14, 400)
    set_control(rig.world, no_decoder=True)
    shown = preview(rig, "One", "Four").json()
    assert [s["verdict"] for s in shown["songs"]] == ["ok", "cannot"] and shown["ok"] is True
    assert "Media Foundation" in shown["songs"][1]["notes"][0] and "Media Feature Pack" in shown["songs"][1]["notes"][0]
    assert burn_it(rig, "One", "Four")[0].status == "done"


def test_a_helper_that_cannot_be_asked_leaves_the_songs_it_would_decode_unburnable_in_words(rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Nowhere:
        def __enter__(self):
            raise DiscUnavailable("There is no disc helper on this computer.")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(burn, "DiscHelper", Nowhere)
    gone = tmp_path / "a.m4a"
    got = burn.check_with_windows([str(gone), str(gone), str(tmp_path / "b.wma")])
    assert sorted(got) == sorted([str(gone), str(tmp_path / "b.wma")])
    assert all(c.verdict == "cannot" and c.via == "windows" and c.sectors == 0 for c in got.values())
    assert got[str(gone)].notes == ["Windows could not be asked to decode this song (There is no disc helper on this computer)."]
    assert burn.check_with_windows([]) == {}


def test_what_the_plan_asks_windows_is_settled_before_the_plan_is_made(rig, tmp_path: Path) -> None:
    four = windows_song(rig, "Four", 14, 400)
    songs = [SongInput(str(four), "Four", "Burner", rig.ids["Four"]), SongInput(str(rig.music / "Burner" / "Mix" / "One.flac"), "One", "Burner", rig.ids["One"])]
    seen: list[list[str]] = []

    def windows(paths: list[str]) -> dict:
        seen.append(paths)
        return {paths[0]: burn.windows_song(paths[0], {"ok": True, "codec": "AAC", "rate": 44100, "channels": 2, "seconds": 5.0})}

    plan = burn.plan_audio(songs, media(rig), scratch(rig), windows=windows)
    assert seen == [[str(four)]] and [s.check.verdict for s in plan.songs] == ["converted", "ok"] and not any(s.check.pending for s in plan.songs)
    # a song windows said nothing about is one that cannot be burned, never one that is guessed at
    plan = burn.plan_audio(songs, media(rig), scratch(rig), windows=lambda paths: {})
    assert plan.songs[0].check.verdict == "cannot" and plan.songs[0].check.notes == ["Windows could not say what is in this song."]
    # and the room for temporary copies counts the decoded copy of the biggest such song (floating point, at worst, is twice the size)
    assert plan.scratch_bytes == plan.song_sectors * 2352
    big = burn.plan_audio(songs, media(rig), scratch(rig), windows=lambda paths: {paths[0]: burn.windows_song(paths[0], {"ok": True, "rate": 44100, "channels": 2, "seconds": 10.0})})
    assert big.scratch_bytes == big.song_sectors * 2352 + big.songs[0].check.sectors * 2352 * 2


# ---------------------------------------------------------------------------------------- the job


def test_while_windows_decodes_a_song_the_progress_goes_through_both_halves(rig) -> None:
    windows_song(rig, "Four", 14, 400)
    found = media(rig)
    songs = [SongInput(str(rig.music / "Burner" / "Mix" / "Four.m4a"), "Four", "Burner", rig.ids["Four"])]
    plan = burn.plan_audio(songs, found, scratch(rig))
    request = burnjob.AudioRequest(drive="E:", eject=False)
    with rig.ctx.db() as conn:
        burn_id = burnjob.begin_audio(conn, plan, request, "decoding", found)
    handle = Handle()
    burnjob.run_audio(db=rig.ctx.db, plan=plan, request=request, batch_id="decoding", burn_id=burn_id, scratch=scratch(rig), handle=handle)
    percents = [int(m.group(1)) for u in handle.updates if (m := re.search(r"Preparing song 1 of 1: Four \((\d+)%\)", str(u.get("message", ""))))]
    assert percents == sorted(percents) and percents[0] <= 25 and percents[-1] == 100
    first_half = [p for p in percents if p <= 50]
    assert first_half and max(first_half) == 50  # Windows' decoding is the first half of the song's preparation, the conversion the second
    assert len(first_half) >= 4  # (the stand-in reports a quarter at a time)


def test_stopping_while_windows_decodes_a_song_leaves_the_disc_as_it_was(rig) -> None:
    windows_song(rig, "Four", 14, 400)
    set_control(rig.world, decode_slow=0.3)
    started = begin(rig, "Four")
    job_id = started["job"]["id"]
    deadline = time.monotonic() + 30
    while "%" not in str(rig.ctx.jobs.get(job_id).message or ""):  # (Windows has begun on the song)
        assert time.monotonic() < deadline, rig.ctx.jobs.get(job_id).to_dict()
        time.sleep(0.02)
    rig.client.post(f"/api/jobs/{job_id}/cancel")
    job = rig.ctx.jobs.wait(job_id, timeout=30)
    shown = rig.client.get(f"/api/discs/burns/{started['burn_id']}").json()
    assert job.status == "cancelled" and (shown["status"], shown["spoiled"]) == ("cancelled", 0)
    assert shown["message"] == "Stopped. Nothing had been written, so the disc is as it was." and not scratch(rig).exists()
    assert media(rig)["blank"] is True


def test_a_song_windows_cannot_decode_after_all_stops_the_burn_with_the_disc_untouched(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    windows_song(rig, "Four", 14, 400)
    real = disc_helper.DiscHelper.call

    def spy(self, cmd, **args):
        if cmd == "decode":
            set_control(rig.world, no_decoder=True)  # (it could be asked about, and then it cannot: the decoder went away)
        return real(self, cmd, **args)

    monkeypatch.setattr(disc_helper.DiscHelper, "call", spy)
    job, history = burn_it(rig, "One", "Four")
    assert job.status == "error" and "Media Foundation" in job.error and job.error.endswith("Nothing had been written, so the disc is as it was.")
    assert (history["status"], history["spoiled"]) == ("failed", 0) and not scratch(rig).exists() and media(rig)["blank"] is True


def test_the_decoded_copies_are_gone_when_the_burn_fails_later(rig) -> None:
    windows_song(rig, "Four", 14, 400)
    set_control(rig.world, write_protected=True)
    job, history = burn_it(rig, "Four")
    assert job.status == "error" and history["status"] == "failed" and history["spoiled"] == 0
    assert not scratch(rig).exists()


def test_the_decoded_copies_of_a_burn_cut_short_are_tidied_up_with_the_rest(rig) -> None:
    folder = scratch(rig)
    folder.mkdir(parents=True)
    with rig.ctx.db() as conn:
        burn_id = store.create_burn(conn, batch_id="cutshort", kind="audio", title="Cut short", drive="E:", items=[])
        other = folder / "another-01.pcm"
        other.write_bytes(b"x")
        left = [folder / "cutshort-01.pcm", folder / "cutshort-01.wav", folder / "cutshort-02.wav.part"]
        for path in left:
            path.write_bytes(b"x")
        assert store.recover_burns(conn, folder) == 1
    assert [p.exists() for p in left] == [False, False, False] and other.exists()
    burn.forget_scratch(folder, "another")
    assert not other.exists()
    del burn_id
