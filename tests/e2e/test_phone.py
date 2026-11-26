"""The app on a phone: a row of tabs and a small player in place of the sidebar and the player bar, pages in one column that never
scroll sideways, rows that play when they are tapped, and menus and dialogs that come up from the bottom.

The browser is Chromium pretending to be an iPhone (its screen, its touch, its name for itself); the real thing is a person's to try.
"""

from __future__ import annotations

import os
import re

from playwright.sync_api import expect

from tests.e2e.conftest import PHONE, add_library, api, signed_in_page, wait_until

expect.set_options(timeout=10_000)

PLAYER = "import('/static/js/player.js')"

# Every page of the app that does not need something to be set up first.
ROUTES = [
    "/", "/songs", "/albums", "/artists", "/playlists", "/favorites", "/recent", "/radio", "/podcasts", "/discover", "/stats",
    "/history", "/history/import", "/tools", "/tools/enrich", "/tools/organize", "/tools/convert", "/tools/duplicates",
    "/tools/missing", "/discs", "/devices", "/settings", "/mixes", "/search/Aurora", "/now",
]

# What sticks out of the page's width: the page itself, or something in it that nothing above it clips (a shelf of cards that is
# swiped sideways is meant to, and is clipped by its own row).
OVERFLOW = """() => {
  const view = document.querySelector('#main');
  const width = view.clientWidth;
  const out = [];
  for (const el of view.querySelectorAll('*')) {
    const box = el.getBoundingClientRect();
    if (box.width === 0 || box.height === 0 || (box.right <= width + 16.5 && box.left >= -0.5)) continue;
    let clipped = false;
    for (let up = el.parentElement; up && up !== view; up = up.parentElement) {
      const style = getComputedStyle(up);
      const edge = up.getBoundingClientRect();
      if (/(auto|scroll|hidden|clip)/.test(style.overflowX) && edge.right <= width + 16.5 && edge.left >= -0.5) { clipped = true; break; }
    }
    if (!clipped) out.push(`${el.tagName.toLowerCase()}.${String(el.className).split(' ').join('.')} right=${Math.round(box.right)} left=${Math.round(box.left)}`);
  }
  return { window: window.innerWidth, view: view.clientWidth, scroll: view.scrollWidth, page: document.documentElement.scrollWidth, offenders: out.slice(0, 6) };
}"""


def open_phone(browser, live, **options):
    pages = signed_in_page(browser, live, device=PHONE, **options)
    return pages, next(pages)


def go(page, route: str) -> None:
    page.evaluate(f"location.hash = '#{route}'")
    page.wait_for_selector(f'#main[data-route="{route}"]')
    page.wait_for_timeout(250)  # (what the page asks for has arrived and been drawn)


def test_every_page_fits_the_width_of_a_phone(browser, live):
    pages, page = open_phone(browser, live)
    add_library(page, live)
    api(page, "/playlists", "POST", {"name": "Evening", "track_ids": [1, 2, 3]})
    page.evaluate(f"{PLAYER}.then(async (m) => {{ const {{ items }} = await fetch('/api/tracks?limit=5').then((r) => r.json()); await m.playQueue(items, 0); m.pause(); }})")
    album = api(page, "/albums?limit=1")["items"][0]["key"]
    routes = ROUTES + [f"/album/{album}", "/artist/Aurora%20Vale", "/playlist/1"]
    problems = []
    for route in routes:
        go(page, route)
        got = page.evaluate(OVERFLOW)
        if got["window"] != 390 or got["scroll"] > got["view"] or got["page"] > 390 or got["offenders"]:
            problems.append(f"{route}: {got}")
    assert not problems, "pages wider than the phone:\n" + "\n".join(problems)
    next(pages, None)


# ---------------------------------------------------------------------------------------------------- the frame


