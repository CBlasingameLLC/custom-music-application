"""A disc's table of contents, and the identifiers made from it.

The helper reports sectors the way Windows numbers them: sector 0 is the start of the first track. MusicBrainz counts from the start of the
disc, two seconds earlier, so its numbers are ours plus 150. Pure functions; nothing here touches a drive or the network.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

PREGAP = 150  # sectors (two seconds) from the start of the disc to sector 0, MusicBrainz's offset for the first track
SECTORS_PER_SECOND = 75
CD_EXTRA_GAP = 11400  # sectors between the end of the audio on an Enhanced CD (CD-Extra) and the data track in the session after it
MAX_TRACKS = 99
_ALPHABET = str.maketrans("+/=", "._-")  # MusicBrainz's base64 uses these in place of the characters that are awkward in a URL


class BadToc(ValueError):
    """The table of contents the drive gave is not one a disc can have. The message is written for the person."""

    for_the_person = True


@dataclass(frozen=True)
class Track:
    number: int
    start: int  # sector, Windows numbering
    audio: bool = True


@dataclass(frozen=True)
class Toc:
    tracks: tuple[Track, ...]  # every track on the disc, in order
    leadout: int  # the sector where the lead-out begins (after the last session)

    @classmethod
    def from_reply(cls, reply: dict[str, Any]) -> "Toc":
        """From the helper's answer to `toc`: {"leadout": 258575, "tracks": [{"number": 1, "audio": true, "start": 0}, ...]}."""
        try:
            tracks = tuple(sorted((Track(int(t["number"]), int(t["start"]), bool(t.get("audio", True))) for t in reply["tracks"]), key=lambda t: t.number))
            leadout = int(reply["leadout"])
        except (KeyError, TypeError, ValueError):
            raise BadToc("The drive did not describe the disc in a way this app understands.") from None
        numbers = [t.number for t in tracks]
        starts = [t.start for t in tracks]
        if (
            not tracks
            or len(set(numbers)) != len(numbers)
            or not all(1 <= n <= MAX_TRACKS for n in numbers)
            or any(a >= b for a, b in zip(starts, starts[1:]))
            or starts[0] < 0
            or leadout <= starts[-1]
        ):
            raise BadToc("The disc's table of contents is not one a disc can have. Take it out, put it back in and try again.")
        return cls(tracks, leadout)

    # ------------------------------------------------------------------------------------------ what is on it

    def end_of(self, track: Track) -> int:
        """The sector after the track's last: where the next track begins - or, when the next one is the data track of an Enhanced CD,
        where the audio session ends - or the lead-out."""
        later = [t for t in self.tracks if t.number > track.number]
        if not later:
            return self.leadout
        following = later[0]
        if track.audio and not following.audio:
            return following.start - CD_EXTRA_GAP
        return following.start

    def sectors_of(self, track: Track) -> int:
        return self.end_of(track) - track.start

    def audio_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.audio]

    # ------------------------------------------------------------------------------------------ what MusicBrainz is told

    def _id_tracks(self) -> tuple[list[Track], int]:
        """The tracks that count for the disc ID, and the lead-out it uses: everything up to the last audio track. A data track after
        the audio (an Enhanced CD) is left out, and the lead-out is that of the audio session."""
        audio = self.audio_tracks()
        if not audio:
            return [], self.leadout
        kept = [t for t in self.tracks if t.number <= audio[-1].number]
        trailing = [t for t in self.tracks if t.number > audio[-1].number]
        return kept, (trailing[0].start - CD_EXTRA_GAP if trailing else self.leadout)

    def disc_id(self) -> str | None:
        """The MusicBrainz disc ID (None for a disc with no audio on it): SHA-1 of the first and last track numbers and 100 sector offsets
        (index 0 is the lead-out, 1 to 99 the tracks, 0 where there is no such track), in base64 with + / = replaced by . _ -."""
        kept, leadout = self._id_tracks()
        if not kept:
            return None
        table = [0] * (MAX_TRACKS + 1)
        table[0] = leadout + PREGAP
        for track in kept:
            table[track.number] = track.start + PREGAP
        digest = hashlib.sha1(f"{kept[0].number:02X}{kept[-1].number:02X}".encode() + "".join(f"{value:08X}" for value in table).encode()).digest()
        return base64.b64encode(digest).decode("ascii").translate(_ALPHABET)

    def toc_string(self) -> str | None:
        """'first last leadout offset1 offset2 ...' in MusicBrainz's numbers: what a lookup by table of contents sends when the disc ID is not known."""
        kept, leadout = self._id_tracks()
        if not kept:
            return None
        return " ".join(str(n) for n in [kept[0].number, kept[-1].number, leadout + PREGAP, *(t.start + PREGAP for t in kept)])

    def attach_url(self) -> str | None:
        """Where a person can add this disc to MusicBrainz when it is not known there."""
        text = self.toc_string()
        return None if text is None else "https://musicbrainz.org/cdtoc/attach?toc=" + quote_plus(text)

    def seconds_of(self, track: Track) -> float:
        return self.sectors_of(track) / SECTORS_PER_SECOND
