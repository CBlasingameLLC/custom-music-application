"""The client for the disc helper, tried against the stand-in drive in tests/fake_disc_helper.py (which keeps its drives and discs in folders)."""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from musictoolkit.discs import helper as disc_helper
from musictoolkit.discs.helper import DiscError, DiscHelper, DiscUnavailable, valid_drive
from tests.fake_disc_helper import add_drive, audio, audio_disc, set_control

FAKE = Path(__file__).parent / "fake_disc_helper.py"


@pytest.fixture
def world(tmp_path: Path) -> Path:
    root = tmp_path / "drives"
    add_drive(root, "E", disc=audio_disc(seed=3, tracks=(20, 30, 25)))
    add_drive(root, "F")
    return root


def start(root: Path) -> DiscHelper:
    return DiscHelper([sys.executable, str(FAKE), str(root)])


@pytest.fixture(autouse=True)
def fresh_listing():
    disc_helper.forget_listing()
    yield
    disc_helper.forget_listing()


def read(helper: DiscHelper, lba: int, count: int, drive: str = "E:") -> bytes:
    return base64.b64decode(helper.call("read", drive=drive, lba=lba, count=count)["data"])


# ---------------------------------------------------------------------------------------- the commands


def test_the_helper_lists_its_drives_and_what_is_in_them(world: Path) -> None:
    with start(world) as helper:
        drives = helper.call("drives")["drives"]
    assert [(d["id"], d["media"]) for d in drives] == [("E:", "audio"), ("F:", "none")]
    assert drives[0]["label"] == "AUDIO CD" and drives[0]["name"]


def test_the_table_of_contents_gives_every_track_and_where_the_lead_out_begins(world: Path) -> None:
    with start(world) as helper:
        toc = helper.call("toc", drive="E:")
    assert (toc["first"], toc["last"], toc["leadout"]) == (1, 3, 75)
    assert [(t["number"], t["audio"], t["start"]) for t in toc["tracks"]] == [(1, True, 0), (2, True, 20), (3, True, 50)]


def test_a_read_returns_exactly_the_sectors_asked_for(world: Path) -> None:
    with start(world) as helper:
        assert read(helper, 18, 4) == audio(3, 18, 4)
        assert read(helper, 0, 1) == audio(3, 0, 1)
        assert helper.call("read", drive="E:", lba=18, count=4)["sectors"] == 4


def test_a_drive_without_a_disc_says_so(world: Path) -> None:
    with start(world) as helper, pytest.raises(DiscError) as caught:
        helper.call("toc", drive="F:")
    assert caught.value.code == "no_media" and "no disc" in str(caught.value)


def test_a_drive_that_is_not_there_is_refused_as_not_found(world: Path) -> None:
    with start(world) as helper, pytest.raises(DiscError) as caught:
        helper.call("read", drive="Q:", lba=0, count=1)
    assert caught.value.code == "not_found"


def test_reading_past_the_end_of_the_disc_is_a_read_error(world: Path) -> None:
    with start(world) as helper, pytest.raises(DiscError) as caught:
        helper.call("read", drive="E:", lba=70, count=10)
    assert caught.value.code == "read_error"


def test_the_data_track_of_a_cd_extra_cannot_be_read_as_audio(tmp_path: Path) -> None:
    root = tmp_path / "drives"
    add_drive(root, "G", disc=audio_disc(tracks=(10, 10), data_sectors=5))
    with start(root) as helper:
        contents = helper.call("toc", drive="G:")
        assert contents["tracks"][-1]["audio"] is False and contents["tracks"][-1]["start"] == 20 + 11400, "the data session starts 11400 sectors on"
        assert len(read(helper, 5, 2, "G:")) == 2 * 2352
        with pytest.raises(DiscError) as caught:
            helper.call("read", drive="G:", lba=contents["tracks"][-1]["start"] + 1, count=1)
        assert caught.value.code == "unsupported"
        with pytest.raises(DiscError) as between:
            helper.call("read", drive="G:", lba=21, count=1)
        assert between.value.code == "read_error", "between the sessions there is nothing to read"


def test_ejecting_takes_the_disc_out(world: Path) -> None:
    with start(world) as helper:
        helper.call("eject", drive="E:")
        assert [d["media"] for d in helper.call("drives")["drives"]] == ["none", "none"]
        with pytest.raises(DiscError) as caught:
            helper.call("toc", drive="E:")
    assert caught.value.code == "no_media"
    assert (world / "E" / "eject.log").read_text() == "ejected\n"


