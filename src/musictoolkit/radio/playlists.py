"""Radio addresses that are really a small playlist file (.pls, .m3u, .asx) pointing at the stream.

Directories and people often give the address of the playlist a station publishes instead of the stream itself. The first address in it is
the stream. HLS (an .m3u8 made of segments, "#EXT-X-..." lines) is a different thing - a browser engine without HLS support cannot play it -
so it is recognised and reported rather than mistaken for a playlist of streams.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from musictoolkit.net.guard import UnsafeURL, check_url

PLAYLIST_TYPES = {
    "audio/x-scpls", "application/pls+xml", "audio/scpls", "audio/x-mpegurl", "audio/mpegurl", "application/x-mpegurl", "application/mpegurl",
    "application/vnd.apple.mpegurl", "video/x-ms-asf", "video/x-ms-wax", "application/x-winamp-playlist", "audio/x-ms-wax",
}
PLAYLIST_SUFFIXES = (".pls", ".m3u", ".m3u8", ".asx", ".wax")
MAX_PLAYLIST_BYTES = 64 * 1024


class UnplayableStream(Exception):
    """The address is a stream (or playlist) this player cannot play; the message says why."""


def looks_like_a_playlist(content_type: str, url: str) -> bool:
    kind = content_type.split(";")[0].strip().lower()
    return kind in PLAYLIST_TYPES or urlsplit(url).path.lower().endswith(PLAYLIST_SUFFIXES)


def is_hls(content: bytes) -> bool:
    return b"#EXT-X-" in content[:MAX_PLAYLIST_BYTES]


def first_stream(content: bytes, base_url: str) -> str:
    """The first usable stream address in a playlist file (relative ones are taken from `base_url`)."""
    if is_hls(content):
        raise UnplayableStream("This station is an HLS stream, which this player cannot play.")
    text = content[:MAX_PLAYLIST_BYTES].decode("utf-8", errors="replace")
    for candidate in _candidates(text):
        try:
            return check_url(urljoin(base_url, candidate))
        except UnsafeURL:
            continue
    raise UnplayableStream("The playlist does not hold an address this player can use.")


def _candidates(text: str) -> list[str]:
    found: list[str] = []
    if re.search(r"<\s*asx\b", text, re.I):  # Windows Media: <ref href="...">
        found += re.findall(r"<\s*ref\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"']", text, re.I)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        pls = re.match(r"file\d*\s*=\s*(\S.*)$", line, re.I)  # .pls: File1=http://...
        if pls:
            found.append(pls.group(1).strip())
        elif "=" not in line.split("://", 1)[0] and "<" not in line:  # .m3u: one address per line
            found.append(line)
    return found
