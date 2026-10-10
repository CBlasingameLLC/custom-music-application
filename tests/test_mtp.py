"""Talking to a phone or player without a drive letter: the helper's conversation, and a device used like a folder."""

from __future__ import annotations

import errno
import json
import sys
from pathlib import Path

import pytest

from musictoolkit.sync import mtp
from musictoolkit.sync.mtp import MtpHelper, MtpTarget, MtpUnavailable
from musictoolkit.sync.targets import Target, TargetError

FAKE = Path(__file__).parent / "fake_mtp_helper.py"
SERIAL = "SER123"


def write_device(root: Path, serial: str = SERIAL, capacity: int = 10_000_000, name: str = "Pixel") -> Path:
    folder = root / serial
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "device.json").write_text(json.dumps({
        "name": name, "manufacturer": "Google", "model": "Pixel 8",
        "storages": [{"id": "s1", "name": "Internal storage", "capacity": capacity}],
    }))
    return folder


def control(root: Path, **settings) -> None:
    (root / "control.json").write_text(json.dumps(settings))


@pytest.fixture
def phones(tmp_path: Path) -> Path:
    root = tmp_path / "phones"
    write_device(root)
    return root


@pytest.fixture
def helper(phones: Path):
    with MtpHelper([sys.executable, str(FAKE), str(phones)]) as running:
        yield running


@pytest.fixture
def target(helper) -> MtpTarget:
    return MtpTarget.connect(helper, serial=SERIAL, storage="s1", base="Music")


