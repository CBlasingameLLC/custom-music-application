"""podcasts.feed: reading a feed from a stranger's server."""

from __future__ import annotations

import pytest

from musictoolkit.podcasts.feed import FeedError, parse_date, parse_duration, parse_feed, plain_text

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:media="http://search.yahoo.com/mrss/">
<channel>
  <title>The &amp; Show</title>
  <link>https://show.example/</link>
  <description>&lt;p&gt;Weekly &lt;b&gt;talk&lt;/b&gt;.&lt;/p&gt;</description>
  <itunes:author>Jo Host</itunes:author>
  <itunes:image href="https://show.example/art.jpg"/>
  <item>
    <title>Episode 2</title>
    <guid isPermaLink="false">ep-2</guid>
    <pubDate>Tue, 03 Jun 2025 10:00:00 +0000</pubDate>
    <enclosure url="https://cdn.example/ep2.mp3?x=1" type="audio/mpeg" length="12345678"/>
    <itunes:duration>1:02:03</itunes:duration>
    <itunes:season>2</itunes:season><itunes:episode>2</itunes:episode>
    <itunes:image href="https://show.example/ep2.jpg"/>
    <description>Short &amp; sweet</description>
    <content:encoded><![CDATA[<p>The <a href="https://x.example">long</a> notes.</p><p>Second paragraph.</p><script>alert(1)</script>]]></content:encoded>
  </item>
  <item>
    <title>Episode 1</title>
    <guid>ep-1</guid>
    <pubDate>Tue, 27 May 2025 10:00:00 -0500</pubDate>
    <enclosure url="https://cdn.example/ep1.m4a" type="audio/x-m4a" length="0"/>
    <itunes:duration>2110</itunes:duration>
  </item>
  <item>
    <title>A video, not for us</title>
    <guid>vid</guid>
    <enclosure url="https://cdn.example/v.mp4" type="video/mp4" length="9"/>
  </item>
  <item><title>No audio at all</title><guid>none</guid></item>
  <item><title>Bad address</title><guid>bad</guid><enclosure url="file:///etc/passwd" type="audio/mpeg"/></item>
  <item><title>Typeless</title><guid>typeless</guid><enclosure url="https://cdn.example/x.opus"/></item>
  <item><title>Same guid again</title><guid>ep-1</guid><enclosure url="https://cdn.example/dup.mp3" type="audio/mpeg"/></item>
