"""Reading a track carefully, against the stand-in drive (whose faults - flaky sectors, bad sectors, a cache that repeats itself - are in
tests/fake_disc_helper.py)."""

from __future__ import annotations

import io
import sys
import zlib
from pathlib import Path

import pytest

from musictoolkit.discs import reader
from musictoolkit.discs.helper import DiscError, DiscHelper
from musictoolkit.discs.reader import BLOCK_SECTORS, HelperSource, read_track
from tests.fake_disc_helper import add_drive, audio, audio_disc, set_control

FAKE = Path(__file__).parent / "fake_disc_helper.py"
SEED = 5
TRACKS = (48, 64, 40)
DISC_SECTORS = sum(TRACKS)


class Counting(HelperSource):
    """The drive as the reader sees it, counting what it is asked."""

    def __init__(self, helper: DiscHelper, drive: str) -> None:
        super().__init__(helper, drive)
        self.reads: list[tuple[int, int]] = []
        self.flushes: list[tuple[int, int]] = []

    def read(self, lba: int, count: int) -> bytes:
        self.reads.append((lba, count))
        return super().read(lba, count)

    def flush(self, lba: int, count: int) -> None:
        self.flushes.append((lba, count))
        super().flush(lba, count)


@pytest.fixture
def drive(tmp_path: Path):
    root = tmp_path / "drives"
    add_drive(root, "E", disc=audio_disc(seed=SEED, tracks=TRACKS))
    helper = DiscHelper([sys.executable, str(FAKE), str(root)])
    helper.start()
    yield root, Counting(helper, "E:")
    helper.close()


def rip(source, first: int = 0, count: int = TRACKS[0], **kw):
    sink = io.BytesIO()
    stats = read_track(source, first, count, sink, disc_sectors=DISC_SECTORS, **kw)
    return sink.getvalue(), stats


# ---------------------------------------------------------------------------------------- a good disc


def test_a_good_track_comes_out_exactly_as_it_is_on_the_disc(drive) -> None:
    _, source = drive
    data, stats = rip(source)
    assert data == audio(SEED, 0, TRACKS[0])
    assert (stats.rereads, stats.suspect, stats.unreadable, stats.problems, stats.sectors) == (0, [], [], 0, TRACKS[0])


def test_a_track_in_the_middle_of_the_disc_and_one_that_ends_in_a_short_block(drive) -> None:
    _, source = drive
    data, _ = rip(source, first=TRACKS[0], count=TRACKS[1])
    assert data == audio(SEED, TRACKS[0], TRACKS[1])
    data, _ = rip(source, first=TRACKS[0] + TRACKS[1], count=TRACKS[2])  # 40 sectors: 16 + 16 + 8
    assert data == audio(SEED, TRACKS[0] + TRACKS[1], TRACKS[2])


