"""Streaming quality in a real browser: the choice in Settings (kept by the device, and the note about what the server can do), and that a song is asked for at the
quality chosen and plays, converted on the way by a real ffmpeg (those tests are skipped where there is none)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from playwright.sync_api import expect

from musictoolkit.transcode import Transcoder
from tests.e2e.conftest import add_library, row, signed_in_page, wait_until
from tests.e2e.support import serve_app, stop_app  # noqa: F401 (a pair that other tests of this folder use)

expect.set_options(timeout=10_000)
STUB = [sys.executable, str(Path(__file__).parent.parent / "fake_ffmpeg.py")]
REAL = Transcoder().available()


def settings(page):
    page.evaluate("location.hash = '#/settings'")
    card = page.locator(".card-panel", has=page.get_by_role("heading", name="Streaming quality"))
    expect(card).to_be_visible()
    return card


def kept(page) -> dict:
    return json.loads(page.evaluate("localStorage.getItem('mtk.ui.v1')"))


def test_settings_offers_the_qualities_and_the_device_keeps_the_choice(page, live) -> None:
    card = settings(page)
    select = card.get_by_label("Quality on this device")
    options = select.locator("option")
    assert options.all_inner_texts() == ["Original (as stored)", "320 kbps", "192 kbps", "128 kbps", "96 kbps", "64 kbps", "32 kbps (smallest)"]
    assert select.input_value() == "0" and kept(page).get("streamKbps") in (0, None)
    select.select_option("128")
    expect(page.locator(".toast")).to_contain_text("From the next song, songs are streamed at 128 kbps or less.")
    assert kept(page)["streamKbps"] == 128
    page.reload()
    page.wait_for_selector("#main")
    assert settings(page).get_by_label("Quality on this device").input_value() == "128"
    settings(page).get_by_label("Quality on this device").select_option("0")
    expect(page.locator(".toast").last).to_contain_text("From the next song, songs are streamed as they are stored.")
    assert kept(page)["streamKbps"] == 0


def test_a_value_that_is_not_a_quality_is_not_kept(page, live) -> None:
    page.evaluate("localStorage.setItem('mtk.ui.v1', JSON.stringify({ streamKbps: 77, theme: 'dark' }))")
    page.reload()
    page.wait_for_selector("#main")
    assert settings(page).get_by_label("Quality on this device").input_value() == "0"


def test_the_card_says_what_the_server_can_do(page, live, tmp_path: Path) -> None:
    live.ctx.transcoder = Transcoder(command=STUB)
    card = settings(page)
    expect(card).to_contain_text("The server can convert songs (ffmpeg stub-1.0).")
    assert card.locator(".note.warn").count() == 0
    live.ctx.transcoder = Transcoder(ffmpeg=str(tmp_path / "no-ffmpeg"))
    page.reload()
    page.wait_for_selector("#main")
    expect(settings(page)).to_contain_text("This server cannot convert songs (ffmpeg was not found (on Linux: sudo apt install ffmpeg)), so every song is sent as it is stored, whatever is chosen here.")


def test_the_desktop_window_has_no_use_for_it(browser, live) -> None:
    pages = signed_in_page(browser, live, init_script="window.mtk = { version: 'test' };")
    page = next(pages)
    page.evaluate("location.hash = '#/settings'")
    page.wait_for_selector(".settings")
    assert page.get_by_role("heading", name="Streaming quality").count() == 0
    next(pages, None)


STATE = "import('/static/js/player.js').then((m) => m.deckState())"


def playing(page, address: str, seconds: float = 0.5) -> None:
    """Until a deck of the player has been told to load this address and has played this far (what the browser decodes is what the server made)."""
    wait_until(page, f"{STATE}.then((s) => s.decks.some((d) => d.src && {address} && d.time > {seconds}))", 25)


@pytest.mark.skipif(not REAL, reason="no ffmpeg with an MP3 encoder (sudo apt install ffmpeg)")
def test_a_song_is_asked_for_at_the_quality_chosen_and_plays_converted_by_ffmpeg(page, live) -> None:
    add_library(page, live)
    settings(page).get_by_label("Quality on this device").select_option("32")  # (the songs of the test library are about 48 kbps)
    page.evaluate("location.hash = '#/songs'")
    page.wait_for_selector(".trow:not(.skeleton)")
    row(page, "Glacier").locator(".t1").dblclick()
    playing(page, "d.src.endsWith('/stream?kbps=32')")
    cache = live.ctx.data_dir / "cache" / "transcode"
    for _ in range(100):  # (the file is kept a moment after the song is whole)
        if list(cache.glob("*-32.mp3")):
            break
        page.wait_for_timeout(100)
    [made] = cache.glob("*-32.mp3")
    assert made.stat().st_size > 10_000 and made.read_bytes()[:3] != b"ID3"


@pytest.mark.skipif(not REAL, reason="no ffmpeg with an MP3 encoder (sudo apt install ffmpeg)")
def test_with_the_original_chosen_a_song_is_asked_for_as_it_is(page, live) -> None:
    add_library(page, live)
    row(page, "Glacier").locator(".t1").dblclick()
    playing(page, "d.src.endsWith('/stream')")
    assert not (live.ctx.data_dir / "cache" / "transcode").exists() or not list((live.ctx.data_dir / "cache" / "transcode").glob("*.mp3"))
