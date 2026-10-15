"""Table of contents and disc IDs. The expected IDs are not worked out by the code under test: they are the ones MusicBrainz itself lists."""

from __future__ import annotations

import base64
import hashlib
import xml.etree.ElementTree as ET

import pytest

from musictoolkit.discs.toc import CD_EXTRA_GAP, BadToc, Toc, Track
from tests.fake_musicbrainz import EXAMPLE_ID, EXAMPLE_LEADOUT, EXAMPLE_OFFSETS, REAL_ANSWER, example_toc, toc_from_musicbrainz_numbers

NS = {"mb": "http://musicbrainz.org/ns/mmd-2.0#"}


def test_the_documented_example_disc_gets_the_id_musicbrainz_knows_it_by() -> None:
    assert example_toc().disc_id() == EXAMPLE_ID


def test_every_disc_in_a_real_musicbrainz_answer_gets_the_id_musicbrainz_lists_for_it() -> None:
    """The answer for the example disc lists three pressings of the album, each with its own offsets, lead-out and ID: three checks that
    belong to MusicBrainz and not to this code."""
    discs = ET.parse(REAL_ANSWER).getroot().findall(".//mb:medium/mb:disc-list/mb:disc", NS)
    assert len(discs) == 3
    for disc in discs:
        offsets = [int(o.text) for o in disc.findall("mb:offset-list/mb:offset", NS)]
        leadout = int(disc.find("mb:sectors", NS).text)
        assert toc_from_musicbrainz_numbers(offsets, leadout).disc_id() == disc.get("id"), disc.get("id")


def test_the_table_of_contents_text_for_a_lookup_by_length() -> None:
    text = example_toc().toc_string()
    assert text == "1 15 258725 " + " ".join(str(o) for o in EXAMPLE_OFFSETS)
    url = example_toc().attach_url()
    assert url == "https://musicbrainz.org/cdtoc/attach?toc=" + text.replace(" ", "+")


def test_track_lengths_come_from_where_the_next_track_starts() -> None:
    toc = example_toc()
    first, last = toc.tracks[0], toc.tracks[-1]
    assert toc.sectors_of(first) == 17510 - 150
    assert toc.end_of(last) == EXAMPLE_LEADOUT - 150 and toc.sectors_of(last) == EXAMPLE_LEADOUT - 235590
    assert toc.seconds_of(first) == pytest.approx((17510 - 150) / 75)
    assert [t.number for t in toc.audio_tracks()] == list(range(1, 16))


# ---------------------------------------------------------------------------------------- data tracks


def enhanced(audio_leadout: int = 60000) -> Toc:
    """Three audio tracks, then (an Enhanced CD) a data track in a second session that starts 11400 sectors after the audio ends."""
    audio = (Track(1, 0), Track(2, 20000), Track(3, 40000))
    return Toc((*audio, Track(4, audio_leadout + CD_EXTRA_GAP, audio=False)), audio_leadout + CD_EXTRA_GAP + 30000)


def test_an_enhanced_cd_is_identified_by_its_audio_alone() -> None:
    """MusicBrainz's rule: the data track after the audio is left out, and the lead-out of the audio session is where the data starts, less 11400."""
    plain = Toc((Track(1, 0), Track(2, 20000), Track(3, 40000)), 60000)
    assert enhanced().disc_id() == plain.disc_id()
    assert enhanced().toc_string() == plain.toc_string()
    assert enhanced().end_of(enhanced().tracks[2]) == 60000, "the last song ends where the audio session does, not where the data begins"
    assert [t.number for t in enhanced().audio_tracks()] == [1, 2, 3]


def test_a_data_track_before_the_audio_is_kept_in_the_id() -> None:
    mixed = Toc((Track(1, 0, audio=False), Track(2, 1000), Track(3, 5000)), 9000)
    audio_only = Toc((Track(2, 1000), Track(3, 5000)), 9000)
    assert mixed.disc_id() not in (None, audio_only.disc_id())
    assert mixed.toc_string().split()[:2] == ["1", "3"]
    assert [t.number for t in mixed.audio_tracks()] == [2, 3]


def test_a_disc_with_no_audio_has_no_id() -> None:
    data = Toc((Track(1, 0, audio=False),), 5000)
    assert data.disc_id() is None and data.toc_string() is None and data.attach_url() is None


def test_tracks_are_put_where_their_numbers_say_when_the_first_is_not_1() -> None:
    toc = Toc((Track(2, 500), Track(3, 2000), Track(4, 3500)), 6000)
    table = [0] * 100
    table[0], table[2], table[3], table[4] = 6150, 650, 2150, 3650
    digest = hashlib.sha1(("02" + "04" + "".join(f"{v:08X}" for v in table)).encode()).digest()
    expected = base64.b64encode(digest).decode().replace("+", ".").replace("/", "_").replace("=", "-")
    assert toc.disc_id() == expected
    assert toc.toc_string() == "2 4 6150 650 2150 3650"


# ---------------------------------------------------------------------------------------- what the drive may hand over


def reply(**changes) -> dict:
    base = {"leadout": 300, "tracks": [{"number": 1, "audio": True, "start": 0}, {"number": 2, "audio": True, "start": 100}]}
    return {**base, **changes}


def test_the_helpers_answer_becomes_a_toc_sorted_by_track_number() -> None:
    toc = Toc.from_reply(reply(tracks=[{"number": 2, "audio": True, "start": 100}, {"number": 1, "audio": True, "start": 0}]))
    assert [(t.number, t.start, t.audio) for t in toc.tracks] == [(1, 0, True), (2, 100, True)] and toc.leadout == 300


@pytest.mark.parametrize("bad", [
    {"tracks": None},                                                                                      # no list of tracks
    {"tracks": []},                                                                                        # none on the disc
    {"leadout": "soon"},                                                                                   # not a number
    {"tracks": [{"number": 1, "start": 0}, {"number": 1, "start": 100}]},                                  # the same number twice
    {"tracks": [{"number": 1, "start": 100}, {"number": 2, "start": 0}]},                                  # out of order on the disc
    {"tracks": [{"number": 0, "start": 0}]},                                                               # no track 0
    {"tracks": [{"number": 100, "start": 0}]},                                                             # nor 100
    {"leadout": 100},                                                                                      # the lead-out not after the last track
    {"tracks": [{"number": 1, "start": -5}]},                                                              # before the disc
])
def test_a_table_of_contents_no_disc_can_have_is_refused(bad: dict) -> None:
    with pytest.raises(BadToc):
        Toc.from_reply({**reply(), **bad})


def test_the_gap_between_the_audio_and_the_data_of_an_enhanced_cd_is_eleven_thousand_four_hundred_sectors() -> None:
    """The number is MusicBrainz's (the end of the first session, the start of the second and its pre-gap), so it is written out here."""
    assert CD_EXTRA_GAP == 11400
    toc = Toc((Track(1, 0), Track(2, 20000), Track(3, 71400, audio=False)), 100000)
    assert toc.end_of(toc.tracks[1]) == 60000 and toc.sectors_of(toc.tracks[1]) == 40000
    assert toc.disc_id() == Toc((Track(1, 0), Track(2, 20000)), 60000).disc_id()
