"""Finding the album for a disc: MusicBrainz is stood in for by a server on this computer that answers in the web service's XML."""

from __future__ import annotations

import socket

import musicbrainzngs
import pytest

from musictoolkit.discs.lookup import LookupFailed, find_releases, manual_candidate
from musictoolkit.integrations import musicbrainz_client
from tests.fake_musicbrainz import EXAMPLE_ID, REAL_ANSWER, example_toc, musicbrainz_script, release_xml, three_track_toc
from tests.fake_streams import serve_script

TOC3 = three_track_toc()
ID3 = TOC3.disc_id()


def asked(monkeypatch: pytest.MonkeyPatch, server) -> None:
    monkeypatch.setenv("MTK_MUSICBRAINZ_URL", server.url)


def release(key: str, **kw) -> str:
    return release_xml(key=key, disc_ids=kw.pop("disc_ids", (ID3,)), **kw)


# ---------------------------------------------------------------------------------------- what comes back


def test_the_real_answer_for_the_documented_disc_is_understood(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(musicbrainz_script(documents={EXAMPLE_ID: REAL_ANSWER.read_bytes()})) as server:
        asked(monkeypatch, server)
        found = find_releases(example_toc())
    assert len(found) == 1
    album = found[0]
    assert (album.title, album.artist, album.year, album.country, album.status) == ("Don’t Give Me Names", "Guano Apes", 2000, "XE", "Official")
    assert album.exact and album.has_front_cover and album.label == "BMG" and album.barcode == "743217522421"
    assert (album.disc_number, album.disc_total, album.source) == (1, 1, "musicbrainz")
    assert album.release_id == "07090529-0fbf-4bd3-adc4-fe627343976d" and album.release_group_id == "9a3641db-6324-3db1-a406-075a2336e8bb"
    assert album.artist_ids == ["66da25f9-1534-4dd1-b88c-718bc24e1ccd"]
    assert len(album.tracks) == 15
    assert [t.number for t in album.tracks] == list(range(1, 16))
    third = album.tracks[2]
    assert (third.title, third.artist, third.recording_id) == ("Big in Japan", "Guano Apes", album.tracks[2].recording_id) and third.recording_id
    assert album.tracks[0].title == "Innocent Greed" and album.tracks[0].length_ms == 231466


def test_the_question_names_the_disc_carries_its_table_of_contents_and_says_who_is_asking(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(musicbrainz_script(known={ID3: [release("a")]})) as server:
        asked(monkeypatch, server)
        find_releases(TOC3)
    request = server.requests[0]
    assert request["path"].startswith(f"/ws/2/discid/{ID3}?")
    assert "toc=1+3+" in request["path"] and "cdstubs=no" in request["path"] and "recordings" in request["path"]
    assert "tests@example.com" in request["headers"]["User-Agent"] and "Music Toolkit tests/0" in request["headers"]["User-Agent"]


def test_a_disc_with_no_listing_gets_the_albums_with_similar_track_lengths_marked_as_not_exact(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(musicbrainz_script(fuzzy=[release("close", title="Close Enough", disc_ids=())])) as server:
        asked(monkeypatch, server)
        found = find_releases(TOC3)
    assert [(c.title, c.exact) for c in found] == [("Close Enough", False)]


def test_a_disc_nobody_knows_gives_nothing(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(musicbrainz_script()) as server:
        asked(monkeypatch, server)
        assert find_releases(TOC3) == []


def test_a_disc_with_no_audio_is_not_looked_up(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    from musictoolkit.discs.toc import Toc, Track

    with serve_script(musicbrainz_script()) as server:
        asked(monkeypatch, server)
        assert find_releases(Toc((Track(1, 0, audio=False),), 5000)) == []
    assert server.requests == []


# ---------------------------------------------------------------------------------------- which is the likeliest


def test_the_albums_are_ordered_exact_then_official_then_with_a_cover_then_earlier(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    releases = [
        release("late", title="Late", date="2005"),
        release("early", title="Early", date="2001-03-04"),
        release("nocover", title="No cover", date="2000", front=False),
        release("promo", title="Promo", date="1999", status="Promotion"),
        release("similar", title="Similar", date="1990", disc_ids=()),
    ]
    with serve_script(musicbrainz_script(known={ID3: releases})) as server:
        asked(monkeypatch, server)
        found = find_releases(TOC3)
    assert [c.title for c in found] == ["Early", "Late", "No cover", "Promo", "Similar"]
    assert [c.exact for c in found] == [True, True, True, True, False]


def test_an_album_whose_tracks_would_not_line_up_with_the_disc_is_left_out(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    four = release("four", title="Four tracks", tracks=(("A", 1000), ("B", 1000), ("C", 1000), ("D", 1000)))
    with serve_script(musicbrainz_script(known={ID3: [four, release("three", title="Three tracks")]})) as server:
        asked(monkeypatch, server)
        assert [c.title for c in find_releases(TOC3)] == ["Three tracks"]


def test_the_same_album_listed_twice_is_one_candidate(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(musicbrainz_script(known={ID3: [release("same"), release("same")]})) as server:
        asked(monkeypatch, server)
        assert len(find_releases(TOC3)) == 1


def test_the_disc_of_a_set_says_which_disc_it_is(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(musicbrainz_script(known={ID3: [release("set", title="Two discs", position=2, media=2)]})) as server:
        asked(monkeypatch, server)
        album = find_releases(TOC3)[0]
    assert (album.disc_number, album.disc_total) == (2, 2)


def test_a_compilation_keeps_each_tracks_own_artist(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    mix = release("mix", title="Hits", artist="Various Artists", track_artists=("Band One", "Band Two", "Band Three"))
    with serve_script(musicbrainz_script(known={ID3: [mix]})) as server:
        asked(monkeypatch, server)
        album = find_releases(TOC3)[0]
    assert album.artist == "Various Artists" and [t.artist for t in album.tracks] == ["Band One", "Band Two", "Band Three"]


# ---------------------------------------------------------------------------------------- text from outside is not trusted


def test_control_characters_reordering_marks_and_huge_text_are_cleaned(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    hostile = release(
        "bad", title="Evil\u0085\u009b Album‮ txt.exe﻿", artist="A" * 5000,
        tracks=(("Line\none\t\ttwo", 1000), ("Normal", 1000), ("B" * 5000, 1000)),
    )  # (the controls XML allows: C1 controls, the marks that reverse text, a byte-order mark, newlines and tabs)
    with serve_script(musicbrainz_script(known={ID3: [hostile]})) as server:
        asked(monkeypatch, server)
        album = find_releases(TOC3)[0]
    assert album.title == "Evil Album txt.exe"
    assert len(album.artist) == 300 and len(album.tracks[2].title) == 300
    assert album.tracks[0].title == "Line one two"
    for text in (album.title, album.artist, *(t.title for t in album.tracks)):
        assert not any(ord(ch) < 32 or ch in "‮﻿" for ch in text)


# ---------------------------------------------------------------------------------------- when it goes wrong


def test_a_busy_server_is_told_apart_from_no_connection(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(musicbrainz_script(status=503)) as server:
        asked(monkeypatch, server)
        with pytest.raises(LookupFailed, match="busy"):
            find_releases(TOC3)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("MTK_MUSICBRAINZ_URL", f"http://127.0.0.1:{port}")  # nothing is listening there any more
    with pytest.raises(LookupFailed, match="could not be reached"):
        find_releases(TOC3)


def test_without_a_contact_the_person_is_asked_for_one(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(musicbrainz_client, "_configured", False)
    with pytest.raises(LookupFailed, match="contact email"):
        find_releases(TOC3)


def test_the_stand_in_is_used_only_while_it_is_asked_for(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(musicbrainz_script(known={ID3: [release("a")]})) as server:
        asked(monkeypatch, server)
        find_releases(TOC3)
        assert mb.hostname.startswith("127.0.0.1:") and mb.https is False
    monkeypatch.delenv("MTK_MUSICBRAINZ_URL")
    with pytest.raises(LookupFailed):  # the real address is not reachable from a test, but it is the one tried
        monkeypatch.setattr(mb, "_safe_read", lambda *a, **k: (_ for _ in ()).throw(musicbrainzngs.NetworkError("offline")))
        find_releases(TOC3)
    assert mb.hostname == "musicbrainz.org" and mb.https is True


# ---------------------------------------------------------------------------------------- nothing known: type it in


def test_a_disc_nobody_knows_still_gets_a_track_list_to_fill_in() -> None:
    album = manual_candidate(TOC3)
    assert (album.source, album.release_id, album.title, album.artist) == ("manual", None, "", "")
    assert [(t.number, t.title, t.length_ms) for t in album.tracks] == [(1, "Track 01", 20000), (2, "Track 02", 30000), (3, "Track 03", 25000)]


def test_a_server_that_asks_for_slower_requests_is_told_apart_from_no_connection(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    with serve_script(musicbrainz_script(status=429)) as server:
        asked(monkeypatch, server)
        with pytest.raises(LookupFailed, match="busy"):
            find_releases(TOC3)


def test_a_release_known_only_by_its_year_is_not_taken_for_an_earlier_one(mb, monkeypatch: pytest.MonkeyPatch) -> None:
    releases = [release("year-only", title="Year only", date="2001"), release("dated", title="Dated", date="2001-03-04")]
    with serve_script(musicbrainz_script(known={ID3: releases})) as server:
        asked(monkeypatch, server)
        assert [c.title for c in find_releases(TOC3)] == ["Dated", "Year only"]