def test_a_phone_gets_a_row_of_tabs_and_no_sidebar_and_no_player_bar_until_something_plays(browser, live):
    pages, page = open_phone(browser, live)
    assert page.evaluate("document.documentElement.dataset.layout") == "phone"
    assert page.locator(".sidebar").count() == 0 and page.locator(".playerbar").count() == 0 and page.locator(".queue-drawer").count() == 0
    tabs = page.get_by_role("navigation", name="Tabs")
    expect(tabs).to_be_visible()
    assert [t.strip() for t in tabs.locator(".tabbar-item").all_inner_texts()] == ["Home", "Songs", "Albums", "Playlists", "More"]
    assert page.locator(".miniplayer").count() == 0  # (nothing is playing: nothing to show)
    assert page.get_by_role("button", name="Back", exact=True).count() == 0  # (the first page has nothing behind it)
    assert page.get_by_role("button", name=re.compile("^Switch to")).count() == 0 and page.get_by_role("button", name="Forward", exact=True).count() == 0  # (the theme is chosen in Settings; a phone has no forward button)
    assert page.get_by_label("Search your library").get_attribute("placeholder") == "Search your library"  # (no keyboard shortcut to hint at)
    assert page.evaluate("getComputedStyle(document.body).overscrollBehaviorY") == "none"  # (a pull on the page is not a bounce or a reload: it is an app)
    box = tabs.bounding_box()
    assert box["y"] + box["height"] == 844 and box["width"] == 390  # (along the bottom edge, the whole width)
    for item in tabs.locator(".tabbar-item").all():
        assert item.bounding_box()["height"] >= 44 and item.bounding_box()["width"] >= 44
    next(pages, None)


def test_the_tab_of_the_page_you_are_on_is_marked_and_more_is_marked_for_the_pages_it_holds(browser, live):
    pages, page = open_phone(browser, live)
    tabs = page.get_by_role("navigation", name="Tabs")
    for route, name in (("/songs", "Songs"), ("/albums", "Albums"), ("/playlists", "Playlists"), ("/", "Home")):
        go(page, route)
        assert tabs.locator('[aria-current="page"]').all_inner_texts() == [name], route
    go(page, "/songs")
    page.get_by_role("button", name="Back", exact=True).tap()  # (the way back that a Home Screen app has no browser to give)
    page.wait_for_selector('#main[data-route="/"]')  # (where it came from)
    go(page, "/settings")
    assert tabs.locator('[aria-current="page"]').count() == 0 and "active" in tabs.get_by_role("button", name="More").get_attribute("class")
    go(page, "/album/anything")  # (an album's own page belongs to the Albums tab)
    assert tabs.locator('[aria-current="page"]').all_inner_texts() == ["Albums"]
    next(pages, None)


def test_more_lists_the_pages_the_tabs_leave_out_and_going_to_one_puts_it_away(browser, live):
    pages, page = open_phone(browser, live)
    page.get_by_role("button", name="More").tap()
    sheet = page.get_by_role("dialog", name="More")
    expect(sheet).to_be_visible()
    names = [t.strip() for t in sheet.locator(".more-item").all_inner_texts()]
    assert names == [
        "Artists", "Favorites", "Recently played", "Radio", "Podcasts", "Discover", "Stats", "Listening history",
        "Library tools", "Discs", "Devices", "Settings",
    ]
    expect(sheet.get_by_role("link", name="Artists")).to_be_focused()  # (the keyboard is in it, not behind it)
    sheet.get_by_role("heading", name="Listen").tap()
    sheet.get_by_role("heading", name="More").tap()
    expect(sheet).to_be_visible()  # (a tap on the sheet itself is not one on what is around it)
    box = sheet.bounding_box()
    assert box["y"] + box["height"] == 844 and box["width"] == 390  # (comes up from the bottom edge)
    for item in sheet.locator(".more-item").all():
        assert item.bounding_box()["height"] >= 44
    sheet.get_by_role("link", name="Settings").tap()
    page.wait_for_selector('#main[data-route="/settings"]')
    expect(sheet).to_have_count(0)
    # the other ways out: the close button, the dark part around it, Escape
    for close in ("button", "backdrop", "escape"):
        page.get_by_role("button", name="More").tap()
        expect(sheet).to_be_visible()
        if close == "button":
            sheet.get_by_role("button", name="Close").tap()
        elif close == "backdrop":
            page.touchscreen.tap(195, 40)
        else:
            page.keyboard.press("Escape")
        expect(sheet).to_have_count(0)
    next(pages, None)


