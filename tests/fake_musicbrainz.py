"""Stand-ins for MusicBrainz and the Cover Art Archive, for the disc tests: scripts for tests/fake_streams.serve_script.

    with serve_script(musicbrainz_script(known={disc_id: [release_xml(key="a", title="Album")]})) as server:
        os.environ["MTK_MUSICBRAINZ_URL"] = server.url

MusicBrainz answers in XML (musicbrainzngs reads nothing else); `release_xml` makes one release the way the web service describes it.
tests/fixtures/musicbrainz_discid_example.xml is the real answer for the example disc in python-discid's documentation.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

NS = "http://musicbrainz.org/ns/mmd-2.0#"
REAL_ANSWER = Path(__file__).parent / "fixtures" / "musicbrainz_discid_example.xml"


# The example disc in python-discid's documentation (15 tracks); MusicBrainz knows it by this ID. Its numbers are MusicBrainz's, which count from
# the start of the disc: 150 sectors before the numbers the drive gives.
EXAMPLE_ID = "TqvKjMu7dMliSfmVEBtrL7sBSno-"
EXAMPLE_OFFSETS = [150, 17510, 33275, 45910, 57805, 78310, 94650, 109580, 132010, 149160, 165115, 177710, 203325, 215555, 235590]
EXAMPLE_LEADOUT = 258725


def toc_from_musicbrainz_numbers(offsets: list[int], leadout: int, first: int = 1):
    from musictoolkit.discs.toc import Toc, Track

    return Toc(tuple(Track(first + i, offset - 150) for i, offset in enumerate(offsets)), leadout - 150)


def example_toc():
    return toc_from_musicbrainz_numbers(EXAMPLE_OFFSETS, EXAMPLE_LEADOUT)


def three_track_toc():
    """A small disc: 3 audio tracks of 20, 30 and 25 seconds."""
    return toc_from_musicbrainz_numbers([150, 150 + 1500, 150 + 1500 + 2250], 150 + 1500 + 2250 + 1875)


def ident(seed: str) -> str:
    """A stable id that looks like the ones MusicBrainz hands out."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "music-toolkit-test/" + seed))


def release_xml(
    *, key: str, title: str = "Album", artist: str = "Artist", date: str = "2001-03-04", status: str = "Official", country: str = "GB",
    label: str = "Some Label", barcode: str = "5012345678900", tracks: tuple[tuple[str, int], ...] = (("First", 180000), ("Second", 200000), ("Third", 190000)),
    disc_ids: tuple[str, ...] = (), position: int = 1, media: int = 1, front: bool = True, track_artists: tuple[str, ...] | None = None,
) -> str:
    """One <release>: `tracks` are (title, length in milliseconds); `disc_ids` the disc IDs listed on its medium (none: a similar-length match)."""
    def credit(name: str) -> str:
        return f'<artist-credit><name-credit><artist id="{ident("artist/" + name)}"><name>{escape(name)}</name><sort-name>{escape(name)}</sort-name></artist></name-credit></artist-credit>'

    discs = "".join(f'<disc id="{d}"><sectors>1000</sectors></disc>' for d in disc_ids)
    listed = ""
    for n, (name, length) in enumerate(tracks, start=1):
        by = track_artists[n - 1] if track_artists else artist
        listed += (
            f'<track id="{ident(f"{key}/track/{n}")}"><position>{n}</position><number>{n}</number><length>{length}</length>'
            f'<recording id="{ident(f"{key}/recording/{n}")}"><title>{escape(name)}</title><length>{length}</length>{credit(by)}</recording>{credit(by)}</track>'
        )
    return (
        f'<release id="{ident(key)}"><title>{escape(title)}</title><status>{escape(status)}</status>{credit(artist)}'
        f'<release-group id="{ident("group/" + key)}" type="Album"><title>{escape(title)}</title></release-group>'
        f'<date>{date}</date><country>{country}</country><barcode>{barcode}</barcode>'
        f'<cover-art-archive><artwork>{str(front).lower()}</artwork><count>{1 if front else 0}</count><front>{str(front).lower()}</front><back>false</back></cover-art-archive>'
        f'<label-info-list count="1"><label-info><catalog-number>CAT 1</catalog-number><label id="{ident("label/" + label)}"><name>{escape(label)}</name></label></label-info></label-info-list>'
        f'<medium-list count="{media}"><medium><position>{position}</position><format>CD</format>'
        f'<disc-list count="{len(disc_ids)}">{discs}</disc-list><track-list count="{len(tracks)}" offset="0">{listed}</track-list></medium></medium-list></release>'
    )


def _document(inner: str) -> bytes:
    return f'<?xml version="1.0" encoding="UTF-8"?><metadata xmlns="{NS}">{inner}</metadata>'.encode()


def musicbrainz_script(
    *, known: dict[str, list[str]] | None = None, fuzzy: list[str] | None = None, documents: dict[str, bytes] | None = None, status: int | None = None
) -> Callable[[BaseHTTPRequestHandler], None]:
    """What /ws/2/discid/<id> answers: `known` maps a disc ID to releases (an exact match); an ID it does not know gets the `fuzzy` releases
    when a table of contents comes with it, else 404; `documents` maps an ID to a whole answer, as it is; `status` answers that always."""

    def reply(h: BaseHTTPRequestHandler, code: int, body: bytes = b"", content_type: str = "application/xml; charset=utf-8") -> None:
        h.send_response(code)
        h.send_header("Content-Type", content_type)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def script(h: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(h.path)
        asked = parse_qs(parts.query)
        found = re.fullmatch(r"/ws/2/discid/([^/]+)", parts.path)
        if status:
            return reply(h, status)
        if not found:
            return reply(h, 404)
        disc_id = found.group(1)
        if documents and disc_id in documents:
            return reply(h, 200, documents[disc_id])
        if known and disc_id in known:
            releases = known[disc_id]
            return reply(h, 200, _document(
                f'<disc id="{disc_id}"><sectors>1000</sectors><offset-list count="0"/><release-list count="{len(releases)}">{"".join(releases)}</release-list></disc>'
            ))
        if fuzzy and "toc" in asked:
            return reply(h, 200, _document(f'<release-list count="{len(fuzzy)}">{"".join(fuzzy)}</release-list>'))
        return reply(h, 404, _document(""))

    return script


def coverart_script(images: dict[str, bytes], *, content_type: str = "image/png") -> Callable[[BaseHTTPRequestHandler], None]:
    """The Cover Art Archive: /release/<id>/front-1200 (or /release-group/<id>/...) sends on to an image, like the real one does to archive.org.
    `images` maps "release/<id>" or "release-group/<id>" to the picture's bytes; anything else is 404."""

    def script(h: BaseHTTPRequestHandler) -> None:
        path = urlsplit(h.path).path
        asked = re.fullmatch(r"/(release|release-group)/([0-9a-f-]+)/front-1200", path)
        stored = re.fullmatch(r"/image/(release|release-group)/([0-9a-f-]+)", path)
        if asked and f"{asked.group(1)}/{asked.group(2)}" in images:
            h.send_response(307)
            h.send_header("Location", f"/image/{asked.group(1)}/{asked.group(2)}")
            h.send_header("Content-Length", "0")
            h.end_headers()
        elif stored and f"{stored.group(1)}/{stored.group(2)}" in images:
            body = images[f"{stored.group(1)}/{stored.group(2)}"]
            h.send_response(200)
            h.send_header("Content-Type", content_type)
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
        else:
            h.send_response(404)
            h.send_header("Content-Length", "0")
            h.end_headers()

    return script