def song(tmp_path: Path, name: str = "one.mp3", size: int = 300) -> Path:
    path = tmp_path / name
    path.write_bytes(bytes(range(256)) * (size // 256 + 1))
    path.write_bytes(path.read_bytes()[:size])
    return path


# ------------------------------------------------------------------------------------------- the conversation


def test_the_helper_lists_the_devices_that_are_plugged_in(helper) -> None:
    (device,) = helper.call("devices")["devices"]

    assert (device["name"], device["serial"], device["model"]) == ("Pixel", SERIAL, "Pixel 8")
    assert device["storages"] == [{"id": "s1", "name": "Internal storage", "capacity": 10_000_000, "free": 10_000_000}]


def test_a_refusal_comes_back_as_an_oserror_with_the_right_kind(helper, phones, tmp_path) -> None:
    helper.call("open", device=f"fake:{SERIAL}")
    with pytest.raises(TargetError) as missing:
        helper.call("list", storage="s1", path="Nowhere")
    assert missing.value.errno == errno.ENOENT and isinstance(missing.value, OSError)

    write_device(phones, "TINY", capacity=100, name="Tiny")
    helper.call("open", device="fake:TINY")
    with pytest.raises(TargetError) as full:
        helper.call("put", storage="s1", path="Music/big.mp3", source=str(song(tmp_path, size=500)))
    assert full.value.errno == errno.ENOSPC and "enough room" in str(full.value)


def test_stray_output_from_a_driver_is_not_mistaken_for_an_answer(helper, phones) -> None:
    control(phones, noise=True)
    assert helper.call("devices")["devices"][0]["serial"] == SERIAL


def test_a_helper_that_stops_is_reported_instead_of_waited_for(tmp_path) -> None:
    dying = [sys.executable, "-c", "import sys; print('{\"event\": \"ready\", \"version\": 1}', flush=True); sys.stdin.readline(); sys.exit(3)"]
    with MtpHelper(dying) as running:
        with pytest.raises(TargetError) as stopped:
            running.call("devices")
    assert stopped.value.errno == errno.ENODEV and "stopped" in str(stopped.value)


def test_a_request_that_is_never_answered_is_given_up_on(phones, monkeypatch) -> None:
    monkeypatch.setattr(mtp, "CALL_TIMEOUT", 0.6)
    with MtpHelper([sys.executable, str(FAKE), str(phones)]) as running:
        with pytest.raises(TargetError) as silent:
            running.call("hang")
    assert silent.value.errno == errno.ETIMEDOUT


@pytest.mark.parametrize(
    ("script", "words"),
    [
        ("import time; time.sleep(30)", "did not start properly"),
        ("print('{\"event\": \"ready\", \"version\": 99}', flush=True)", "not the version"),
    ],
)
def test_a_helper_that_does_not_start_properly_is_explained(monkeypatch, script, words) -> None:
    monkeypatch.setattr(mtp, "START_TIMEOUT", 0.8)
    with pytest.raises(MtpUnavailable) as caught:
        MtpHelper([sys.executable, "-c", script]).start()
    assert words in str(caught.value)


def test_a_missing_program_is_explained() -> None:
    with pytest.raises(MtpUnavailable, match="could not be started"):
        MtpHelper(["/no/such/mtk-mtp"]).start()


def test_without_a_helper_the_reason_is_plain(monkeypatch) -> None:
    monkeypatch.delenv("MTK_MTP_HELPER", raising=False)
    monkeypatch.setattr(mtp.sys, "platform", "linux")
    assert mtp.availability() == (False, "Phones and players without a drive letter (MTP) are only supported on Windows.")
    monkeypatch.setattr(mtp.sys, "platform", "win32")
    monkeypatch.setattr(mtp, "helper_command", lambda: None)
    ok, reason = mtp.availability()
    assert not ok and "missing from this installation" in reason


def test_listing_devices_never_raises_and_is_reused_for_a_moment(phones, monkeypatch) -> None:
    monkeypatch.setenv("MTK_MTP_HELPER", f"{sys.executable} {FAKE} {phones}")
    first = mtp.list_devices(fresh=True)
    assert first["available"] is True and [d["serial"] for d in first["devices"]] == [SERIAL]

    write_device(phones, "SER999", name="Walkman")
    assert [d["serial"] for d in mtp.list_devices()["devices"]] == [SERIAL], "reused within a few seconds"
    assert sorted(d["serial"] for d in mtp.list_devices(fresh=True)["devices"]) == [SERIAL, "SER999"]

    monkeypatch.setenv("MTK_MTP_HELPER", f"{sys.executable} -c pass")
    broken = mtp.list_devices(fresh=True)
    assert broken["available"] is False and broken["devices"] == [] and "did not start" in broken["reason"]


# ------------------------------------------------------------------------------------------- a device used like a folder


def test_a_target_is_found_by_serial_and_its_storage_checked(helper) -> None:
    found = MtpTarget.connect(helper, serial=SERIAL, storage="Internal storage", base="Music", label="Gym phone")
    assert found.label == "Gym phone" and found.storage_id == "s1" and isinstance(found, Target)

    with pytest.raises(TargetError, match="not connected"):
        MtpTarget.connect(helper, serial="OTHER", storage="s1", base="Music")
    with pytest.raises(TargetError, match="storage is not available"):
        MtpTarget.connect(helper, serial=SERIAL, storage="sd-card", base="Music")


def test_files_go_on_and_come_off_and_empty_folders_are_tidied(target, phones, tmp_path) -> None:
    source = song(tmp_path, size=300)

    assert target.put(source, "Artist/Album/01 - One.mp3") == 300
    assert target.size_of("Artist/Album/01 - One.mp3") == 300
    assert target.size_of("Artist/Album/nothing.mp3") is None and target.size_of("Artist/Album") is None, "a folder is not a file"
    assert target.list_names("Artist/Album") == ["01 - One.mp3"] and target.list_names("No/Such") == []
    assert (phones / SERIAL / "s1" / "Music" / "Artist" / "Album" / "01 - One.mp3").exists()
    assert target.free_space() == 10_000_000 - 300

    target.delete("Artist/Album/01 - One.mp3")

    assert target.size_of("Artist/Album/01 - One.mp3") is None
    assert not (phones / SERIAL / "s1" / "Music" / "Artist").exists(), "the emptied folders are tidied away"
    assert (phones / SERIAL / "s1" / "Music").exists(), "but never the music folder itself"


def test_a_cover_picture_does_not_keep_an_emptied_folder_alive_but_a_neighbouring_song_does(target, phones, tmp_path) -> None:
    root = phones / SERIAL / "s1" / "Music"
    target.put(song(tmp_path, "a.mp3"), "Artist/Album/01 - A.mp3")
    target.put(song(tmp_path, "b.mp3"), "Artist/Album/02 - B.mp3")
    target.put(song(tmp_path, "cover.jpg", 50), "Artist/Album/cover.jpg")

    target.delete("Artist/Album/01 - A.mp3")
    assert (root / "Artist" / "Album" / "02 - B.mp3").exists(), "another song in the folder is untouched"

    target.delete("Artist/Album/02 - B.mp3")
    assert not (root / "Artist").exists(), "with only the cover left, the folder goes with its last song"


def test_writing_over_a_file_replaces_it_and_text_can_be_written(target, tmp_path) -> None:
    target.put(song(tmp_path, size=300), "Artist/a.mp3")
    assert target.put(song(tmp_path, size=120), "Artist/a.mp3") == 120 and target.size_of("Artist/a.mp3") == 120

    target.write_text("Playlists/Road trip.m3u8", "#EXTM3U\n../Artist/a.mp3\n")
    assert target.size_of("Playlists/Road trip.m3u8") == len("#EXTM3U\n../Artist/a.mp3\n")


def test_a_full_device_refuses_a_file_and_nothing_half_written_stays(phones, helper, tmp_path) -> None:
    write_device(phones, "TINY", capacity=400, name="Tiny")
    tiny = MtpTarget.connect(helper, serial="TINY", storage="s1", base="Music")
    tiny.put(song(tmp_path, "a.mp3", 300), "a.mp3")

    with pytest.raises(TargetError) as full:
        tiny.put(song(tmp_path, "b.mp3", 300), "b.mp3")

    assert full.value.errno == errno.ENOSPC and tiny.size_of("b.mp3") is None and tiny.size_of("a.mp3") == 300


def test_a_copy_that_breaks_off_is_removed_again(target, phones, tmp_path) -> None:
    control(phones, fail_put={"match": "Song B", "code": "disconnected", "partial": True})

    with pytest.raises(TargetError) as broken:
        target.put(song(tmp_path, size=300), "Artist/Song B.mp3")

    assert broken.value.errno == errno.ENODEV
    assert not list((phones / SERIAL / "s1").rglob("Song B*")), "never half a song where a player will find it"


def test_an_unplugged_device_says_so(target, phones, tmp_path) -> None:
    assert target.is_connected() is True
    control(phones, unplugged=[SERIAL])

    assert target.is_connected() is False
    with pytest.raises(TargetError) as gone:
        target.size_of("a.mp3")
    assert gone.value.errno == errno.ENODEV
