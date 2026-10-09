"""The interface, driven like a person would: real clicks in a real browser, real audio element."""

from __future__ import annotations

import json
import re
import urllib.parse

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import add_library, api, play, row, wait_until

expect.set_options(timeout=10_000)

TITLES = ["Drift", "Glacier", "Gravel Dust", "Neon Rain", "Polar Night", "Porch Light", "Salt and Stone", "Signal Fade", "Wires"]


def player_bar(page):
    return page.locator(".playerbar")


def test_first_run_walks_a_new_user_to_a_playable_library(page, live):
    expect(page.get_by_text("Add your music")).to_be_visible()
    page.get_by_role("button", name="Add music folder").click()
    # A plain browser has no native folder picker, so the app asks for the path instead.
    page.get_by_label("Folder path").fill(str(live.library))
    page.get_by_role("button", name="Choose").click()

    expect(page.get_by_text("Library updated: 9 added")).to_be_visible(timeout=60_000)
    expect(page.locator(".shelf").first).to_be_visible()
    expect(page.locator(".sidebar")).to_contain_text("9 songs")
    page.get_by_role("link", name="Songs").click()
    expect(page.locator(".trow:not(.skeleton)")).to_have_count(9)


def test_songs_table_sorting_search_filters_and_smart_playlists(page, live):
    add_library(page, live)
    expect(page.locator(".trow:not(.skeleton)")).to_have_count(9)

    page.get_by_text("Title", exact=True).first.click()
    expect(page.locator(".trow").first).to_contain_text("Drift")
    page.get_by_text("Title", exact=True).first.click()
    expect(page.locator(".trow").first).to_contain_text("Wires")

    search = page.get_by_label("Search songs, artists, albums")
    search.fill("neon")
    expect(page.locator(".trow:not(.skeleton)")).to_have_count(1)
    expect(page.locator(".trow").first).to_contain_text("Neon Rain")
    search.fill("")

    page.get_by_role("button", name="Genre").click()
    page.get_by_label("Folk").check()
    page.mouse.click(700, 150)
    expect(page.locator(".trow:not(.skeleton)")).to_have_count(2)
    expect(page.get_by_text("2 matching songs")).to_be_visible()

    page.get_by_role("button", name="Save as smart playlist").click()
    page.get_by_placeholder("e.g. Highly rated folk").fill("Folk mix")
    expect(page.get_by_text("2 songs match right now")).to_be_visible()
    page.get_by_role("button", name="Create").click()
    expect(page.locator(".hero h1")).to_have_text("Folk mix")
    expect(page.locator(".rule-summary")).to_contain_text("Genre")
    expect(page.locator(".trow:not(.skeleton)")).to_have_count(2)
    expect(page.locator(".sidebar")).to_contain_text("Folk mix")


def test_playback_controls(page, live):
    add_library(page, live)
    play(page, "Glacier")
    expect(player_bar(page).locator(".pb-text .t1")).to_have_text("Glacier")
    expect(player_bar(page).get_by_role("button", name="Pause")).to_be_visible()
    expect(page.locator(".seek .time").first).not_to_have_text("0:00")  # the clock is running
    assert page.evaluate("document.title").startswith("▶ Glacier")

    player_bar(page).get_by_role("button", name="Pause").click()
    expect(player_bar(page).get_by_role("button", name="Play", exact=True)).to_be_visible()
    page.keyboard.press("Space")
    expect(player_bar(page).get_by_role("button", name="Pause")).to_be_visible()

    page.locator("input[aria-label=Seek]").evaluate(
        "el => { el.value = 8; el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }"
    )
    expect(page.locator(".seek .time").first).to_have_text(re.compile(r"^0:0[89]$"))

    player_bar(page).get_by_role("button", name="Next").click()
    expect(player_bar(page).locator(".pb-text .t1")).to_have_text("Polar Night")
    player_bar(page).get_by_role("button", name="Previous").click()
    expect(player_bar(page).locator(".pb-text .t1")).to_have_text("Glacier")

    page.keyboard.press("m")
    expect(player_bar(page).get_by_role("button", name="Unmute")).to_be_visible()
    page.keyboard.press("m")
    expect(player_bar(page).get_by_role("button", name="Mute")).to_be_visible()