def test_a_flush_reads_and_throws_away(world: Path) -> None:
    with start(world) as helper:
        assert helper.call("flush", drive="E:", lba=0, count=40) == {"id": 1, "ok": True}
        with pytest.raises(DiscError) as caught:
            helper.call("flush", drive="E:", lba=0, count=5000)
    assert caught.value.code == "failed"


# ---------------------------------------------------------------------------------------- the protocol around them


def test_stray_output_between_replies_is_not_an_answer(world: Path) -> None:
    set_control(world, noise=True)
    with start(world) as helper:
        assert read(helper, 3, 2) == audio(3, 3, 2)
        assert helper.call("toc", drive="E:")["last"] == 3


def test_a_helper_that_goes_quiet_is_given_up_on_and_stopped(world: Path) -> None:
    helper = start(world)
    helper.start()
    started = time.monotonic()
    with pytest.raises(DiscError) as caught:
        helper.call("hang", timeout=0.4)
    assert caught.value.code == "timeout" and time.monotonic() - started < 5
    with pytest.raises(DiscError) as again:
        helper.call("drives")
    assert again.value.code == "stopped"
    helper.close()


def test_a_helper_that_dies_is_reported_as_stopped(world: Path) -> None:
    with start(world) as helper, pytest.raises(DiscError) as caught:
        helper.call("crash")
    assert caught.value.code == "stopped"


def test_progress_events_reach_the_caller_before_the_reply(world: Path) -> None:
    seen: list[tuple[int, str]] = []
    with start(world) as helper:
        helper.call("ticks", n=4, on_event=lambda e: seen.append((e["done"], e["message"])))
    assert seen == [(1, "step 1"), (2, "step 2"), (3, "step 3"), (4, "step 4")]


def test_a_command_that_is_running_can_be_cancelled_from_another_thread(world: Path) -> None:
    set_control(world, slow=0.2)  # a 16-sector read would take 3.2 seconds
    outcome: list[object] = []
    with start(world) as helper:
        worker = threading.Thread(target=lambda: outcome.append(_attempt(helper)))
        started = time.monotonic()
        worker.start()
        time.sleep(0.4)
        helper.cancel()
        worker.join(timeout=10)
        assert time.monotonic() - started < 3, "the read should have been cut short"
        assert isinstance(outcome[0], DiscError) and outcome[0].code == "cancelled"
        set_control(world)  # the helper is still usable afterwards
        assert read(helper, 0, 2) == audio(3, 0, 2)


def _attempt(helper: DiscHelper) -> object:
    try:
        return helper.call("read", drive="E:", lba=0, count=16)
    except DiscError as error:
        return error


def test_cancelling_when_nothing_is_running_does_nothing(world: Path) -> None:
    with start(world) as helper:
        helper.cancel()
        assert read(helper, 0, 1) == audio(3, 0, 1)


def test_a_helper_of_another_version_is_refused() -> None:
    old = DiscHelper([sys.executable, "-c", "import time; print('{\"event\":\"ready\",\"version\":99}', flush=True); time.sleep(5)"])
    with pytest.raises(DiscUnavailable, match="not the version"):
        old.start()


def test_a_helper_that_cannot_be_started_says_so() -> None:
    with pytest.raises(DiscUnavailable, match="could not be started"):
        DiscHelper(["/no/such/mtk-disc"]).start()


# ---------------------------------------------------------------------------------------- finding the helper, naming a drive


@pytest.mark.parametrize("given, wanted", [("E:", "E:"), ("e:", "E:"), (" d: ", "D:")])
def test_a_drive_is_named_by_its_letter(given: str, wanted: str) -> None:
    assert valid_drive(given) == wanted


@pytest.mark.parametrize("given", ["", None, "E", "E:\\", "EE:", "C:\\Windows", "\\\\.\\PhysicalDrive0", "../x", "1:", "E:/"])
def test_anything_else_is_refused_before_it_can_reach_a_device(given: object) -> None:
    with pytest.raises(DiscError) as caught:
        valid_drive(given)
    assert caught.value.code == "not_found"