# ---------------------------------------------------------------------------------------------------- playing


def test_a_tap_on_a_row_plays_it_and_the_small_player_opens_the_big_one(browser, live):
    pages, page = open_phone(browser, live)
    add_library(page, live)
    go(page, "/songs")
    page.locator(".trow:not(.skeleton)").first.tap()
    mini = page.locator(".miniplayer")
    expect(mini).to_be_visible()
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")
    title = mini.locator(".t1").inner_text()
    assert title and mini.locator(".t2").inner_text()
    assert page.locator(".trow.current .t1").inner_text() == title
    assert mini.get_by_role("button", name="Pause").bounding_box()["width"] >= 44

    line = "parseFloat(getComputedStyle(document.querySelector('.mini-progress')).getPropertyValue('--pct'))"
    wait_until(page, f"{line} > 0")
    page.evaluate(f"{PLAYER}.then((m) => {{ m.seek(6); m.pause(); }})")  # (half way through the 12 seconds)
    wait_until(page, f"{line} > 40 && {line} < 60")
    page.evaluate(f"{PLAYER}.then((m) => m.toggle())")
    mini.get_by_role("button", name="Pause").tap()
    wait_until(page, f"{PLAYER}.then((m) => !m.player.get().playing)")
    expect(mini.get_by_role("button", name="Play")).to_be_visible()
    mini.get_by_role("button", name="Next").tap()
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().pos === 1)")
    assert mini.locator(".t1").inner_text() != title

    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")  # (Next plays the next song)
    page.locator(".trow.current").tap()  # (a tap on the row that is playing is a Pause, or a Play: not a restart)
    wait_until(page, f"{PLAYER}.then((m) => !m.player.get().playing)")
    page.locator(".trow.current").tap()
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")

    mini.locator(".mini-main").tap()
    page.wait_for_selector('#main[data-route="/now"]')
    expect(mini).to_have_count(0)  # (Now Playing is the player: the small one is not drawn over it)
    now = page.locator(".nowplaying")
    expect(now.get_by_role("button", name="Pause")).to_be_visible()  # (it is playing)
    for name in ("Previous", "Next"):
        assert now.get_by_role("button", name=name).bounding_box()["width"] >= 44
    assert now.get_by_role("button", name="Pause").bounding_box()["width"] >= 60
    expect(now.get_by_label("Seek")).to_be_visible()
    next(pages, None)


def test_a_row_of_songs_on_a_phone_is_the_song_with_its_artist_under_it_and_two_buttons(browser, live):
    pages, page = open_phone(browser, live)
    add_library(page, live)
    go(page, "/songs")
    row = page.locator(".trow:not(.skeleton)").first
    cells = row.locator(".tcell")
    assert cells.count() == 3  # the song, the heart, the menu: no number, artist, album, year, length...
    assert row.locator(".tcell.title .t2").inner_text() == "Aurora Vale"  # (the artist moves under the title when it has no column of its own)
    assert row.bounding_box()["height"] >= 44
    buttons = row.locator("button").all()
    assert len(buttons) == 2
    for button in buttons:
        box = button.bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44  # (a finger)
    next(pages, None)


def test_a_text_box_on_a_phone_is_big_enough_that_the_phone_does_not_zoom_into_it(browser, live):
    """Safari zooms the page into any text box whose text is smaller than 16 px, and does not zoom back out."""
    pages, page = open_phone(browser, live)
    go(page, "/songs")
    for box in (page.get_by_label("Search your library"), page.get_by_label("Search songs, artists, albums")):
        assert page.evaluate("(el) => parseFloat(getComputedStyle(el).fontSize)", box.element_handle()) >= 16
        assert box.locator("xpath=ancestor::label").bounding_box()["height"] >= 40  # (the box that is drawn, which a tap anywhere in focuses)
    go(page, "/settings")
    for el in page.locator("input[type=text], input[type=password], input[type=number], select").all():
        if el.is_visible():
            assert page.evaluate("(e) => parseFloat(getComputedStyle(e).fontSize)", el.element_handle()) >= 16
    next(pages, None)