def test_favorites_and_ratings(page, live):
    add_library(page, live)
    play(page, "Glacier")
    expect(player_bar(page).locator(".pb-text .t1")).to_have_text("Glacier")
    player_bar(page).locator(".icon-btn.heart").click()
    favorites = "/api/tracks?rules=" + urllib.parse.quote(json.dumps({"rules": [{"field": "favorite", "op": "is", "value": True}]}))
    wait_until(page, f"fetch({favorites!r}).then(r => r.json()).then(d => d.items.length === 1 && d.items[0].title === 'Glacier')")

    page.get_by_role("link", name="Favorites").click()
    expect(page.locator(".trow:not(.skeleton)")).to_have_count(1)

    page.evaluate("location.hash = '#/songs'")
    page.wait_for_selector(".trow:not(.skeleton)")
    row(page, "Wires").locator(".star").nth(3).click()  # fourth star
    wait_until(page, "fetch('/api/tracks?limit=100').then(r => r.json()).then(d => d.items.filter(t => t.rating === 4).length === 1)")
    rated = [t["title"] for t in api(page, "/tracks?limit=100")["items"] if t["rating"] == 4]
    assert rated == ["Wires"]


def test_queue_drawer_shuffle_and_repeat(page, live):
    add_library(page, live)
    play(page, "Glacier")
    expect(player_bar(page).get_by_role("button", name="Pause")).to_be_visible()

    player_bar(page).get_by_role("button", name="Queue", exact=True).click()
    expect(page.locator(".queue-drawer .qrow")).to_have_count(9)
    expect(page.locator(".qrow.current")).to_contain_text("Glacier")

    titles = page.locator(".queue-drawer .qrow .t1")
    before = titles.all_inner_texts()
    page.locator(".queue-drawer .qrow").nth(6).locator(".qtext").drag_to(page.locator(".queue-drawer .qrow").nth(3).locator(".qtext"))
    expect(titles.nth(3)).to_have_text(before[6])  # dragged up to where it was dropped
    expect(page.locator(".qrow.current")).to_contain_text("Glacier")  # and the current song is unaffected

    page.locator(".queue-drawer .qrow").nth(7).locator("button[aria-label='Remove from queue']").click()
    expect(page.locator(".queue-drawer .qrow")).to_have_count(8)
    player_bar(page).get_by_role("button", name="Queue", exact=True).click()
    expect(page.locator(".queue-drawer")).to_have_count(0)

    player_bar(page).get_by_role("button", name="Shuffle: off").click()
    expect(player_bar(page).get_by_role("button", name="Shuffle: on")).to_be_visible()
    player_bar(page).get_by_role("button", name="Repeat: off").click()
    expect(player_bar(page).get_by_role("button", name="Repeat: all")).to_be_visible()
    player_bar(page).get_by_role("button", name="Repeat: all").click()
    expect(player_bar(page).get_by_role("button", name="Repeat: one")).to_be_visible()


def test_now_playing_shows_synced_lyrics_and_an_equalizer(page, live):
    add_library(page, live)
    play(page, "Glacier")
    expect(player_bar(page).get_by_role("button", name="Pause")).to_be_visible()
    page.keyboard.press("n")
    expect(page.locator(".lyrics.synced p")).to_have_count(3)
    wait_until(page, "!!document.querySelector('.lyrics.synced p.active')", timeout=10)

    page.get_by_role("tab", name="Sound").click()
    page.get_by_label("Equalizer preset").select_option("Bass boost")
    expect(page.locator(".eq-band .db").first).to_have_text("+6")
    page.get_by_role("tab", name="Up next").click()
    expect(page.locator(".qrow").first).to_be_visible()


