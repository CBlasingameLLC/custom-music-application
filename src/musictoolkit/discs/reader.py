"""Reading the audio of a CD track carefully.

A drive cannot always be believed. A dirty or scratched disc can make it hand back wrong sectors without saying so, and it keeps what it
read last in a memory of its own, so asking again straight away may only bring the same wrong answer back. In `verified` mode the reader

  1. reads the track once, straight through, remembering a checksum of every block of 16 sectors;
  2. pushes the drive's memory out by reading somewhere else on the disc;
  3. reads the track a second time and compares;
  4. for every block that differs, reads it again - pushing the memory out each time - until two reads agree.

What it had to do is reported. A block that never settles is kept as its likeliest version and marked, and a block the drive cannot read
at all is filled with silence and marked: nothing is ever passed off as good. `fast` mode does the first step only.
"""

from __future__ import annotations

import base64
import zlib
from dataclasses import asdict, dataclass, field
from typing import BinaryIO, Callable, Protocol

from musictoolkit.discs.helper import SECTOR_BYTES, DiscError, DiscHelper

BLOCK_SECTORS = 16  # what is read, checked and re-read at a time (the most one request to the helper may ask for)
MAX_ATTEMPTS = 8  # reads of one block before it is given up on
FLUSH_SECTORS = 2048  # about 4.8 MB: more than the memory of any drive, read from somewhere else to push earlier data out of it
MAX_UNREADABLE = 100  # blocks one track may lose before the disc is judged too damaged to go on with
READ_TIMEOUT = 90.0  # seconds: a drive struggling with a bad sector can take a long time over one request
ACCURACY = ("verified", "fast")


class SectorSource(Protocol):
    def read(self, lba: int, count: int) -> bytes: ...

    def flush(self, lba: int, count: int) -> None: ...


class HelperSource:
    """The sectors of one drive, through a running helper."""

    def __init__(self, helper: DiscHelper, drive: str) -> None:
        self.helper = helper
        self.drive = drive

    def read(self, lba: int, count: int) -> bytes:
        data = base64.b64decode(self.helper.call("read", drive=self.drive, lba=lba, count=count, timeout=READ_TIMEOUT)["data"])
        if len(data) != count * SECTOR_BYTES:
            raise DiscError("read_error", "The drive gave back less than it was asked for.")
        return data

    def flush(self, lba: int, count: int) -> None:
        self.helper.call("flush", drive=self.drive, lba=lba, count=count, timeout=READ_TIMEOUT)


@dataclass
class ReadStats:
    sectors: int = 0
    rereads: int = 0  # blocks whose two reads differed, so that they were read again
    suspect: list[int] = field(default_factory=list)  # first sector of each block whose reads never agreed: the likeliest version was kept
    unreadable: list[int] = field(default_factory=list)  # first sector of each block the drive could not read: silence was put in its place

    @property
    def problems(self) -> int:
        return len(self.suspect) + len(self.unreadable)

    def to_dict(self) -> dict:
        return {**asdict(self), "problems": self.problems}


def _flush(source: SectorSource, near: int, disc_sectors: int) -> None:
    """Read somewhere far from `near` (the other end of the disc), so that what the drive remembers of `near` is pushed out. Only that the
    drive went there matters: sectors there that cannot be read do not make the sectors that are wanted any worse."""
    count = min(FLUSH_SECTORS, max(BLOCK_SECTORS, disc_sectors // 4))
    try:
        source.flush(disc_sectors - count if near < disc_sectors // 2 else 0, count)
    except DiscError as error:
        if error.code not in ("read_error", "unsupported"):
            raise


def _read_block(source: SectorSource, lba: int, count: int, disc_sectors: int) -> bytes | None:
    """One block, trying again (after pushing the drive's memory out) when the drive says it cannot read it; None if it never can.
    Anything but a read error - no disc, a changed disc, no answer - is not a bad block: it stops the whole rip."""
    for attempt in range(MAX_ATTEMPTS):
        try:
            return source.read(lba, count)
        except DiscError as error:
            if error.code != "read_error":
                raise
            if attempt + 1 < MAX_ATTEMPTS:
                _flush(source, lba, disc_sectors)
    return None


def _settle(
    source: SectorSource, sink: BinaryIO, lba: int, count: int, offset: int, disc_sectors: int,
    reads: dict[int, list], stats: ReadStats,
) -> None:
    """The two reads of this block differ. Read it again until some version has been seen twice, and keep that one."""
    stats.rereads += 1
    for _ in range(MAX_ATTEMPTS):
        winner = next((crc for crc, (_, votes) in reads.items() if votes >= 2), None)
        if winner is not None:
            break
        _flush(source, lba, disc_sectors)
        try:
            data = source.read(lba, count)
        except DiscError as error:
            if error.code != "read_error":
                raise
            continue
        crc = zlib.crc32(data)
        reads.setdefault(crc, [data, 0])[1] += 1
    else:
        winner = next((crc for crc, (_, votes) in reads.items() if votes >= 2), None)
    if winner is None:  # nothing was ever seen twice: the first version stays, and the block is marked
        stats.suspect.append(lba)
        return
    sink.seek(offset)
    sink.write(reads[winner][0])


def read_track(
    source: SectorSource, first: int, count: int, sink: BinaryIO, *, disc_sectors: int, accuracy: str = "verified",
    on_progress: Callable[[int, int], None] | None = None, check: Callable[[], None] | None = None,
) -> ReadStats:
    """Read `count` sectors from sector `first` of the disc into `sink` (a file opened "w+b": it is written straight through and patched).
    `on_progress(done, total)` counts sectors over all the passes; `check()` is called before each block and may raise to stop (a cancel).
    Raises DiscError for anything that is not a bad block (no disc, a changed disc, a drive that stopped answering)."""
    if accuracy not in ACCURACY:
        raise ValueError(f"accuracy must be one of: {', '.join(ACCURACY)}")
    stats = ReadStats(sectors=count)
    blocks = [(first + start, min(BLOCK_SECTORS, count - start)) for start in range(0, count, BLOCK_SECTORS)]
    total = count * (2 if accuracy == "verified" else 1)
    done = 0
    crcs: list[int | None] = []

    def tell() -> None:
        if on_progress:
            on_progress(done, total)

    for lba, n in blocks:  # pass 1: straight through
        if check:
            check()
        data = _read_block(source, lba, n, disc_sectors)
        if data is None:
            stats.unreadable.append(lba)
            if len(stats.unreadable) > MAX_UNREADABLE:
                raise DiscError("read_error", "This part of the disc cannot be read: it is too scratched or dirty. Clean it and try again.")
            sink.write(bytes(n * SECTOR_BYTES))
            crcs.append(None)
        else:
            sink.write(data)
            crcs.append(zlib.crc32(data))
        done += n
        tell()

    if accuracy == "verified":
        _flush(source, first, disc_sectors)
        for (lba, n), crc in zip(blocks, crcs):  # pass 2: again, and compare
            if check:
                check()
            if crc is not None:
                offset = (lba - first) * SECTOR_BYTES
                again: bytes | None
                try:
                    again = source.read(lba, n)
                except DiscError as error:
                    if error.code != "read_error":
                        raise
                    again = None
                if again is None or zlib.crc32(again) != crc:
                    sink.seek(offset)
                    reads: dict[int, list] = {crc: [sink.read(n * SECTOR_BYTES), 1]}
                    if again is not None:
                        reads.setdefault(zlib.crc32(again), [again, 0])[1] += 1
                    _settle(source, sink, lba, n, offset, disc_sectors, reads, stats)
            done += n
            tell()
    sink.flush()
    return stats
