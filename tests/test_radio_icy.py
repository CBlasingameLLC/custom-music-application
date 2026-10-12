"""radio.icy: the audio is separated from the "what is playing" text, however the stream is cut into pieces."""

from __future__ import annotations

import random

import pytest

from musictoolkit.radio.icy import IcyDemuxer, metaint_from, split_title


def stream(audio: bytes, metaint: int, blocks: list[bytes]) -> bytes:
    """What a station sends: `metaint` bytes of audio, a metadata block, `metaint` more ... (a block is its text, padded here)."""
    body = bytearray()
    for n, block in enumerate(blocks):
        body += audio[n * metaint : (n + 1) * metaint]
        padded = block + b"\0" * (-len(block) % 16)
        body += bytes([len(padded) // 16]) + padded
    return bytes(body)


def feed_in_pieces(demuxer: IcyDemuxer, body: bytes, sizes: list[int]) -> bytes:
    out, i = bytearray(), 0
    for size in sizes:
        out += demuxer.feed(body[i : i + size])
        i += size
    out += demuxer.feed(body[i:])
    return bytes(out)


def test_the_audio_comes_out_alone_and_the_title_is_remembered() -> None:
    audio = bytes(range(256)) * 4
    body = stream(audio, 100, [b"StreamTitle='Miles Davis - So What';StreamUrl='';", b"", b"StreamTitle='Next One';"])
    demuxer = IcyDemuxer(100)
    heard = demuxer.feed(body)
    assert heard == audio[:300] and demuxer.title == "Next One" and demuxer.titles_seen == 2


def test_it_makes_no_difference_where_the_stream_is_cut() -> None:
    rng = random.Random(7)
    audio = bytes(rng.randrange(256) for _ in range(5000))
    blocks = [b"StreamTitle='One';", b"StreamTitle='Two - Deux';StreamUrl='http://x';", b"", b"StreamTitle='Three';"] * 3
    body = stream(audio, 400, blocks)
    expected = audio[: 400 * len(blocks)]
    for trial in range(60):
        sizes = [rng.choice([1, 1, 2, 3, 7, 16, 17, 399, 400, 401, 1000]) for _ in range(rng.randrange(1, 40))]
        demuxer = IcyDemuxer(400)
        assert feed_in_pieces(demuxer, body, sizes) == expected, f"trial {trial}, cut as {sizes}"
        assert demuxer.title == "Three" and demuxer.titles_seen == 9


def test_one_byte_at_a_time() -> None:
    audio = bytes(range(200))
    body = stream(audio, 50, [b"StreamTitle='A';", b"StreamTitle='B';"])
    demuxer = IcyDemuxer(50)
    assert b"".join(demuxer.feed(bytes([b])) for b in body) == audio[:100] and demuxer.title == "B"


def test_a_block_may_be_the_longest_the_format_allows() -> None:
    long_title = "x" * 4000
    block = f"StreamTitle='{long_title}';".encode()
    block = block[: 255 * 16]
    body = stream(b"a" * 20, 10, [block, b""])
    demuxer = IcyDemuxer(10)
    assert demuxer.feed(body) == b"a" * 20
    assert demuxer.title is not None and len(demuxer.title) <= 300, "a title is cut short rather than filling the screen"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("StreamTitle='It''s Here';".replace("''", "'"), "It's Here"),  # an apostrophe in the title
        ("StreamTitle='Björk - Jóga';", "Björk - Jóga"),
        ("StreamTitle='   spaced    out  ';", "spaced out"),
        ("StreamTitle='';StreamUrl='';", None),
        ("StreamUrl='http://nothing';", None),
    ],
)
def test_titles_are_read_out_of_the_block(text, expected) -> None:
    demuxer = IcyDemuxer(4)
    demuxer.feed(stream(b"abcd", 4, [text.encode("utf-8")]))
    assert demuxer.title == expected


def test_an_older_server_that_sends_latin_1_is_read_too() -> None:
    demuxer = IcyDemuxer(4)
    demuxer.feed(stream(b"abcd", 4, ["StreamTitle='Beyoncé - Déjà Vu';".encode("latin-1")]))
    assert demuxer.title == "Beyoncé - Déjà Vu"


def test_an_empty_title_clears_the_last_one() -> None:
    demuxer = IcyDemuxer(4)
    demuxer.feed(stream(b"abcdefgh", 4, [b"StreamTitle='Song';", b"StreamTitle='';"]))
    assert demuxer.title is None and demuxer.titles_seen == 2


@pytest.mark.parametrize("title, expected", [
    ("Miles Davis - So What", ("Miles Davis", "So What")),
    ("A - B - C", ("A", "B - C")),
    ("Just a name", (None, "Just a name")),
    (" - leading", (None, "- leading")),
    ("", (None, None)),
    (None, (None, None)),
])
def test_artist_and_title_are_told_apart_when_the_station_joins_them(title, expected) -> None:
    assert split_title(title) == expected


@pytest.mark.parametrize("headers, expected", [
    ({"icy-metaint": "16000"}, 16000),
    ({"Icy-MetaInt": " 8192 "}, 8192),
    ({"icy-metaint": "0"}, 0),
    ({"icy-metaint": "-5"}, 0),
    ({"icy-metaint": "lots"}, 0),
    ({"icy-metaint": "99999999"}, 0),
    ({}, 0),
])
def test_the_interval_comes_from_the_headers_or_is_zero(headers, expected) -> None:
    assert metaint_from(headers) == expected


def test_an_interval_of_zero_is_not_accepted() -> None:
    with pytest.raises(ValueError):
        IcyDemuxer(0)