def test_the_page_of_the_player_has_the_heart_and_it_works(browser, live):
    pages, page = open_phone(browser, live)
    add_library(page, live)
    go(page, "/songs")
    page.locator(".trow:not(.skeleton)").first.tap()
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")
    go(page, "/now")
    title = page.locator(".np-meta h1").inner_text()
    heart = page.locator(".nowplaying").get_by_role("button", name="Add to favorites")
    assert heart.bounding_box()["width"] >= 44
    heart.tap()
    expect(page.locator(".nowplaying").get_by_role("button", name="Remove from favorites")).to_be_visible()
    favorites = [t["title"] for t in api(page, "/tracks?limit=50")["items"] if t["favorite"]]
    assert favorites == [title]
    next(pages, None)


def test_the_seek_bar_takes_a_finger(browser, live):
    pages, page = open_phone(browser, live)
    add_library(page, live)
    go(page, "/songs")
    page.locator(".trow:not(.skeleton)").first.tap()
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")
    go(page, "/now")
    seek = page.get_by_label("Seek")
    box = seek.bounding_box()
    assert box["height"] >= 28  # (a track thin as a line is not something to hit)
    page.touchscreen.tap(box["x"] + box["width"] * 0.75, box["y"] + box["height"] / 2)
    wait_until(page, f"{PLAYER}.then((m) => m.deckState().decks[0].time > m.deckState().decks[0].duration * 0.6)")
    next(pages, None)


# ---------------------------------------------------------------------------------------------------- menus and dialogs


def test_the_menu_of_a_row_comes_up_from_the_bottom_and_a_dialog_too(browser, live):
    pages, page = open_phone(browser, live)
    add_library(page, live)
    go(page, "/songs")
    page.get_by_role("button", name="More actions").first.tap()
    menu = page.get_by_role("menu", name="Actions")
    expect(menu).to_be_visible()
    box = menu.bounding_box()
    assert box["y"] + box["height"] == 844 and box["x"] == 0 and box["width"] == 390
    assert all(item.bounding_box()["height"] >= 44 for item in menu.get_by_role("menuitem").all())
    menu.get_by_role("menuitem", name="Add to playlist…").tap()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    box = dialog.bounding_box()
    assert box["y"] + box["height"] == 844 and box["width"] == 390  # (a sheet from the bottom, the whole width, not a box in the middle)
    next(pages, None)


def test_a_toast_is_as_wide_as_the_phone_even_when_it_says_little_and_is_clear_of_the_player(browser, live):
    pages, page = open_phone(browser, live)
    add_library(page, live)
    go(page, "/songs")
    page.locator(".trow:not(.skeleton)").first.tap()
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")
    page.evaluate("import('/static/js/state.js').then((m) => m.toast('Saved', 'info', 0))")
    toast = page.locator(".toast")
    expect(toast).to_be_visible()
    box, mini = toast.bounding_box(), page.locator(".miniplayer").bounding_box()
    assert box["width"] >= 390 - 24 - 1 and box["x"] >= 11 and box["y"] + box["height"] <= mini["y"]
    next(pages, None)


# ---------------------------------------------------------------------------------------------------- other screens


def test_a_phone_on_its_side_still_gets_the_phone_layout_and_a_computer_does_not(browser, live):
    pages, page = open_phone(browser, live)
    page.set_viewport_size({"width": 844, "height": 390})
    page.wait_for_timeout(200)
    assert page.evaluate("document.documentElement.dataset.layout") == "phone"
    assert page.locator(".tabbar").count() == 1 and page.locator(".sidebar").count() == 0
    next(pages, None)


