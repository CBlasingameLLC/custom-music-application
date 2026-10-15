"""Writing discs: what the helper is asked to do (media, burn_audio, burn_data, build_iso, erase) and how the stand-in drive answers, which is
how the real helper has to answer. A disc that was burned can be read back, so what went in can be compared with what comes out."""

from __future__ import annotations

import base64
import sys
import threading
import time
from pathlib import Path

import pytest

from musictoolkit.discs import helper as disc_helper
from musictoolkit.discs.helper import DiscError, DiscHelper
from tests.fake_disc_helper import CD_BLANK_SECTORS, SECTOR_BYTES, add_drive, audio, audio_disc, blank_disc, set_control

FAKE = Path(__file__).parent / "fake_disc_helper.py"


@pytest.fixture(autouse=True)
def fresh_listing():
    disc_helper.forget_listing()
    yield
    disc_helper.forget_listing()


@pytest.fixture
def world(tmp_path: Path) -> Path:
    root = tmp_path / "drives"
    add_drive(root, "E", writer=True, disc=blank_disc(type="CD-R"))
    add_drive(root, "F", writer=False, disc=audio_disc(seed=3, tracks=(20, 30)))
    return root


def start(root: Path) -> DiscHelper:
    return DiscHelper([sys.executable, str(FAKE), str(root)])


def pcm(tmp_path: Path, name: str, sectors: int, seed: int = 5) -> Path:
    """A song as the drive wants it: 44.1 kHz, 16 bit, stereo, in whole sectors."""
    path = tmp_path / name
    path.write_bytes(audio(seed, 1000 * seed, sectors))
    return path


def read(helper: DiscHelper, lba: int, count: int, drive: str = "E:") -> bytes:
    return base64.b64decode(helper.call("read", drive=drive, lba=lba, count=count)["data"])


# ---------------------------------------------------------------------------------------- what is in the drive


@pytest.mark.parametrize("code", ["not_blank", "write_protected", "no_space", "underrun"])
def test_a_refusal_the_recorder_can_make_has_words_of_its_own(code: str) -> None:
    """The real helper sends IMAPI's own words when it has some; when it sends only a code, the person still gets a sentence that says what
    to do, not "The drive did not accept that."."""
    words = str(DiscError(code))
    assert words == disc_helper.MESSAGES[code] and words.endswith((".", "?"))
    assert words != str(DiscError("some_code_nobody_knows"))
    assert str(DiscError(code, "IMAPI said so")) == "IMAPI said so"  # the helper's own words win


def test_the_listing_says_which_drives_can_write_and_what(world: Path) -> None:
    with start(world) as helper:
        drives = {d["id"]: d for d in helper.call("drives")["drives"]}
    assert drives["E:"]["writer"] is True and drives["E:"]["writes"] == ["CD-R", "CD-RW"] and drives["E:"]["media"] == "unknown"  # (a blank disc has no table of contents: to the listing it is a disc the drive cannot read, as with the real helper)
    assert drives["F:"]["writer"] is False and drives["F:"]["writes"] == []


def test_a_blank_disc_says_what_it_is_and_how_much_it_holds(world: Path) -> None:
    with start(world) as helper:
        media = helper.call("media", drive="E:")
    assert (media["state"], media["type"], media["blank"], media["rewritable"]) == ("blank", "CD-R", True, False)
    assert media["capacity_sectors"] == media["free_sectors"] == CD_BLANK_SECTORS and media["can_audio"] is True
    assert [s["label"] for s in media["speeds"]] == ["8x", "16x", "24x", "32x", "48x"] and media["speeds"][0]["speed"] == 600


def test_a_pressed_disc_and_an_empty_tray_are_not_blank(world: Path) -> None:
    add_drive(world, "G", writer=True)
    with start(world) as helper:
        pressed = helper.call("media", drive="F:")
        empty = helper.call("media", drive="G:")
    assert (pressed["state"], pressed["blank"], pressed["free_sectors"], pressed["can_audio"], pressed["writer"]) == ("written", False, 0, False, False)
    assert (empty["state"], empty["blank"], empty["free_sectors"], empty["can_audio"], empty["writer"]) == ("none", False, 0, False, True)


# ---------------------------------------------------------------------------------------- music


def test_a_burned_music_disc_reads_back_exactly_what_was_written_with_two_seconds_between_songs(world: Path, tmp_path: Path) -> None:
    first, second = pcm(tmp_path, "one.pcm", 400, seed=5), pcm(tmp_path, "two.pcm", 500, seed=6)
    seen: list[dict] = []
    with start(world) as helper:
        done = helper.call("burn_audio", drive="E:", tracks=[{"path": str(first), "sectors": 400}, {"path": str(second), "sectors": 500}], on_event=seen.append)
        assert done["tracks"] == 2
        toc = helper.call("toc", drive="E:")
        assert [(t["number"], t["audio"], t["start"]) for t in toc["tracks"]] == [(1, True, 0), (2, True, 550)] and toc["leadout"] == 1050
        assert read(helper, 0, 16) == first.read_bytes()[: 16 * SECTOR_BYTES]
        assert read(helper, 550, 16) == second.read_bytes()[: 16 * SECTOR_BYTES]
        assert read(helper, 400, 16) == b"\0" * 16 * SECTOR_BYTES, "the two seconds between the songs are silence"
        assert helper.call("media", drive="E:")["state"] == "written"
    assert seen and seen[-1]["done"] == seen[-1]["total"] == 1050 and seen[0]["message"] == "Song 1 of 2"