def test_the_default_reads_everything_twice_and_pushes_the_drives_memory_out_between(drive) -> None:
    _, source = drive
    rip(source)
    blocks = -(-TRACKS[0] // BLOCK_SECTORS)
    assert len(source.reads) == 2 * blocks
    assert [lba for lba, _ in source.reads[:blocks]] == [lba for lba, _ in source.reads[blocks:]]
    assert len(source.flushes) == 1, "once, between the two passes"


def test_fast_reads_once_and_never_flushes(drive) -> None:
    _, source = drive
    data, _ = rip(source, accuracy="fast")
    assert data == audio(SEED, 0, TRACKS[0]) and len(source.reads) == -(-TRACKS[0] // BLOCK_SECTORS) and source.flushes == []


def test_progress_counts_sectors_over_both_passes(drive) -> None:
    _, source = drive
    seen: list[tuple[int, int]] = []
    rip(source, on_progress=lambda done, total: seen.append((done, total)))
    assert seen[0][0] == BLOCK_SECTORS and seen[-1] == (2 * TRACKS[0], 2 * TRACKS[0])
    assert all(a[0] < b[0] for a, b in zip(seen, seen[1:])) and {t for _, t in seen} == {2 * TRACKS[0]}
    seen.clear()
    rip(source, accuracy="fast", on_progress=lambda done, total: seen.append((done, total)))
    assert seen[-1] == (TRACKS[0], TRACKS[0])


# ---------------------------------------------------------------------------------------- a drive that cannot be trusted


def test_a_sector_that_came_wrong_in_the_first_pass_is_found_and_put_right(drive) -> None:
    root, source = drive
    set_control(root, flaky=[{"from": 20, "to": 20, "times": 1}])
    data, stats = rip(source)
    assert data == audio(SEED, 0, TRACKS[0])
    assert stats.rereads == 1 and stats.problems == 0


def test_a_sector_that_came_wrong_in_the_second_pass_is_found_and_the_first_version_kept(drive) -> None:
    root, source = drive
    set_control(root, flaky=[{"from": 20, "to": 20, "times": 1, "after": 1}])  # the first read is right, the second is not, the third is right
    data, stats = rip(source)
    assert data == audio(SEED, 0, TRACKS[0]) and stats.rereads == 1 and stats.problems == 0


def test_the_drives_memory_does_not_fool_the_second_pass(drive) -> None:
    """The drive remembers the last 32 sectors it read. Without pushing them out, the second pass would be handed the same wrong sector."""
    root, source = drive
    set_control(root, cache=32, flaky=[{"from": 40, "to": 40, "times": 1}])
    data, stats = rip(source)
    assert data == audio(SEED, 0, TRACKS[0]), "the wrong sector was in the drive's memory when the second pass came"
    assert stats.rereads == 1


def test_a_block_that_keeps_coming_out_differently_is_marked_not_hidden(drive) -> None:
    root, source = drive
    set_control(root, flaky=[{"from": 20, "to": 20, "times": 100, "varies": True}])
    data, stats = rip(source)
    assert stats.suspect == [16] and stats.unreadable == [] and stats.problems == 1 and stats.rereads == 1
    assert len(data) == TRACKS[0] * 2352
    assert data[:16 * 2352] == audio(SEED, 0, 16) and data[32 * 2352:] == audio(SEED, 32, TRACKS[0] - 32), "only the bad block is in doubt"


def test_a_block_that_cannot_be_read_at_all_is_silence_and_marked(drive) -> None:
    root, source = drive
    set_control(root, bad=[{"from": 20, "to": 20}])
    data, stats = rip(source)
    assert stats.unreadable == [16] and stats.suspect == [] and stats.problems == 1
    assert data[16 * 2352:32 * 2352] == bytes(16 * 2352)
    assert data[:16 * 2352] == audio(SEED, 0, 16) and data[32 * 2352:] == audio(SEED, 32, TRACKS[0] - 32)
    assert len([r for r in source.reads if r[0] == 16]) == reader.MAX_ATTEMPTS, "it was tried several times, with the memory pushed out between"


def test_a_disc_too_scratched_to_go_on_with_is_given_up_on(drive, monkeypatch: pytest.MonkeyPatch) -> None:
    root, source = drive
    set_control(root, bad=[{"from": 0, "to": 47}])
    monkeypatch.setattr(reader, "MAX_UNREADABLE", 1)
    monkeypatch.setattr(reader, "MAX_ATTEMPTS", 2)
    with pytest.raises(DiscError, match="too scratched") as caught:
        rip(source)
    assert caught.value.code == "read_error"


# ---------------------------------------------------------------------------------------- things that are not a bad block


def test_a_disc_that_is_taken_out_stops_the_rip(drive) -> None:
    root, source = drive
    set_control(root, change_after=2)
    with pytest.raises(DiscError) as caught:
        rip(source)
    assert caught.value.code == "media_changed"


def test_a_cancel_is_noticed_before_the_next_block(drive) -> None:
    _, source = drive

    class Stop(Exception):
        pass

    calls = []

    def check() -> None:
        calls.append(1)
        if len(calls) == 3:
            raise Stop

    with pytest.raises(Stop):
        rip(source, check=check)
    assert len(source.reads) == 2, "two blocks were read; the third was never asked for"


def test_an_unknown_accuracy_is_refused(drive) -> None:
    _, source = drive
    with pytest.raises(ValueError, match="accuracy"):
        rip(source, accuracy="paranoid")


def test_a_short_read_from_the_helper_is_a_read_error(drive, monkeypatch: pytest.MonkeyPatch) -> None:
    _, source = drive
    real = source.helper.call

    def short(cmd, **kw):
        reply = real(cmd, **kw)
        if cmd == "read":
            import base64

            reply["data"] = base64.b64encode(base64.b64decode(reply["data"])[:-4]).decode()
        return reply

    monkeypatch.setattr(source.helper, "call", short)
    with pytest.raises(DiscError, match="less than it was asked"):
        HelperSource.read(source, 0, 2)


def test_what_was_found_is_reported_in_plain_numbers() -> None:
    stats = reader.ReadStats(sectors=100, rereads=2, suspect=[16], unreadable=[32, 48])
    assert stats.to_dict() == {"sectors": 100, "rereads": 2, "suspect": [16], "unreadable": [32, 48], "problems": 3}
    assert zlib.crc32(b"") == 0  # (the reader compares reads by their CRC-32)
