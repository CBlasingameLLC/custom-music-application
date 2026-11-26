"""Draw the icons the installed web app uses (the same picture as the favicon in index.html).

    python scripts/make_pwa_icons.py

The four files go in src/musictoolkit/web/static/icons/ and are committed: this only has to run again when the picture changes.

  icon-192.png, icon-512.png   rounded corners on a clear background (the "any" icons of the manifest)
  icon-maskable-512.png        the picture on a full square: a phone cuts its own shape out of it (the note stays inside the
                               circle that is always kept, a fifth of the width in from every edge)
  apple-touch-icon.png         180 x 180, the full square with no clear corners: iOS fills clear pixels with black and rounds the corners itself
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "src" / "musictoolkit" / "web" / "static" / "icons"
SCALE = 8  # the note and the corners are drawn this many times bigger, then shrunk: smooth edges
TOP_LEFT = (0x8B, 0x7B, 0xFF)
BOTTOM_RIGHT = (0x5B, 0x9B, 0xFF)
RADIUS = 8 / 32  # of the width, as in the favicon


def gradient(size: int) -> Image.Image:
    """The diagonal blend from the top left to the bottom right corner."""
    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            pixels[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(TOP_LEFT, BOTTOM_RIGHT))
    return image


def note(draw: ImageDraw.ImageDraw, size: int) -> None:
    """The music note: a stem with a flag, and two heads, on the favicon's 32 x 32 grid."""
    unit = size / 32
    width = round(2 * unit)
    stem = [(13 * unit, 22 * unit), (13 * unit, 10 * unit), (21 * unit, 8 * unit), (21 * unit, 20 * unit)]
    draw.line(stem, fill="white", width=width, joint="curve")
    for x, y in stem:  # round ends
        draw.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill="white")
    for x, y in ((11, 22), (19, 20)):
        draw.ellipse([(x - 2.5) * unit, (y - 2.5) * unit, (x + 2.5) * unit, (y + 2.5) * unit], fill="white")


def draw(size: int, *, rounded: bool) -> Image.Image:
    """The icon at `size`: the blend at that size, and the note and the corners drawn bigger and shrunk, for smooth edges."""
    base = gradient(size).convert("RGBA")
    big = size * SCALE
    glyph = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    note(ImageDraw.Draw(glyph), big)
    base.alpha_composite(glyph.resize((size, size), Image.LANCZOS))
    if not rounded:
        return base.convert("RGB")
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, big - 1, big - 1], radius=round(big * RADIUS), fill=255)
    base.putalpha(mask.resize((size, size), Image.LANCZOS))
    return base


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size, rounded in (
        ("icon-192.png", 192, True),
        ("icon-512.png", 512, True),
        ("icon-maskable-512.png", 512, False),
        ("apple-touch-icon.png", 180, False),
    ):
        draw(size, rounded=rounded).save(OUT / name, optimize=True)
        print(f"wrote {OUT / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
