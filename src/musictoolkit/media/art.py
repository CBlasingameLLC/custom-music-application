"""Cover art: embedded picture first, then a cover/folder image beside the file.

Thumbnails are cached on disk, keyed by the source file's identity (path,
mtime, size) so editing a file's art naturally invalidates its thumbnails.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp4 import MP4

FOLDER_ART_STEMS = ("cover", "folder", "front", "album", "albumart", "art")
FOLDER_ART_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
THUMBNAIL_SIZES = (64, 128, 256, 512, 1024)


def sniff_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"GIF":
        return "image/gif"
    return "application/octet-stream"


def _from_id3(path: Path) -> bytes | None:
    try:
        frames = ID3(path).getall("APIC")
    except ID3NoHeaderError:
        return None
    if not frames:
        return None
    front = next((f for f in frames if f.type == 3), frames[0])
    return bytes(front.data)


def _from_flac(path: Path) -> bytes | None:
    pictures = FLAC(path).pictures
    if not pictures:
        return None
    front = next((p for p in pictures if p.type == 3), pictures[0])
    return bytes(front.data)


def _from_mp4(path: Path) -> bytes | None:
    covers = (MP4(path).tags or {}).get("covr")
    return bytes(covers[0]) if covers else None


def _from_ogg(path: Path) -> bytes | None:
    audio = mutagen.File(path)
    blocks = (audio.get("metadata_block_picture") if audio is not None else None) or []
    for block in blocks:
        picture = Picture(base64.b64decode(block))
        if picture.data:
            return bytes(picture.data)
    return None


_EMBEDDED_READERS = {
    ".mp3": _from_id3,
    ".flac": _from_flac,
    ".m4a": _from_mp4,
    ".mp4": _from_mp4,
    ".m4b": _from_mp4,
    ".ogg": _from_ogg,
    ".oga": _from_ogg,
    ".opus": _from_ogg,
}


def embedded_art(path: Path) -> bytes | None:
    reader = _EMBEDDED_READERS.get(path.suffix.lower())
    if reader is None:
        return None
    try:
        return reader(path)
    except Exception:  # a corrupt tag must never break a page of albums
        return None


def folder_art(path: Path) -> Path | None:
    try:
        candidates = {
            entry.name.lower(): entry.path
            for entry in os.scandir(path.parent)
            if entry.is_file() and os.path.splitext(entry.name)[1].lower() in FOLDER_ART_EXTS
        }
    except OSError:
        return None
    for stem in FOLDER_ART_STEMS:
        for ext in sorted(FOLDER_ART_EXTS):
            hit = candidates.get(stem + ext)
            if hit:
                return Path(hit)
    return None


def find_art(path: Path) -> bytes | None:
    data = embedded_art(path)
    if data:
        return data
    sidecar = folder_art(path)
    if sidecar is None:
        return None
    try:
        return sidecar.read_bytes()
    except OSError:
        return None


def thumbnail(data: bytes, size: int) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        img.load()
        rgb = img.convert("RGB")
    rgb.thumbnail((size, size), Image.LANCZOS)
    out = io.BytesIO()
    rgb.save(out, "JPEG", quality=86, optimize=True)
    return out.getvalue()


def cached_art(path: Path, size: int, cache_dir: Path) -> bytes | None:
    """JPEG thumbnail of the track's cover at `size` px, or None if it has no art."""
    if size not in THUMBNAIL_SIZES:
        size = 256
    try:
        stat = path.stat()
    except OSError:
        return None
    key = hashlib.sha1(f"{path}|{stat.st_mtime_ns}|{stat.st_size}|{size}".encode("utf-8")).hexdigest()
    target = cache_dir / key[:2] / f"{key}.jpg"
    missing_marker = target.with_suffix(".none")
    if target.exists():
        return target.read_bytes()
    if missing_marker.exists():
        return None

    data = find_art(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        missing_marker.touch()
        return None
    try:
        out = thumbnail(data, size)
    except Exception:  # unreadable image data counts as "no art"
        missing_marker.touch()
        return None
    target.write_bytes(out)
    return out
