"""Finding out which album a disc is: MusicBrainz knows discs by an identifier made from their table of contents (see `toc`).

Everything MusicBrainz sends is treated as untrusted text: it ends up in file names and tags, so control characters and the characters that
turn text around are dropped and every field has a length limit.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

import musicbrainzngs

from musictoolkit.discs.toc import SECTORS_PER_SECOND, Toc
from musictoolkit.integrations import musicbrainz_client

logger = logging.getLogger("musictoolkit")

BUSY = (429, 500, 502, 503)  # what MusicBrainz answers when it is overloaded or asks an app to slow down
_UNWANTED = re.compile(r"[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩﻿]")  # controls, and the marks that reorder or hide text
_override_applied = False


class LookupFailed(Exception):
    """The lookup could not be done; the message says why, in words for the person."""

    for_the_person = True


def clean(value: object, limit: int = 300) -> str:
    text = _UNWANTED.sub(" ", str(value if value is not None else ""))
    return " ".join(text.split())[:limit]


def _number(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass
class CandidateTrack:
    number: int
    title: str
    artist: str
    recording_id: str | None = None
    length_ms: int | None = None


@dataclass
class Candidate:
    """One album this disc might be."""

    source: str  # "musicbrainz" or "manual"
    release_id: str | None
    release_group_id: str | None
    title: str
    artist: str
    artist_ids: list[str] = field(default_factory=list)
    date: str = ""
    year: int | None = None
    country: str = ""
    label: str = ""
    barcode: str = ""
    status: str = ""
    disc_number: int = 1
    disc_total: int = 1
    exact: bool = False  # this very disc ID is listed for the album (not just a disc with similar track lengths)
    has_front_cover: bool = False
    tracks: list[CandidateTrack] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _use_the_service_asked_for() -> None:
    """MTK_MUSICBRAINZ_URL points the lookups at a stand-in server (tests and the packaged-app check). Without it, musicbrainz.org over https."""
    global _override_applied
    override = os.environ.get("MTK_MUSICBRAINZ_URL")
    if override:
        parts = urlsplit(override)
        musicbrainzngs.set_hostname(parts.netloc, use_https=parts.scheme == "https")
        _override_applied = True
    elif _override_applied:
        musicbrainzngs.set_hostname("musicbrainz.org", use_https=True)
        _override_applied = False


def _date_key(date: str) -> int:
    digits = date.replace("-", "")
    return int(digits.ljust(8, "9")) if digits.isdigit() else 99999999  # an unknown month or day counts as late in the year


def _candidate(release: dict, disc_id: str, track_count: int) -> Candidate | None:
    media = release.get("medium-list") or []
    chosen = next((m for m in media if disc_id in [d.get("id") for d in m.get("disc-list") or []]), None)
    exact = chosen is not None
    if chosen is None:
        chosen = next((m for m in media if len(m.get("track-list") or []) == track_count), None)
    if chosen is None or len(chosen.get("track-list") or []) != track_count:
        return None  # the tracks would not line up with the disc
    caa = release.get("cover-art-archive") or {}
    date = clean(release.get("date"), 10)
    labels = release.get("label-info-list") or []
    credit = release.get("artist-credit") or []
    tracks = []
    for index, entry in enumerate(chosen["track-list"], start=1):
        recording = entry.get("recording") or {}
        tracks.append(CandidateTrack(
            number=_number(entry.get("position")) or index,
            title=clean(entry.get("title") or recording.get("title")),
            artist=clean(entry.get("artist-credit-phrase") or recording.get("artist-credit-phrase") or release.get("artist-credit-phrase")),
            recording_id=clean(recording.get("id"), 40) or None,
            length_ms=_number(entry.get("length") or recording.get("length")),
        ))
    return Candidate(
        source="musicbrainz",
        release_id=clean(release.get("id"), 40) or None,
        release_group_id=clean((release.get("release-group") or {}).get("id"), 40) or None,
        title=clean(release.get("title")),
        artist=clean(release.get("artist-credit-phrase")),
        artist_ids=[clean(c["artist"].get("id"), 40) for c in credit if isinstance(c, dict) and c.get("artist", {}).get("id")],
        date=date,
        year=_number(date[:4]) if date[:4].isdigit() else None,
        country=clean(release.get("country"), 10),
        label=clean(next((l["label"]["name"] for l in labels if l.get("label", {}).get("name")), "")),
        barcode=clean(release.get("barcode"), 20),
        status=clean(release.get("status"), 30),
        disc_number=_number(chosen.get("position")) or 1,
        disc_total=_number(release.get("medium-count")) or len(media) or 1,
        exact=exact,
        has_front_cover=str(caa.get("front", "")).lower() == "true",
        tracks=tracks,
    )


def find_releases(toc: Toc) -> list[Candidate]:
    """The albums this disc might be, best guess first: this very disc ID, official releases, ones with a front cover, earlier ones.
    An empty list when MusicBrainz does not know the disc. Raises LookupFailed when it could not be asked."""
    disc_id = toc.disc_id()
    text = toc.toc_string()
    if disc_id is None or text is None:
        return []
    _use_the_service_asked_for()
    try:
        releases = musicbrainz_client.releases_for_disc(disc_id, text)
    except RuntimeError:
        raise LookupFailed("MusicBrainz asks every app to give a contact email. Add yours in Settings, Accounts.") from None
    except musicbrainzngs.NetworkError as exc:
        # The library tries a server that says "busy" (500, 502, 503) several times over a minute, then gives up with a NetworkError.
        if getattr(exc.cause, "code", None) in BUSY:
            raise LookupFailed("MusicBrainz is busy right now (it limits how fast apps may ask). Try again in a minute.") from None
        raise LookupFailed("MusicBrainz could not be reached. Check the internet connection and look again.") from None
    except musicbrainzngs.ResponseError as exc:  # (a server that is busy never gets here: the library calls that a NetworkError)
        code = getattr(exc.cause, "code", None)
        raise LookupFailed(f"MusicBrainz answered with an error{f' ({code})' if code else ''}.") from None
    except musicbrainzngs.WebServiceError as exc:
        logger.warning("MusicBrainz lookup failed: %s", exc)
        raise LookupFailed("MusicBrainz could not be asked right now.") from None
    count = len(toc._id_tracks()[0])
    found: dict[str, Candidate] = {}
    for release in releases:
        candidate = _candidate(release, disc_id, count)
        if candidate is not None and candidate.release_id and candidate.release_id not in found:
            found[candidate.release_id] = candidate
    return sorted(
        found.values(),
        key=lambda c: (c.exact, c.status.lower() == "official", c.has_front_cover, -_date_key(c.date)),
        reverse=True,
    )


def manual_candidate(toc: Toc) -> Candidate:
    """Nothing known about the disc: the tracks with numbers and lengths from the disc itself, to be named by hand."""
    audio = toc.audio_tracks()
    return Candidate(
        source="manual", release_id=None, release_group_id=None, title="", artist="",
        tracks=[
            CandidateTrack(number=i, title=f"Track {i:02d}", artist="", length_ms=round(toc.sectors_of(t) * 1000 / SECTORS_PER_SECOND))
            for i, t in enumerate(audio, start=1)
        ],
    )
