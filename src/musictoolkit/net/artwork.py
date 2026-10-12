"""Small pictures for stations and podcasts, fetched once and kept in the app's own folder.

The page never loads a picture from the internet itself (that would tell every picture host who is browsing, and from where). It asks this
app, which fetches the address it has on record - through the same checks as everything else - looks at what came back, re-draws it as a
small JPEG (so nothing the server sent, an SVG with a script in it for one, is ever handed on), and keeps that. A picture that could not be
had is not asked for again for a day.
"""

from __future__ import annotations

import hashlib
import io
import time
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from musictoolkit.net import guard

MAX_BYTES = 2_000_000
MAX_PIXELS = 25_000_000
KEEP_FOR = 30 * 86400
RETRY_AFTER = 86400


def _paths(cache_dir: Path, art_url: str, size: int) -> tuple[Path, Path]:
    name = hashlib.sha256(f"{size}\0{art_url}".encode()).hexdigest()[:40]
    return cache_dir / f"{name}.jpg", cache_dir / f"{name}.miss"


def cached_picture(art_url: str | None, cache_dir: Path, size: int = 256) -> bytes | None:
    """A JPEG of at most size x size pixels for the picture at `art_url`, or None if it cannot be had."""
    if not art_url:
        return None
    picture, miss = _paths(cache_dir, art_url, size)
    now = time.time()
    try:
        if picture.exists() and now - picture.stat().st_mtime < KEEP_FOR:
            return picture.read_bytes()
        if miss.exists() and now - miss.stat().st_mtime < RETRY_AFTER:
            return None
    except OSError:
        pass
    data = _fetch_and_redraw(art_url, size)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if data is None:
            miss.write_bytes(b"")
        else:
            picture.write_bytes(data)
            miss.unlink(missing_ok=True)
    except OSError:
        pass
    return data


def _fetch_and_redraw(art_url: str, size: int) -> bytes | None:
    try:
        got = guard.fetch(art_url, max_bytes=MAX_BYTES, deadline=15.0, headers={"Accept": "image/*"})
    except guard.FetchError:
        return None
    try:
        with Image.open(io.BytesIO(got.content)) as image:
            if image.format not in ("JPEG", "PNG", "GIF", "WEBP") or image.width * image.height > MAX_PIXELS:
                return None
            image.thumbnail((size, size))
            flat = Image.new("RGB", image.size, (255, 255, 255))
            rgba = image.convert("RGBA")
            flat.paste(rgba, mask=rgba.getchannel("A"))
            out = io.BytesIO()
            flat.save(out, "JPEG", quality=85)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