def test_a_song_shorter_than_four_seconds_is_made_four_seconds_long(world: Path, tmp_path: Path) -> None:
    short = pcm(tmp_path, "short.pcm", 100)
    with start(world) as helper:
        helper.call("burn_audio", drive="E:", tracks=[{"path": str(short), "sectors": 100}])
        assert helper.call("toc", drive="E:")["leadout"] == 300


def test_music_goes_only_onto_a_blank_cd_that_the_drive_writes(world: Path, tmp_path: Path) -> None:
    song = pcm(tmp_path, "one.pcm", 400)
    tracks = [{"path": str(song), "sectors": 400}]
    add_drive(world, "G", writer=True, disc=blank_disc(type="DVD-R", capacity=2_295_104))
    add_drive(world, "H", writer=True, disc={**audio_disc(seed=3, tracks=(20,)), "type": "CD-R"})
    add_drive(world, "I", writer=True)
    with start(world) as helper:
        for drive, code in (("F:", "unsupported"), ("G:", "unsupported"), ("H:", "not_blank"), ("I:", "no_media")):
            with pytest.raises(DiscError) as refused:
                helper.call("burn_audio", drive=drive, tracks=tracks)
            assert refused.value.code == code, drive


def test_too_much_music_for_the_disc_is_refused_before_anything_is_written(world: Path, tmp_path: Path) -> None:
    add_drive(world, "G", writer=True, disc=blank_disc(type="CD-R", capacity=800))
    songs = [{"path": str(pcm(tmp_path, f"{n}.pcm", 400)), "sectors": 400} for n in range(3)]
    with start(world) as helper:
        with pytest.raises(DiscError) as refused:
            helper.call("burn_audio", drive="G:", tracks=songs)
        assert refused.value.code == "no_space" and "800" in str(refused.value)
        assert helper.call("media", drive="G:")["blank"] is True, "the disc is still blank"


@pytest.mark.parametrize("tracks", [[], [{"path": "nowhere.pcm"}], [{"path": "{odd}"}]], ids=["none", "missing file", "not whole sectors"])
def test_songs_that_cannot_be_written_are_refused(world: Path, tmp_path: Path, tracks: list) -> None:
    odd = tmp_path / "odd.pcm"
    odd.write_bytes(b"\0" * (SECTOR_BYTES + 1))
    tracks = [{**t, "path": str(odd)} if t.get("path") == "{odd}" else t for t in tracks]
    with start(world) as helper:
        with pytest.raises(DiscError) as refused:
            helper.call("burn_audio", drive="E:", tracks=tracks)
        assert refused.value.code == "failed"
        assert helper.call("media", drive="E:")["blank"] is True


def test_a_burn_that_fails_half_way_spoils_a_cd_r_and_a_cd_rw_can_be_erased(world: Path, tmp_path: Path) -> None:
    add_drive(world, "G", writer=True, disc=blank_disc(type="CD-RW"))
    songs = [{"path": str(pcm(tmp_path, f"{n}.pcm", 400)), "sectors": 400} for n in range(2)]
    set_control(world, burn_fail_at=500)
    with start(world) as helper:
        for drive in ("E:", "G:"):
            with pytest.raises(DiscError) as refused:
                helper.call("burn_audio", drive=drive, tracks=songs)
            assert refused.value.code == "underrun"
            assert helper.call("media", drive=drive)["blank"] is False
        with pytest.raises(DiscError) as locked:
            helper.call("erase", drive="E:")
        assert locked.value.code == "unsupported", "a CD-R cannot be erased"
        helper.call("erase", drive="G:")
        assert helper.call("media", drive="G:")["blank"] is True


def test_a_burn_that_is_stopped_spoils_the_disc_and_says_it_was_stopped(world: Path, tmp_path: Path) -> None:
    songs = [{"path": str(pcm(tmp_path, f"{n}.pcm", 600)), "sectors": 600} for n in range(2)]
    set_control(world, burn_slow=0.01)
    with start(world) as helper:
        threading.Timer(0.5, helper.cancel).start()
        started = time.monotonic()
        with pytest.raises(DiscError) as stopped:
            helper.call("burn_audio", drive="E:", tracks=songs)
        assert stopped.value.code == "cancelled" and time.monotonic() - started < 10
        assert helper.call("media", drive="E:")["blank"] is False


def test_a_write_protected_disc_is_refused(world: Path, tmp_path: Path) -> None:
    set_control(world, write_protected=True)
    song = {"path": str(pcm(tmp_path, "one.pcm", 400)), "sectors": 400}
    with start(world) as helper:
        with pytest.raises(DiscError) as refused:
            helper.call("burn_audio", drive="E:", tracks=[song])
        assert refused.value.code == "write_protected"