def test_the_layout_follows_the_size_of_the_window(browser, live):
    """A window dragged wider, a tablet turned: the sidebar and the player bar come back, and go again when it is narrow."""
    pages, page = open_phone(browser, live)
    assert page.locator(".tabbar").count() == 1 and page.locator(".sidebar").count() == 0
    page.set_viewport_size({"width": 1360, "height": 860})
    page.wait_for_selector(".sidebar")
    assert page.locator(".tabbar").count() == 0 and page.evaluate("document.documentElement.dataset.layout") == "desktop"
    assert page.locator(".playerbar").count() == 1
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_selector(".tabbar")
    assert page.locator(".sidebar").count() == 0 and page.evaluate("document.documentElement.dataset.layout") == "phone"
    next(pages, None)


def test_the_desktop_window_keeps_its_own_layout_however_narrow_it_is_made(browser, live):
    """The window of the Windows app can be made as narrow as 720 px, which is where a phone's layout begins in a browser: it is never a phone."""
    narrow = {"viewport": {"width": 700, "height": 700}}
    pages = signed_in_page(browser, live, init_script="window.mtk = { version: 'test' };", device=narrow)
    page = next(pages)
    assert page.evaluate("document.documentElement.dataset.layout") == "desktop"
    assert page.locator(".sidebar").count() == 1 and page.locator(".tabbar").count() == 0 and page.locator(".playerbar").count() == 1
    next(pages, None)
    pages = signed_in_page(browser, live, device=narrow)  # (the same width in a browser tab is a phone's)
    page = next(pages)
    assert page.evaluate("document.documentElement.dataset.layout") == "phone" and page.locator(".sidebar").count() == 0
    next(pages, None)


def test_where_a_finger_and_a_pointer_that_hovers_are_both_there_a_click_selects_and_does_not_play(browser, live):
    """A tablet with a trackpad: its pointer is coarse but it can hover, so it is not a screen where a tap is all there is. (Blink's own
    settings say what the pointer is; Playwright has no switch for it.)"""
    flags = "--blink-settings=primaryPointerType=2,availablePointerTypes=2,primaryHoverType=2,availableHoverTypes=2"
    both = browser.browser_type.launch(executable_path=os.environ.get("MTK_CHROMIUM") or None, args=[flags, "--autoplay-policy=no-user-gesture-required"])
    try:
        pages = signed_in_page(both, live)
        page = next(pages)
        assert page.evaluate("[matchMedia('(pointer: coarse)').matches, matchMedia('(hover: hover)').matches, matchMedia('(hover: none)').matches]") == [True, True, False]
        add_library(page, live)
        go(page, "/songs")
        page.locator(".trow:not(.skeleton) .t1").first.click()  # (the title: the middle of the row is a link to the album)
        page.wait_for_timeout(400)
        assert page.locator(".trow.selected").count() == 1  # (selected, as on a computer)
        assert page.evaluate(f"{PLAYER}.then((m) => m.player.get().playing)") is False
        next(pages, None)
    finally:
        both.close()


def test_a_tablet_gets_the_layout_of_a_computer_with_what_a_pointer_would_show_always_shown(browser, live):
    tablet = {"viewport": {"width": 820, "height": 1180}, "has_touch": True, "is_mobile": True, "device_scale_factor": 2}
    pages = signed_in_page(browser, live, device=tablet)
    page = next(pages)
    assert page.evaluate("document.documentElement.dataset.layout") == "desktop"
    assert page.locator(".sidebar").count() == 1 and page.locator(".tabbar").count() == 0
    page.evaluate("location.hash = '#/albums'")
    page.wait_for_selector(".card, .empty")
    add_library(page, live)
    go(page, "/albums")
    page.wait_for_selector(".card .card-play")
    assert page.evaluate("getComputedStyle(document.querySelector('.card .card-play')).opacity") == "1"  # (nothing hovers on a tablet)
    next(pages, None)


