"""The front cover of an album, from the Cover Art Archive (coverartarchive.org, which sends on to archive.org).

Fetched through the same checks as everything else that comes from outside (public addresses only, every redirect looked at), then re-drawn
as a JPEG of a sensible size: what the server sent is never passed on as it is - not into a tag, not next to the music.
"""

from __future__ import annotations

import io
import logging
import os
import re

from PIL import Image, UnidentifiedImageError

from musictoolkit.net import guard

logger = logging.getLogger("musictoolkit")

BASE = "https://coverartarchive.org"
MAX_BYTES = 8_000_000
MAX_PIXELS = 40_000_000
MAX_SIDE = 1400
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _base() -> str:
    return (os.environ.get("MTK_COVERART_URL") or BASE).rstrip("/")  # the variable is for tests and the packaged-app check


def as_jpeg(data: bytes) -> bytes | None:
    """`data` re-drawn as a JPEG of at most MAX_SIDE pixels a side, or None if it is not a picture this app will take."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("JPEG", "PNG", "WEBP", "GIF") or image.width * image.height > MAX_PIXELS:
                return None
            image.thumbnail((MAX_SIDE, MAX_SIDE))
            rgba = image.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            out = io.BytesIO()
            flat.save(out, "JPEG", quality=92)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


def front_cover(release_id: str | None, release_group_id: str | None = None) -> bytes | None:
    """A JPEG of the album's front cover, or None when it has none or it cannot be had. The release's own cover is tried first, then the
    one MusicBrainz shows for the album as a whole. The ids come from MusicBrainz and are only used if they look like ids."""
    for kind, ident in (("release", release_id), ("release-group", release_group_id)):
        if not ident or not _UUID.match(ident):
            continue
        try:
            got = guard.fetch(f"{_base()}/{kind}/{ident}/front-1200", max_bytes=MAX_BYTES, deadline=30.0, headers={"Accept": "image/jpeg, image/png"})
        except guard.FetchError as exc:
            logger.debug("No cover from %s %s: %s", kind, ident, exc)
            continue
        picture = as_jpeg(got.content)
        if picture is not None:
            return picture
    return None