def test_the_disc_can_be_ejected_when_the_burn_is_done(world: Path, tmp_path: Path) -> None:
    song = {"path": str(pcm(tmp_path, "one.pcm", 400)), "sectors": 400}
    with start(world) as helper:
        helper.call("burn_audio", drive="E:", tracks=[song], eject=True)
        assert helper.call("media", drive="E:")["state"] == "none"
    assert (world / "E" / "eject.log").exists()


# ---------------------------------------------------------------------------------------- files


def test_files_burned_to_a_disc_are_on_it_under_the_names_they_were_given(world: Path, tmp_path: Path) -> None:
    one, two = tmp_path / "a.mp3", tmp_path / "b.mp3"
    one.write_bytes(b"first song" * 1000)
    two.write_bytes(b"second song" * 3000)
    with start(world) as helper:
        done = helper.call("burn_data", drive="E:", label="MY MUSIC", files=[{"src": str(one), "dest": "Artist/Album/01 One.mp3"}, {"src": str(two), "dest": "02 Two.mp3"}])
        assert done["files"] == 2
        media = helper.call("media", drive="E:")
    root = Path(media["root"])
    assert (root / "Artist" / "Album" / "01 One.mp3").read_bytes() == one.read_bytes() and (root / "02 Two.mp3").read_bytes() == two.read_bytes()
    assert media["state"] == "written" and media["blank"] is False
    with start(world) as helper:
        assert {d["id"]: d["media"] for d in helper.call("drives")["drives"]}["E:"] == "data"


@pytest.mark.parametrize("dest", ["../outside.mp3", "..\\outside.mp3", "C:/outside.mp3", "", "/"], ids=["up", "up (backslash)", "drive", "empty", "root"])
def test_a_name_that_leaves_the_disc_is_refused(world: Path, tmp_path: Path, dest: str) -> None:
    song = tmp_path / "a.mp3"
    song.write_bytes(b"x" * 100)
    with start(world) as helper:
        with pytest.raises(DiscError) as refused:
            helper.call("burn_data", drive="E:", label="X", files=[{"src": str(song), "dest": dest}])
        assert refused.value.code == "failed"
        assert helper.call("media", drive="E:")["blank"] is True, "a name is looked at before the disc is touched"
    assert not (tmp_path / "outside.mp3").exists()


def test_files_that_do_not_fit_are_refused_and_a_disc_that_is_not_blank_is_not_written(world: Path, tmp_path: Path) -> None:
    big = tmp_path / "big.bin"
    big.write_bytes(b"\0" * (2048 * 300))
    add_drive(world, "G", writer=True, disc=blank_disc(type="CD-R", capacity=200))
    with start(world) as helper:
        with pytest.raises(DiscError) as full:
            helper.call("burn_data", drive="G:", label="X", files=[{"src": str(big), "dest": "big.bin"}])
        assert full.value.code == "no_space"
        helper.call("burn_data", drive="E:", label="X", files=[{"src": str(big), "dest": "big.bin"}])
        with pytest.raises(DiscError) as used:
            helper.call("burn_data", drive="E:", label="Y", files=[{"src": str(big), "dest": "again.bin"}])
        assert used.value.code == "not_blank"


# ---------------------------------------------------------------------------------------- an image, and erasing


def test_an_image_is_written_in_whole_sectors_and_never_over_a_file_that_is_there(world: Path, tmp_path: Path) -> None:
    song = tmp_path / "a.mp3"
    song.write_bytes(b"x" * 5000)
    out = tmp_path / "disc.iso"
    with start(world) as helper:  # (no drive is needed)
        done = helper.call("build_iso", label="MUSIC", files=[{"src": str(song), "dest": "Album/a.mp3"}], out=str(out))
        assert done["bytes"] == out.stat().st_size == done["sectors"] * 2048 and out.stat().st_size % 2048 == 0
        for out_again, message in ((out, "already"), (tmp_path / "disc.txt", "image"), (tmp_path / "nowhere" / "x.iso", "image")):
            with pytest.raises(DiscError) as refused:
                helper.call("build_iso", label="MUSIC", files=[{"src": str(song), "dest": "a.mp3"}], out=str(out_again))
            assert message in str(refused.value)


def test_a_rewritable_disc_is_erased_to_blank_and_a_disc_that_is_blank_already_stays(world: Path, tmp_path: Path) -> None:
    add_drive(world, "G", writer=True, disc=blank_disc(type="CD-RW"))
    song = tmp_path / "a.mp3"
    song.write_bytes(b"x" * 5000)
    with start(world) as helper:
        helper.call("erase", drive="G:")
        helper.call("burn_data", drive="G:", label="X", files=[{"src": str(song), "dest": "a.mp3"}])
        assert helper.call("media", drive="G:")["blank"] is False
        helper.call("erase", drive="G:", full=True)
        media = helper.call("media", drive="G:")
        assert media["blank"] is True and media["free_sectors"] == CD_BLANK_SECTORS and not (world / "G" / "files").exists()
        with pytest.raises(DiscError) as refused:
            helper.call("erase", drive="F:")
        assert refused.value.code == "unsupported"