def test_where_a_phone_layout_begins(browser, live):
    """720 px wide is a phone's, 721 is not; a short touch screen is a phone on its side, a short window on a computer is not."""
    def layout(width: int, height: int, touch: bool) -> str:
        device = {"viewport": {"width": width, "height": height}, "has_touch": touch, "is_mobile": touch}
        pages = signed_in_page(browser, live, device=device)
        page = next(pages)
        found = page.evaluate("document.documentElement.dataset.layout")
        next(pages, None)
        return found

    assert layout(720, 900, touch=True) == "phone" and layout(721, 900, touch=True) == "desktop"
    assert layout(720, 900, touch=False) == "phone"  # (a computer's window made narrow is a phone's too: the sidebar has no room)
    assert layout(900, 500, touch=True) == "phone" and layout(900, 501, touch=True) == "desktop"
    assert layout(1000, 400, touch=False) == "desktop"  # (a short window with a mouse is not a phone on its side)


# ---------------------------------------------------------------------------------------------------- accessibility


def test_the_screens_of_a_phone_pass_axe_in_both_themes(browser, live):
    """The same checks as the sweep of the computer's screens (test_accessibility.py), over what a phone draws that a computer does not: the
    row of tabs, the small player, the sheet of pages, the full-screen player, a menu and a dialog that come up from the bottom."""
    from tests.e2e.test_accessibility import CLEAR, violations  # (skips this test too when axe is not installed)

    pages, page = open_phone(browser, live)
    add_library(page, live)
    page.evaluate(f"{PLAYER}.then(async (m) => {{ const {{ items }} = await fetch('/api/tracks?limit=5').then((r) => r.json()); await m.playQueue(items, 0); m.pause(); }})")
    found = []
    for theme in ("dark", "light"):
        page.evaluate(f"import('/static/js/state.js').then((s) => s.setTheme('{theme}'))")
        for route in ("/", "/songs", "/albums", "/playlists", "/settings", "/now"):
            go(page, route)
            page.evaluate(CLEAR)
            found += [f"{theme} {route}: {line}" for line in violations(page)]
        go(page, "/songs")
        page.get_by_role("navigation", name="Tabs").get_by_role("button", name="More").tap()
        found += [f"{theme} the More sheet: {line}" for line in violations(page)]
        page.keyboard.press("Escape")
        page.get_by_role("button", name="More actions").first.tap()
        expect(page.get_by_role("menu", name="Actions")).to_be_visible()
        found += [f"{theme} a row's menu: {line}" for line in violations(page)]
        page.get_by_role("menuitem", name="Add to playlist…").tap()
        expect(page.get_by_role("dialog")).to_be_visible()
        found += [f"{theme} a dialog: {line}" for line in violations(page)]
        page.evaluate(CLEAR)
    assert not found, "axe found:\n" + "\n".join(found)
    next(pages, None)


ANDROID_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"


def test_a_phone_that_is_not_an_iphone_has_the_layout_of_a_phone_and_the_engine_of_a_computer(browser, live):
    """The phone layout is about the size of the screen and the plain engine is about iOS: neither is taken for the other."""
    from tests.e2e.conftest import COUNT_WEB_AUDIO

    pages = signed_in_page(browser, live, init_script=COUNT_WEB_AUDIO, device={**PHONE, "user_agent": ANDROID_UA})
    page = next(pages)
    assert page.evaluate("document.documentElement.dataset.layout") == "phone"
    assert page.evaluate(f"{PLAYER}.then((m) => m.PLAIN)") is False
    add_library(page, live)
    go(page, "/songs")
    page.locator(".trow:not(.skeleton)").first.tap()
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")
    go(page, "/now")
    assert page.locator("canvas.visualizer").count() == 0  # (there is no room for it beside the buttons)
    assert page.evaluate("window.__webAudio") == {"contexts": 1, "elementSources": 2}  # (the equalizer, loudness levelling and crossfade are all there)
    page.get_by_role("tab", name="Sound").tap()
    expect(page.get_by_role("heading", name="Equalizer")).to_be_visible()
    next(pages, None)
