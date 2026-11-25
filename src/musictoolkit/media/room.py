"""Telling that a write failed because the drive ran out of room.

A plain file write says so with its error number (28, ENOSPC). The audio library (libsndfile, through `soundfile`) does not. Found on a drive that was
really full: of a write that the system refused it says only `System error.` (WAV, FLAC, MP3, Ogg and Opus alike; no error number, and never the word
"space"), and a FLAC file that cannot get even its header onto a drive with next to no room says `Error : problem with initialization of the flac
decoder.`. The words are no use for telling: when libsndfile cannot open a file, its message holds the file's own name, and a folder called "Outer
Space" is not a full drive. So for those two messages the drive is looked at: how much is left where the file was being written.

That has to be done before what was half written is taken away, because taking it away gives its room back (on a drive of 1 MiB the half-written file
*is* the drive, and with it gone the drive looks as if it had room).
"""

from __future__ import annotations

import errno
import shutil
from pathlib import Path

LOW_ROOM = 1 << 20  # a drive with less than this left (1 MiB) that has just refused a write has run out of room
REFUSED_WRITE = ("system error", "problem with initialization of the flac")  # what the audio library says (in lower case) of a write that the system refused


def no_room(exc: BaseException, folder: Path | None = None) -> bool:
    """Whether a failed write was the drive running full: the error number is 28 (ENOSPC), or the C library's own words say "No space left", or the
    audio library refused a write (`REFUSED_WRITE`) and `folder`, the one that was being written to, has less than `LOW_ROOM` left. Other words are no
    use for this, and with no folder there is no drive to look at. A drive that cannot be looked at is not taken for a full one."""
    words = str(exc).lower()
    if getattr(exc, "errno", None) == errno.ENOSPC or "no space left" in words:
        return True
    if folder is not None and any(refused in words for refused in REFUSED_WRITE):
        try:
            return shutil.disk_usage(folder).free < LOW_ROOM
        except OSError:
            return False
    return False
