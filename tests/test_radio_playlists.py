"""radio.playlists: the stream inside a station's playlist file."""

from __future__ import annotations

import pytest

from musictoolkit.radio.playlists import UnplayableStream, first_stream, is_hls, looks_like_a_playlist

BASE = "http://radio.example/listen.pls"


def test_a_pls_file_gives_its_first_entry() -> None:
    pls = b"[playlist]\nNumberOfEntries=2\nFile1=http://stream.example:8000/live\nTitle1=Live\nLength1=-1\nFile2=http://backup.example/live\nVersion=2\n"
    assert first_stream(pls, BASE) == "http://stream.example:8000/live"


def test_an_m3u_file_gives_its_first_address_and_skips_comments() -> None:
    m3u = b"#EXTM3U\n#EXTINF:-1,Station\n\nhttp://stream.example/a.mp3\nhttp://stream.example/b.mp3\n"
    assert first_stream(m3u, "http://radio.example/x.m3u") == "http://stream.example/a.mp3"


def test_relative_entries_are_taken_from_the_playlists_own_address() -> None:
    assert first_stream(b"/live/stream.aac\n", "https://radio.example/x/list.m3u") == "https://radio.example/live/stream.aac"
    assert first_stream(b"stream.aac\n", "https://radio.example/x/list.m3u") == "https://radio.example/x/stream.aac"


def test_an_asx_file_gives_its_first_ref() -> None:
    asx = b'<asx version="3.0"><title>Radio</title><entry><ref href="http://wm.example/live.wma"/></entry></asx>'
    assert first_stream(asx, "http://radio.example/x.asx") == "http://wm.example/live.wma"


def test_entries_that_cannot_be_used_are_skipped() -> None:
    pls = b"[playlist]\nFile1=ftp://old.example/x\nFile2=http://user:pw@host.example/y\nFile3=http://ok.example/z\n"
    assert first_stream(pls, BASE) == "http://ok.example/z"


def test_a_playlist_with_nothing_usable_says_so() -> None:
    with pytest.raises(UnplayableStream, match="does not hold an address"):
        first_stream(b"#EXTM3U\n# nothing here\n", BASE)
    with pytest.raises(UnplayableStream, match="does not hold an address"):
        first_stream(b"\xff\xfe\x00garbage\x00", BASE)


def test_an_hls_playlist_is_recognised_and_not_mistaken_for_a_list_of_streams() -> None:
    hls = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nsegment1.ts\n"
    assert is_hls(hls) and not is_hls(b"#EXTM3U\nhttp://x/y\n")
    with pytest.raises(UnplayableStream, match="HLS"):
        first_stream(hls, "http://radio.example/live.m3u8")


@pytest.mark.parametrize(
    "content_type, url, expected",
    [
        ("audio/x-scpls", "http://x/stream", True),
        ("audio/mpegurl; charset=utf-8", "http://x/stream", True),
        ("application/vnd.apple.mpegurl", "http://x/stream", True),
        ("audio/mpeg", "http://x/live.pls", True),
        ("text/plain", "http://x/path/list.M3U?token=1", True),
        ("audio/mpeg", "http://x/stream", False),
        ("audio/aacp", "http://x/stream.aac", False),
        ("", "http://x/", False),
    ],
)
def test_playlist_files_are_told_from_streams_by_type_or_name(content_type, url, expected) -> None:
    assert looks_like_a_playlist(content_type, url) is expected
