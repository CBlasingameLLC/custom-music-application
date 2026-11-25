"""Telling that a write failed because the drive ran out of room: by the error number, by the C library's own words, and, for the audio library's refusals
(which say nothing of room), by looking at the drive."""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from musictoolkit.media.room import LOW_ROOM, REFUSED_WRITE, no_room

SYSTEM_ERROR = sf.LibsndfileError(2)  # all that libsndfile says of a write that the system refused (found on a drive that was really full)
FLAC_HEADER = RuntimeError("Error : problem with initialization of the flac decoder.")  # what a FLAC file says when not even its header fits
NOT_OPENED = sf.LibsndfileError(2, prefix="Error opening '/music/Outer Space/01.flac.part': ")  # (the name of the file is in what is said of one that cannot be opened)


def room_left(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda folder: SimpleNamespace(total=10**12, used=10**12 - free, free=free))


@pytest.mark.parametrize("exc,full", [
    (OSError(errno.ENOSPC, "No space left on device"), True),
    (OSError(errno.ENOSPC, "Il n'y a plus de place sur le disque"), True),  # (the number, when the words are another language's)
    (RuntimeError("System error : No space left on device."), True),  # (the words, when only they come through)
    (RuntimeError("Error opening '/music/Outer Space/01.flac': Format not recognised."), False),  # (a folder called Space is not a full drive)
    (OSError(errno.ENOENT, "No such file or directory", "/music/Outer Space/01.flac"), False),
    (OSError(errno.EIO, "Input/output error"), False),
    (SYSTEM_ERROR, False),  # (the audio library's refusals are told by looking at the drive: with no folder there is no drive to look at)
    (FLAC_HEADER, False),
])
def test_what_counts_as_the_drive_running_full(exc: BaseException, full: bool) -> None:
    assert no_room(exc) is full


@pytest.mark.parametrize("refused", [SYSTEM_ERROR, FLAC_HEADER, NOT_OPENED], ids=["system error", "flac header", "not opened"])
@pytest.mark.parametrize("free,full", [
    (0, True), (300 * 1024, True), (900 * 1024, True), (LOW_ROOM - 1, True),  # (less than 1 MiB left)
    (LOW_ROOM, False), (1100 * 1024, False), (100 * 1024**2, False), (50 * 1024**3, False),
])
def test_the_audio_librarys_refusal_of_a_write_is_a_full_drive_when_the_drive_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refused: BaseException, free: int, full: bool
) -> None:
    room_left(monkeypatch, free)
    assert no_room(refused, tmp_path) is full
    assert no_room(RuntimeError("Something else went wrong."), tmp_path) is False  # (only those messages are taken for it, whatever is left on the drive)
    assert no_room(RuntimeError("Error opening '/music/x.flac': Format not recognised."), tmp_path) is False


def test_a_folder_called_outer_space_is_not_a_full_drive_but_a_full_drive_is_full_whatever_its_folders_are_called(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for words in ("Outer Space", "Space Left", "Spaced Out"):
        broken = sf.LibsndfileError(2, prefix=f"Error opening '/music/{words}/01.flac.part': ")
        room_left(monkeypatch, 50 * 1024**3)
        assert no_room(broken, tmp_path) is False, words  # (the folder is looked at, not the name: this drive has all the room in the world)
        assert no_room(OSError(errno.ENOENT, "No such file or directory", f"/music/{words}/01.flac"), tmp_path) is False, words
        room_left(monkeypatch, 0)
        assert no_room(broken, tmp_path) is True, words  # (and on a drive with none, the same message is the drive)


@pytest.mark.parametrize("refused", [SYSTEM_ERROR, FLAC_HEADER], ids=["system error", "flac header"])
def test_a_drive_that_cannot_be_looked_at_is_not_taken_for_a_full_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refused: BaseException) -> None:
    def gone(folder: Path) -> None:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr(shutil, "disk_usage", gone)
    assert no_room(refused, tmp_path) is False


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="Linux's /dev/full refuses every write for want of room, which stands for a full drive")
@pytest.mark.parametrize("name,options", [
    ("wav", dict(format="WAV", subtype="PCM_16")),
    ("flac", dict(format="FLAC", subtype="PCM_16")),
    ("mp3", dict(format="MP3", subtype="MPEG_LAYER_III", bitrate_mode="CONSTANT", compression_level=0.05)),
    ("ogg", dict(format="OGG", subtype="VORBIS")),
    ("opus", dict(format="OGG", subtype="OPUS")),
])
def test_the_words_the_audio_library_really_uses_for_a_refused_write_are_the_ones_that_are_looked_for(
    monkeypatch: pytest.MonkeyPatch, name: str, options: dict[str, str]
) -> None:
    """What `no_room` looks for was found on a drive that was really full; this asks the real library again (with /dev/full standing in for the drive), so
    that a newer libsndfile that words it differently is noticed here and not by a person whose rip said "System error."."""
    block = np.zeros((44100, 2), dtype=np.float32) + 0.1
    refusal: BaseException | None = None
    try:
        with sf.SoundFile("/dev/full", "w", 48000 if name == "opus" else 44100, 2, **options) as out:
            for _ in range(8):
                out.write(block)
    except (OSError, RuntimeError) as exc:
        refusal = exc
    assert refusal is not None, f"{name}: the audio library took 8 seconds of sound onto /dev/full"
    assert any(words in str(refusal).lower() for words in REFUSED_WRITE), f"{name}: it said {str(refusal)!r}"
    assert no_room(refusal, Path("/dev")) is False  # (/dev has room: only the drive's own state decides, not the words)
    room_left(monkeypatch, 0)
    assert no_room(refusal, Path("/dev")) is True
