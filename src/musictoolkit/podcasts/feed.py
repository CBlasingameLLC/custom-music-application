"""Reading a podcast feed (RSS 2.0 with the iTunes tags most podcasts use).

A feed is text from a stranger's server, so it is read with defusedxml (no entities, no external files, no DTD tricks), every address in it
is checked before it is kept, and only the audio episodes are taken. Descriptions are reduced to plain text.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from musictoolkit.net.guard import UnsafeURL, check_url

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
MEDIA = "{http://search.yahoo.com/mrss/}"
MAX_EPISODES = 1000
MAX_TEXT = 4000
AUDIO_SUFFIXES = (".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac")
MAX_DURATION = 48 * 3600


class FeedError(Exception):
    """The text is not a podcast feed this app can read; the message says why in words a person can read."""


@dataclass
class ParsedEpisode:
    guid: str
    title: str
    audio_url: str
    description: str = ""
    published_at: str | None = None
    duration_seconds: float | None = None
    audio_type: str | None = None
    audio_bytes: int | None = None
    art_url: str | None = None
    season: int | None = None
    episode: int | None = None


@dataclass
class ParsedFeed:
    title: str
    author: str = ""
    description: str = ""
    site_url: str | None = None
    art_url: str | None = None
    episodes: list[ParsedEpisode] = field(default_factory=list)


class _Text(HTMLParser):
    BLOCKS = {"p", "br", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def plain_text(value: str | None, limit: int = MAX_TEXT) -> str:
    """Text out of a description that may hold HTML: tags gone, entities decoded, paragraphs kept, runs of blanks squeezed."""
    if not value:
        return ""
    parser = _Text()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # a parser that gives up on malformed markup must not lose the episode
        text = re.sub(r"<[^>]*>", " ", value)
    lines = [" ".join(line.split()) for line in html.unescape(text).replace("\r", "").split("\n")]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned[:limit]


def _text(parent: Element, *names: str) -> str:
    for name in names:
        found = parent.find(name)
        if found is not None and (found.text or "").strip():
            return found.text.strip()  # type: ignore[union-attr]
    return ""


def _link(value: str | None) -> str | None:
    try:
        return check_url(value) if value else None
    except UnsafeURL:
        return None


def parse_duration(value: str | None) -> float | None:
    """Seconds from "1:02:03", "35:10", "2110" or "2110.5"; None for anything else (or an absurd length)."""
    if not value:
        return None
    value = value.strip()
    try:
        if ":" in value:
            parts = [float(p) for p in value.split(":")]
            if not 2 <= len(parts) <= 3 or any(p < 0 for p in parts):
                return None
            seconds = 0.0
            for part in parts:
                seconds = seconds * 60 + part
        else:
            seconds = float(value)
    except ValueError:
        return None
    return seconds if 0 < seconds <= MAX_DURATION else None


def parse_date(value: str | None) -> str | None:
    """An ISO 8601 UTC time from an RSS date ("Tue, 03 Jun 2025 10:00:00 +0000") or an ISO one; None if it is neither."""
    if not value or not value.strip():
        return None
    for read in (parsedate_to_datetime, datetime.fromisoformat):
        try:
            moment = read(value.strip())
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
    return None


def _whole(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdigit() and int(value) < 100000 else None


def _enclosure(item: Element) -> tuple[str, str | None, int | None] | None:
    """(address, type, size) of the item's audio, from <enclosure> or (failing that) <media:content>; None if it has none."""
    for element in [*item.findall("enclosure"), *item.findall(f"{MEDIA}content")]:
        url = _link(element.get("url"))
        kind = (element.get("type") or "").split(";")[0].strip().lower() or None
        if not url:
            continue
        looks_like_audio = (kind or "").startswith("audio/") or kind in ("application/ogg",) or (kind is None and url.split("?")[0].lower().endswith(AUDIO_SUFFIXES))
        if not looks_like_audio:
            continue
        size = element.get("length") or element.get("fileSize") or ""
        return url, kind, int(size) if size.isdigit() and int(size) > 0 else None
    return None


def _image(element: Element) -> str | None:
    image = element.find(f"{ITUNES}image")
    if image is not None and _link(image.get("href")):
        return _link(image.get("href"))
    return _link(_text(element, "image/url"))


def parse_feed(content: bytes) -> ParsedFeed:
    """The podcast and its audio episodes (newest first) from the text of an RSS feed."""
    try:
        root = fromstring(content)
    except DefusedXmlException as exc:
        raise FeedError("The feed uses XML features that are not accepted (entities or external files).") from exc
    except (ParseError, ValueError) as exc:
        raise FeedError("The address does not hold a podcast feed (it is not valid XML).") from exc
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise FeedError("The address does not hold a podcast feed (no RSS channel in it).")
    title = plain_text(_text(channel, "title"), 300)
    if not title:
        raise FeedError("The feed has no title.")
    feed = ParsedFeed(
        title=title,
        author=plain_text(_text(channel, f"{ITUNES}author", "managingEditor"), 200),
        description=plain_text(_text(channel, f"{ITUNES}summary", "description")),
        site_url=_link(_text(channel, "link")),
        art_url=_image(channel),
    )
    seen: set[str] = set()
    for item in channel.findall("item"):
        audio = _enclosure(item)
        if audio is None:
            continue
        guid = _text(item, "guid") or audio[0]
        if guid in seen:
            continue
        seen.add(guid)
        episode_title = plain_text(_text(item, "title", f"{ITUNES}title"), 300) or "Untitled episode"
        feed.episodes.append(
            ParsedEpisode(
                guid=guid[:500],
                title=episode_title,
                audio_url=audio[0],
                audio_type=audio[1],
                audio_bytes=audio[2],
                description=plain_text(_text(item, f"{ITUNES}summary", f"{CONTENT}encoded", "description")),
                published_at=parse_date(_text(item, "pubDate", "{http://purl.org/dc/elements/1.1/}date")),
                duration_seconds=parse_duration(_text(item, f"{ITUNES}duration")),
                art_url=_image(item),
                season=_whole(_text(item, f"{ITUNES}season")),
                episode=_whole(_text(item, f"{ITUNES}episode")),
            )
        )
        if len(feed.episodes) >= MAX_EPISODES:
            break
    feed.episodes.sort(key=lambda e: e.published_at or "", reverse=True)
    return feed