def test_the_helper_is_found_through_the_override_in_either_form(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MTK_DISC_HELPER", json.dumps(["C:\\Program Files\\Python\\python.exe", "x.py"]))
    assert disc_helper.helper_command() == ["C:\\Program Files\\Python\\python.exe", "x.py"]
    monkeypatch.setenv("MTK_DISC_HELPER", "tool --flag")
    assert disc_helper.helper_command() == ["tool", "--flag"]
    assert disc_helper.availability() == (True, None)


def test_without_a_helper_the_reason_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MTK_DISC_HELPER", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    ok, reason = disc_helper.availability()
    assert not ok and "only supported on Windows" in reason
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(disc_helper, "helper_command", lambda: None)
    ok, reason = disc_helper.availability()
    assert not ok and "missing from this installation" in reason


def test_the_listing_never_raises_and_says_what_went_wrong(monkeypatch: pytest.MonkeyPatch, world: Path) -> None:
    monkeypatch.setenv("MTK_DISC_HELPER", json.dumps([sys.executable, str(FAKE), str(world)]))
    listing = disc_helper.list_drives()
    assert listing["available"] and listing["error"] is None and [d["id"] for d in listing["drives"]] == ["E:", "F:"]

    disc_helper.forget_listing()
    monkeypatch.setenv("MTK_DISC_HELPER", json.dumps([sys.executable, "-c", "pass"]))  # starts, says nothing
    monkeypatch.setattr(disc_helper, "START_TIMEOUT", 0.5)
    broken = disc_helper.list_drives()
    assert broken["available"] and broken["drives"] == [] and broken["error"]

    disc_helper.forget_listing()
    monkeypatch.delenv("MTK_DISC_HELPER")
    monkeypatch.setattr(disc_helper, "helper_command", lambda: None)
    monkeypatch.setattr(sys, "platform", "linux")
    absent = disc_helper.list_drives()
    assert absent["available"] is False and "Windows" in absent["reason"] and absent["drives"] == []


def test_the_listing_is_reused_for_a_moment_and_while_a_rip_has_the_drive_open(monkeypatch: pytest.MonkeyPatch, world: Path) -> None:
    monkeypatch.setenv("MTK_DISC_HELPER", json.dumps([sys.executable, str(FAKE), str(world)]))
    assert [d["media"] for d in disc_helper.list_drives()["drives"]] == ["audio", "none"]
    add_drive(world, "F", disc=audio_disc())  # a disc goes into the second drive
    assert [d["media"] for d in disc_helper.list_drives()["drives"]] == ["audio", "none"], "asked again within the moment: the same answer"
    assert [d["media"] for d in disc_helper.list_drives(fresh=True)["drives"]] == ["audio", "audio"]

    add_drive(world, "F")
    (world / "F" / "disc.json").unlink(missing_ok=True)
    with disc_helper.in_use():  # a rip has the drive: nobody starts a second helper, not even when asked for a fresh look
        assert [d["media"] for d in disc_helper.list_drives(fresh=True)["drives"]] == ["audio", "audio"]
    assert [d["media"] for d in disc_helper.list_drives(fresh=True)["drives"]] == ["audio", "none"]


# ---------------------------------------------------------------------------------------- the stand-in's own faults (later tests rely on them)


def test_the_stand_in_makes_a_sector_read_wrongly_the_first_time_only_when_asked_to(world: Path) -> None:
    set_control(world, flaky=[{"from": 5, "to": 6, "times": 1}])
    with start(world) as helper:
        first, second = read(helper, 4, 4), read(helper, 4, 4)
    good = audio(3, 4, 4)
    assert second == good
    assert first != good and sum(a != b for a, b in zip(first, good)) == 2, "one bit in each of the two flaky sectors"


def test_the_stand_in_can_fail_a_read_every_time(world: Path) -> None:
    set_control(world, bad=[{"from": 10, "to": 11}])
    with start(world) as helper:
        for _ in range(2):
            with pytest.raises(DiscError) as caught:
                helper.call("read", drive="E:", lba=8, count=4)
            assert caught.value.code == "read_error"
        assert read(helper, 12, 4) == audio(3, 12, 4)


def test_a_drive_cache_repeats_a_wrong_answer_until_something_else_has_been_read(world: Path) -> None:
    """This is the trap the reader has to avoid: asking again straight away gets the drive's memory, wrong data included."""
    set_control(world, flaky=[{"from": 5, "to": 5, "times": 1}], cache=16)
    good = audio(3, 4, 4)
    with start(world) as helper:
        first = read(helper, 4, 4)
        assert first != good
        assert read(helper, 4, 4) == first, "the drive hands back what it remembered"
        helper.call("flush", drive="E:", lba=30, count=16)  # reading 16 other sectors pushes it out of the cache
        assert read(helper, 4, 4) == good


def test_the_stand_in_can_report_a_changed_disc_after_a_number_of_reads(world: Path) -> None:
    set_control(world, change_after=2)
    with start(world) as helper:
        read(helper, 0, 1)
        read(helper, 1, 1)
        with pytest.raises(DiscError) as caught:
            read(helper, 2, 1)
    assert caught.value.code == "media_changed"
