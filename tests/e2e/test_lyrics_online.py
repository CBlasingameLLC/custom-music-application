"""Lyrics from lrclib.net in the browser: asked for with a button or by a setting, kept, credited, and never forced on."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from musictoolkit.media import lrclib
from tests.e2e.conftest import add_library, api, play
from tests.fake_services import serve

expect.set_options(timeout=15_000)

HIT = {"id": 7, "trackName": "Wires", "artistName": "Mara Quinn", "albumName": "Static Hearts", "duration": 12.0,
       "instrumental": False, "plainLyrics": "Sparks\nand wire", "syncedLyrics": "[00:00.50]Sparks fly\n[00:03.00]and wire hums\n"}


@pytest.fixture
def lrclib_service(monkeypatch):
    lrclib._trouble.clear()
    with serve({"/get": (200, HIT)}) as service:
        monkeypatch.setattr(lrclib, "BASE_URL", service.url)
        yield service
    lrclib._trouble.clear()


def open_lyrics(page, title: str) -> None:
    page.evaluate("location.hash = '#/songs'")
    page.wait_for_selector(".trow:not(.skeleton)")
    play(page, title)
    page.evaluate("location.hash = '#/now'")
    expect(page.get_by_role("tab", name="Lyrics")).to_be_visible()


def test_a_song_without_lyrics_offers_a_lookup_and_keeps_what_it_finds(page, live, lrclib_service):
    add_library(page, live)
    open_lyrics(page, "Wires")

    expect(page.get_by_role("heading", name="No lyrics for this song")).to_be_visible()
    assert lrclib_service.requests == [], "nothing is asked until the button is pressed"

    page.get_by_role("button", name="Look up lyrics online").click()

    expect(page.locator(".lyrics.synced p")).to_have_count(2)
    expect(page.locator(".lyrics.synced")).to_contain_text("and wire hums")
    expect(page.locator(".lyrics-credit")).to_contain_text("Lyrics from LRCLIB")
    assert lrclib_service.count("/get") == 1

    page.evaluate("location.hash = '#/songs'")
    page.evaluate("location.hash = '#/now'")
    expect(page.locator(".lyrics.synced p")).to_have_count(2)
    assert lrclib_service.count("/get") == 1, "kept: the second look at the same song asks nobody"


def test_turning_the_setting_on_finds_lyrics_without_a_button(page, live, lrclib_service):
    add_library(page, live)
    page.evaluate("location.hash = '#/settings'")
    page.get_by_label("Look up lyrics on lrclib.net when a song has none").check()
    expect(page.get_by_text("Saved")).to_be_visible()
    assert api(page, "/settings")["app"]["lyrics_lrclib"] is True

    open_lyrics(page, "Wires")

    expect(page.locator(".lyrics.synced p")).to_have_count(2)
    expect(page.locator(".lyrics-credit")).to_be_visible()


def test_lyrics_in_the_file_come_first_and_ask_nobody(page, live, lrclib_service):
    add_library(page, live)
    api(page, "/settings", "PUT", {"app": {"lyrics_lrclib": True}})

    open_lyrics(page, "Glacier")  # the test library gives this one a .lrc file

    expect(page.locator(".lyrics.synced")).to_contain_text("Frozen in the dark")
    expect(page.locator(".lyrics-credit")).to_have_count(0)
    assert lrclib_service.requests == []


def test_a_service_in_trouble_is_explained_and_can_be_tried_again(page, live, lrclib_service):
    add_library(page, live)
    lrclib_service.routes["/get"] = (503, {"message": "busy"})
    open_lyrics(page, "Wires")

    page.get_by_role("button", name="Look up lyrics online").click()

    expect(page.get_by_role("heading", name="Could not reach LRCLIB")).to_be_visible()
    expect(page.get_by_text("LRCLIB is busy right now")).to_be_visible()
    lrclib_service.routes["/get"] = (200, HIT)
    page.get_by_role("button", name="Try again").click()
    expect(page.locator(".lyrics.synced p")).to_have_count(2)


def test_a_song_lrclib_does_not_have_says_so(page, live, lrclib_service):
    add_library(page, live)
    lrclib_service.routes["/get"] = (404, None)
    lrclib_service.routes["/search"] = (200, [])
    open_lyrics(page, "Wires")

    page.get_by_role("button", name="Look up lyrics online").click()

    expect(page.get_by_text("LRCLIB does not have this one either.")).to_be_visible()
    lrclib_service.routes["/get"] = (200, HIT)
    page.get_by_role("button", name="Ask again").click()
    expect(page.locator(".lyrics.synced p")).to_have_count(2)


def test_an_instrumental_says_so(page, live, lrclib_service):
    add_library(page, live)
    lrclib_service.routes["/get"] = (200, {"id": 3, "instrumental": True, "plainLyrics": None, "syncedLyrics": None})
    open_lyrics(page, "Wires")

    page.get_by_role("button", name="Look up lyrics online").click()

    expect(page.get_by_role("heading", name="Instrumental")).to_be_visible()