def test_a_listen_is_logged_once_it_counts(page, live):
    add_library(page, live)
    play(page, "Glacier")
    # A song counts as played after min(half its length, 4 min), at least 10 s of actual listening.
    wait_until(page, "fetch('/api/history/recent').then(r => r.json()).then(d => d.items.length > 0)", timeout=30)
    recent = api(page, "/history/recent")["items"]
    assert recent[0]["title"] == "Glacier"
    assert recent[0]["source"] == "future_scrobble"


def test_playlist_from_a_selection_can_be_reordered_and_trimmed(page, live):
    add_library(page, live)
    page.get_by_text("Title", exact=True).first.click()  # Drift, Glacier, Gravel Dust, ...
    expect(page.locator(".trow").first).to_contain_text("Drift")
    page.locator(".trow").nth(0).locator(".t1").click()
    page.locator(".trow").nth(2).locator(".t1").click(modifiers=["Shift"])
    expect(page.locator(".selection-bar")).to_contain_text("3 songs selected")

    page.get_by_role("button", name="Playlist").click()
    page.get_by_placeholder("New playlist name").fill("Test mix")
    page.get_by_role("button", name="Create").click()
    expect(page.get_by_text('Created "Test mix" with 3 songs')).to_be_visible()
    playlist = next(p for p in api(page, "/playlists")["items"] if p["name"] == "Test mix")
    assert playlist["tracks"] == 3

    page.evaluate(f"location.hash = '#/playlist/{playlist['id']}'")
    expect(page.locator(".trow:not(.skeleton)")).to_have_count(3)
    page.locator(".trow").nth(0).locator(".t1").drag_to(page.locator(".trow").nth(2).locator(".t1"))
    wait_until(page, f"fetch('/api/tracks?playlist={playlist['id']}').then(r => r.json()).then(d => d.items[2].title === 'Drift')")
    assert [t["title"] for t in api(page, f"/tracks?playlist={playlist['id']}")["items"]] == ["Glacier", "Gravel Dust", "Drift"]

    page.locator(".trow").nth(1).locator(".t1").click()
    page.keyboard.press("Delete")
    expect(page.locator(".trow:not(.skeleton)")).to_have_count(2)
    assert api(page, f"/playlists/{playlist['id']}")["tracks"] == 2


def test_context_menu_and_song_details(page, live):
    add_library(page, live)
    row(page, "Drift").locator(".t1").click(button="right")
    expect(page.locator(".menu")).to_be_visible()
    labels = page.locator(".menu-item").all_inner_texts()
    for wanted in ["Play", "Add to queue", "Add to playlist", "Go to album", "Song details"]:
        assert any(wanted in label for label in labels), f"{wanted!r} missing from {labels}"
    page.get_by_role("menuitem", name="Song details").click()
    expect(page.locator(".modal .info-grid").first).to_contain_text("Drift")
    page.keyboard.press("Escape")
    expect(page.locator(".modal")).to_have_count(0)


def test_search_keyboard_shortcuts_and_themes(page, live):
    add_library(page, live)
    page.locator("input[aria-label='Search your library']").fill("fernwoods")
    expect(page.get_by_text("Results for")).to_be_visible()
    expect(page.locator(".trow")).to_have_count(2)
    expect(page.locator(".card").first).to_be_visible()
    page.get_by_role("link", name="Home").click()
    expect(page.locator("input[aria-label='Search your library']")).to_have_value("")  # the box follows the page

    page.evaluate("document.activeElement.blur()")
    page.keyboard.press("n")
    wait_until(page, "location.hash === '#/now'")
    page.keyboard.press("q")
    expect(page.locator(".queue-drawer")).to_have_count(1)
    page.keyboard.press("q")
    expect(page.locator(".queue-drawer")).to_have_count(0)

    page.get_by_role("button", name="Switch to light theme").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")  # remembered