</channel></rss>"""


def test_a_feed_gives_the_podcast_and_its_audio_episodes_newest_first() -> None:
    feed = parse_feed(FEED.encode())
    assert (feed.title, feed.author, feed.site_url, feed.art_url) == ("The & Show", "Jo Host", "https://show.example/", "https://show.example/art.jpg")
    assert feed.description == "Weekly talk."
    assert [e.guid for e in feed.episodes] == ["ep-2", "ep-1", "typeless"], "audio only, no repeats, newest first (undated ones last)"
    two, one, typeless = feed.episodes
    assert two.title == "Episode 2" and two.audio_url == "https://cdn.example/ep2.mp3?x=1" and two.audio_type == "audio/mpeg" and two.audio_bytes == 12345678
    assert two.duration_seconds == 3723 and two.published_at == "2025-06-03T10:00:00+00:00" and (two.season, two.episode) == (2, 2)
    assert two.art_url == "https://show.example/ep2.jpg"
    assert one.published_at == "2025-05-27T15:00:00+00:00" and one.duration_seconds == 2110 and one.audio_bytes is None
    assert typeless.audio_type is None and typeless.title == "Typeless"


def test_notes_become_plain_text_with_paragraphs_and_no_scripts() -> None:
    notes = parse_feed(FEED.encode()).episodes[0].description
    assert notes == "The long notes.\nSecond paragraph."
    assert "<" not in notes and "alert" not in notes


def test_a_feed_with_entities_or_external_files_is_refused() -> None:
    bomb = b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><rss><channel><title>&b;</title></channel></rss>'
    external = b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><title>&x;</title></channel></rss>'
    for hostile in (bomb, external):
        with pytest.raises(FeedError, match="not accepted"):
            parse_feed(hostile)


@pytest.mark.parametrize(
    "content, message",
    [
        (b"", "not valid XML"),
        (b"<html><body>Not a feed</body></html>", "no RSS channel"),
        (b"<rss><channel><item><title>x</title></item></channel></rss>", "no title"),
        (b"<rss><channel><title>t</title>", "not valid XML"),
        (b"\xff\xfe garbage", "not valid XML"),
        (b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title></feed>', "no RSS channel"),
    ],
)
def test_text_that_is_not_a_feed_says_so(content, message) -> None:
    with pytest.raises(FeedError, match=message):
        parse_feed(content)


def test_a_feed_with_no_episodes_is_still_a_feed() -> None:
    feed = parse_feed(b"<rss><channel><title>Empty</title></channel></rss>")
    assert feed.title == "Empty" and feed.episodes == []


def test_the_feed_is_read_whatever_its_namespaces_are_called() -> None:
    feed = parse_feed(
        b'<rss xmlns:it="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>T</title><it:author>A</it:author>'
        b'<item><title>E</title><enclosure url="http://h.example/e.mp3" type="audio/mpeg"/><it:duration>10:00</it:duration></item></channel></rss>'
    )
    assert feed.author == "A" and feed.episodes[0].duration_seconds == 600


def test_media_content_stands_in_for_a_missing_enclosure() -> None:
    feed = parse_feed(
        b'<rss xmlns:media="http://search.yahoo.com/mrss/"><channel><title>T</title><item><title>E</title><guid>g</guid>'
        b'<media:content url="http://h.example/e.mp3" type="audio/mpeg" fileSize="4096"/></item></channel></rss>'
    )
    assert feed.episodes[0].audio_url == "http://h.example/e.mp3" and feed.episodes[0].audio_bytes == 4096


def test_an_episode_without_a_guid_is_known_by_its_audio_address() -> None:
    feed = parse_feed(
        b"<rss><channel><title>T</title>"
        b"<item><title>A</title><enclosure url='https://cdn.example/a.mp3' type='audio/mpeg'/></item>"
        b"<item><title>B</title><enclosure url='https://cdn.example/b.mp3' type='audio/mpeg'/></item></channel></rss>"
    )
    assert sorted(e.guid for e in feed.episodes) == ["https://cdn.example/a.mp3", "https://cdn.example/b.mp3"]


def test_no_more_than_the_cap_of_episodes_is_kept() -> None:
    items = "".join(f"<item><title>E{n}</title><guid>g{n}</guid><enclosure url='http://h.example/{n}.mp3' type='audio/mpeg'/></item>" for n in range(1100))
    assert len(parse_feed(f"<rss><channel><title>Big</title>{items}</channel></rss>".encode()).episodes) == 1000


@pytest.mark.parametrize(
    "value, expected",
    [("1:02:03", 3723), ("35:10", 2110), ("2110", 2110), ("2110.5", 2110.5), ("00:00:30", 30), (" 90 ", 90), ("0", None), ("-5", None), ("", None), (None, None),
     ("soon", None), ("1:2:3:4", None), ("1:xx", None), ("999999", None), ("1:-5", None)],
)
def test_durations_in_every_form_podcasts_use(value, expected) -> None:
    assert parse_duration(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Tue, 03 Jun 2025 10:00:00 +0000", "2025-06-03T10:00:00+00:00"),
        ("Tue, 03 Jun 2025 10:00:00 GMT", "2025-06-03T10:00:00+00:00"),
        ("Tue, 03 Jun 2025 10:00:00 -0500", "2025-06-03T15:00:00+00:00"),
        ("2025-06-03T10:00:00Z".replace("Z", "+00:00"), "2025-06-03T10:00:00+00:00"),
        ("2025-06-03T10:00:00", "2025-06-03T10:00:00+00:00"),
        ("last Tuesday", None), ("", None), (None, None), ("Tue, 99 Jun 2025 10:00:00 +0000", None),
    ],
)
def test_dates_are_turned_into_utc(value, expected) -> None:
    assert parse_date(value) == expected


def test_plain_text_reduces_markup() -> None:
    assert plain_text("<p>One &amp; two</p><p>Three<br>four</p><style>x{}</style>") == "One & two\nThree\nfour"
    assert plain_text("  a   b \n\n\n c ") == "a b\nc"
    assert plain_text(None) == "" and plain_text("") == "" and plain_text("<br><p></p>") == ""
    assert len(plain_text("x" * 9000)) == 4000
