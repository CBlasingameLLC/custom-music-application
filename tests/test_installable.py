"""The app as something a phone or a Mac installs: its manifest, its icons, the tags in its pages, and the service worker's file."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from musictoolkit import __version__
from musictoolkit.web.app import STATIC_DIR
from musictoolkit.web.signin import sign_in_page
from tests.conftest import build_web

TOKEN = "k" * 8 + "T0ken" + "z" * 30


def _manifest() -> dict:
    return json.loads((STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))


def _head_of(page: str) -> str:
    return page.split("</head>")[0]


def _tags(page: str, name: str) -> list[dict[str, str]]:
    """The attributes of each <name ...> tag in a page's head."""
    return [dict(re.findall(r'([\w-]+)="([^"]*)"', tag)) for tag in re.findall(rf"<{name}\b[^>]*>", _head_of(page))]


def _meta(page: str, name: str) -> str | None:
    found = [tag.get("content") for tag in _tags(page, "meta") if tag.get("name") == name]
    return found[0] if found else None


# ---------------------------------------------------------------------------------------------------- the manifest


def test_the_manifest_says_what_an_installed_app_is() -> None:
    manifest = _manifest()
    assert manifest["name"] == "Music Toolkit" and manifest["short_name"] == "Music Kit"
    assert len(manifest["short_name"]) <= 12  # (what a phone prints under the icon)
    assert manifest["display"] == "standalone"  # (no browser around it)
    assert manifest["start_url"] == manifest["scope"] == manifest["id"] == "/"
    assert manifest["theme_color"] == manifest["background_color"] == "#0d0f14"  # (the app's own dark background: no flash of white while it starts)


def test_every_icon_in_the_manifest_is_there_and_is_the_size_it_says() -> None:
    icons = _manifest()["icons"]
    assert {(icon["sizes"], icon["purpose"]) for icon in icons} >= {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")}
    for icon in icons:
        assert icon["src"].startswith("/static/icons/") and icon["type"] == "image/png"
        file = STATIC_DIR / icon["src"].removeprefix("/static/")
        assert file.is_file(), icon["src"]
        with Image.open(file) as picture:
            assert f"{picture.width}x{picture.height}" == icon["sizes"] and picture.format == "PNG", icon["src"]


def test_the_maskable_icon_has_no_clear_corners_and_the_round_ones_do() -> None:
    """A phone cuts its own shape out of a maskable icon (clear corners would show as black); the rounded ones carry their own."""
    icons = STATIC_DIR / "icons"
    with Image.open(icons / "icon-maskable-512.png") as maskable, Image.open(icons / "icon-512.png") as round_one, Image.open(icons / "apple-touch-icon.png") as apple:
        assert maskable.mode == "RGB" and apple.mode == "RGB"  # (no alpha at all: iOS paints clear pixels black)
        assert round_one.mode == "RGBA" and round_one.getpixel((0, 0))[3] == 0 and round_one.getpixel((256, 256))[3] == 255
        assert apple.size == (180, 180)


def test_the_manifest_and_the_icons_are_sent_to_a_browser_that_has_no_cookie_yet(tmp_path: Path) -> None:
    """A browser fetches the manifest without credentials: with a token set, it still has to get it (and the icons)."""
    web = build_web(tmp_path, None, token=TOKEN)
    stranger = TestClient(web.app)
    manifest = stranger.get("/static/manifest.webmanifest")
    assert manifest.status_code == 200 and manifest.headers["content-type"].startswith("application/manifest+json")
    assert manifest.json() == _manifest() and manifest.headers["cache-control"] == "no-cache"
    for icon in _manifest()["icons"]:
        assert stranger.get(icon["src"]).status_code == 200
    assert stranger.get("/static/icons/apple-touch-icon.png").headers["content-type"] == "image/png"
    assert stranger.get("/api/about").status_code == 401  # (the open part is the app's own files and nothing more)


# ---------------------------------------------------------------------------------------------------- the tags in the pages


def test_the_app_page_has_the_tags_that_make_it_an_app() -> None:
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert [tag["href"] for tag in _tags(page, "link") if tag.get("rel") == "manifest"] == ["/static/manifest.webmanifest"]
    apple = [tag["href"] for tag in _tags(page, "link") if tag.get("rel") == "apple-touch-icon"]
    assert apple == ["/static/icons/apple-touch-icon.png"] and (STATIC_DIR / "icons" / "apple-touch-icon.png").is_file()
    assert "viewport-fit=cover" in _meta(page, "viewport") and "width=device-width" in _meta(page, "viewport")  # (drawn under the notch: the page pads for it)
    assert _meta(page, "theme-color") == _manifest()["theme_color"]
    assert _meta(page, "apple-mobile-web-app-capable") == "yes" and _meta(page, "mobile-web-app-capable") == "yes"
    assert _meta(page, "apple-mobile-web-app-title") == _manifest()["short_name"]
    assert _meta(page, "apple-mobile-web-app-status-bar-style") == "black"  # (not black-translucent: the light theme would put white clock digits on a pale page)


def test_the_sign_in_page_can_be_installed_too() -> None:
    """The first thing an installed app shows, before the cookie is there, is this page: it carries the same tags."""
    page = sign_in_page()
    assert [tag["href"] for tag in _tags(page, "link") if tag.get("rel") == "manifest"] == ["/static/manifest.webmanifest"]
    assert [tag["href"] for tag in _tags(page, "link") if tag.get("rel") == "apple-touch-icon"] == ["/static/icons/apple-touch-icon.png"]
    assert "viewport-fit=cover" in _meta(page, "viewport") and _meta(page, "theme-color") == "#0d0f14"
    assert _meta(page, "apple-mobile-web-app-capable") == "yes"
    assert "<script" not in page  # (it still runs no script)


# ---------------------------------------------------------------------------------------------------- the service worker's file


def test_the_service_worker_comes_from_the_root_with_the_version_of_the_app_and_asks_for_no_token(tmp_path: Path) -> None:
    web = build_web(tmp_path, None, token=TOKEN)
    stranger = TestClient(web.app)
    got = stranger.get("/sw.js")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("text/javascript")
    assert got.headers["service-worker-allowed"] == "/"  # (in charge of the whole app, not only of /static/)
    assert got.headers["cache-control"] == "no-cache"  # (a browser looks for a new one every time, and a new version of the app is a new file)
    assert f"const VERSION = '{__version__}';" in got.text and "__VERSION__" not in got.text
    assert "content-security-policy" in got.headers
    head = stranger.head("/sw.js")
    assert head.status_code == 200 and head.headers["content-length"] == got.headers["content-length"] and head.content == b""
    assert stranger.get("/sw.js", headers={"host": "evil.example"}).status_code == 403  # (the same Host rule as the rest)


def test_the_file_of_the_service_worker_is_the_source_with_only_the_version_filled_in(tmp_path: Path) -> None:
    web = build_web(tmp_path, None)
    source = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    assert source.count("__VERSION__") == 1
    assert web.client.get("/sw.js").text == source.replace("__VERSION__", __version__)


def test_the_page_registers_the_worker_from_the_root_and_not_in_the_desktop_window() -> None:
    """The page asks for /sw.js (in charge of the whole app) and not for the copy under /static/, which would be in charge of /static/ only;
    the desktop window has no use for it: its server is on a port of its own each time."""
    main = (STATIC_DIR / "js" / "main.js").read_text(encoding="utf-8")
    assert "register('/sw.js', { scope: '/' })" in main
    assert "'serviceWorker' in navigator && !window.mtk" in main