def test_layout_survives_a_narrow_window(page, live):
    add_library(page, live)
    for width in (900, 640):
        page.set_viewport_size({"width": width, "height": 700})
        for route in ("#/", "#/songs", "#/albums", "#/playlists", "#/settings"):
            page.evaluate(f"location.hash = '{route}'")
            page.wait_for_timeout(250)
            assert page.evaluate("document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth"), (
                f"horizontal page scroll at {width}px on {route}"
            )


FAKE_DESKTOP = """
(() => {
  const listeners = new Set();
  let status = { state: 'idle', current: '0.3.0', version: null, percent: 0, error: null, checkedAt: null };
  const push = (patch) => { status = { ...status, ...patch }; listeners.forEach((l) => l({ ...status })); };
  window.__bridge = { calls: [], push };
  window.mtk = {
    selectFolder: async () => null, selectFile: async () => null, showItemInFolder: async () => null,
    openPath: async () => null, openExternal: async () => null, onMediaKey: () => () => {},
    update: {
      status: async () => ({ ...status }),
      check: async () => { window.__bridge.calls.push('check'); push({ state: 'checking' }); setTimeout(() => push({ state: 'up-to-date', checkedAt: Date.now() }), 80); return { ...status }; },
      install: async () => { window.__bridge.calls.push('install'); return true; },
      setAuto: async (on) => { window.__bridge.calls.push('auto:' + on); return { ...status }; },
      onStatus: (fn) => { listeners.add(fn); return () => listeners.delete(fn); },
    },
  };
})();
"""


class TestDesktopUpdates:
    """The Updates card and the restart prompt, against a stand-in for the desktop shell's bridge."""

    @pytest.fixture
    def desktop(self, page):
        page.add_init_script(FAKE_DESKTOP)
        page.reload()
        page.wait_for_selector(".sidebar")
        page.evaluate("location.hash = '#/settings'")
        return page

    def calls(self, page) -> list[str]:
        return page.evaluate("window.__bridge.calls")

    def test_checking_downloading_and_restarting(self, desktop):
        page = desktop
        card = page.locator(".card-panel", has_text="Updates")
        expect(card.locator(".status-line")).to_have_text("Not checked yet.")

        card.get_by_role("button", name="Check for updates").click()
        expect(card.locator(".status-line")).to_contain_text("You have the latest version (0.3.0)")
        assert self.calls(page) == ["check"]

        page.evaluate("window.__bridge.push({ state: 'downloading', version: '0.3.1', percent: 40 })")
        expect(card.locator(".status-line")).to_have_text("Downloading version 0.3.1… 40%")
        expect(card.get_by_role("button", name="Check for updates")).to_be_disabled()

        page.evaluate("window.__bridge.push({ state: 'ready', version: '0.3.1', percent: 100 })")
        expect(page.get_by_text("Music Toolkit 0.3.1 is ready to install.")).to_be_visible()
        expect(page.get_by_role("button", name="Update ready · restart")).to_be_visible()
        page.locator(".toast").get_by_role("button", name="Restart now").click()
        card.get_by_role("button", name="Restart and install 0.3.1").click()
        page.get_by_role("button", name="Update ready · restart").click()
        assert self.calls(page).count("install") == 3

    def test_a_failed_check_says_why_and_the_automatic_switch_is_remembered(self, desktop):
        page = desktop
        card = page.locator(".card-panel", has_text="Updates")
        page.evaluate("window.__bridge.push({ state: 'error', error: \"Couldn't reach GitHub to look for updates. Are you offline?\" })")
        expect(card.locator(".status-line")).to_contain_text("Are you offline?")

        switch = card.get_by_label("Look for updates automatically")
        expect(switch).to_be_checked()
        switch.uncheck()
        expect(page.get_by_text("Saved")).to_be_visible()
        assert "auto:false" in self.calls(page)
        assert api(page, "/settings")["app"]["auto_update"] is False

    def test_a_plain_browser_has_no_updates_card(self, page):
        page.evaluate("location.hash = '#/settings'")
        expect(page.get_by_role("heading", name="Appearance and data")).to_be_visible()
        expect(page.get_by_role("heading", name="Updates")).to_have_count(0)
