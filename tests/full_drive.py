"""A stand-in for a drive that fills up in the middle of a song, for the tests of what a rip does then.

The audio library says very little of a write that the system refused (found on a drive that was really full: `System error.`, with no error number and
no word about room), so a test that wants to know whether the app notices has to make that happen: the write of one chosen file is refused as the library
refuses it, and the drive says how much is left the way a real one does, with what was half written still on it.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import soundfile as sf

PLENTY = 50 * 1024**3  # bytes


def drive_fills_up(monkeypatch: pytest.MonkeyPatch, song: str, *, full: bool = True) -> None:
    """From now on a write into a file whose name ends with `song` (say "02 - Two.flac.part") is refused, as libsndfile refuses one on a full drive.
    With `full` the drive then has no room while a half-written (`.part`) file is on it and plenty once it has been taken away, which is what a drive
    that a song has just filled does: whoever looks at the room after tidying up sees the room that tidying gave back. Without `full` the drive has
    plenty all the time: the write was refused for some other reason."""
    real_write = sf.SoundFile.write

    def refusing(self: sf.SoundFile, data: object) -> None:
        if str(self.name).endswith(song):
            raise sf.LibsndfileError(2)
        return real_write(self, data)

    def room(folder: str | Path) -> SimpleNamespace:
        free = 0 if full and any(Path(folder).glob("*.part")) else PLENTY
        return SimpleNamespace(total=10**12, used=10**12 - free, free=free)

    monkeypatch.setattr(sf.SoundFile, "write", refusing)
    monkeypatch.setattr(shutil, "disk_usage", room)
