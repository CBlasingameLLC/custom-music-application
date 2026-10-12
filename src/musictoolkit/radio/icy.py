"""Internet radio's in-band "what is playing" (ICY metadata).

A client that sends `Icy-MetaData: 1` is answered with an `icy-metaint: N` header and a body in which, after every N bytes of audio, comes
one length byte L followed by L*16 bytes of text such as `StreamTitle='Artist - Title';StreamUrl='';` (padded with zeros; L may be 0).
Audio players must not hear those bytes. `IcyDemuxer` takes the body in whatever pieces it arrives in and gives back the audio alone,
remembering the latest title.
"""

from __future__ import annotations

import re

MAX_TITLE = 300  # characters kept of a title: a station that sends a novel is not to fill the screen

_TITLE = re.compile(r"StreamTitle='(.*?)'\s*(?:;|$)", re.S)


class IcyDemuxer:
    def __init__(self, metaint: int) -> None:
        if metaint <= 0:
            raise ValueError("metaint must be positive")
        self.metaint = metaint
        self.title: str | None = None  # the latest StreamTitle, None until one has come (or when the station sends an empty one)
        self.titles_seen = 0
        self._audio_left = metaint
        self._meta_left = 0
        self._in_meta = False
        self._meta = bytearray()

    def feed(self, data: bytes) -> bytes:
        """The audio in `data` (which may start or end anywhere, even in the middle of a metadata block)."""
        out = bytearray()
        i, n = 0, len(data)
        while i < n:
            if self._in_meta:
                take = min(self._meta_left, n - i)
                self._meta += data[i : i + take]
                i += take
                self._meta_left -= take
                if self._meta_left == 0:
                    self._finish()
                    self._in_meta = False
                    self._audio_left = self.metaint
            elif self._audio_left == 0:
                length = data[i] * 16
                i += 1
                if length:
                    self._in_meta, self._meta_left, self._meta = True, length, bytearray()
                else:
                    self._audio_left = self.metaint
            else:
                take = min(self._audio_left, n - i)
                out += data[i : i + take]
                i += take
                self._audio_left -= take
        return bytes(out)

    def _finish(self) -> None:
        raw = bytes(self._meta).rstrip(b"\0")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")  # older servers
        found = _TITLE.search(text)
        if found is None:
            return
        self.title = _clean(found.group(1)) or None
        self.titles_seen += 1


def _clean(text: str) -> str:
    return " ".join(text.replace("\0", " ").split())[:MAX_TITLE]


def split_title(title: str | None) -> tuple[str | None, str | None]:
    """("Artist", "Title") from "Artist - Title"; a title with no " - " in it is all title."""
    if not title:
        return None, None
    if " - " in title:
        artist, rest = title.split(" - ", 1)
        if artist.strip() and rest.strip():
            return artist.strip(), rest.strip()
    return None, title.strip()


def metaint_from(headers: dict[str, str]) -> int:
    """The interval from a response's headers, or 0 when the station is not sending metadata (or sends nonsense)."""
    value = next((v for k, v in headers.items() if k.lower() == "icy-metaint"), "")
    try:
        interval = int(str(value).strip())
    except ValueError:
        return 0
    return interval if 0 < interval <= 1_000_000 else 0
