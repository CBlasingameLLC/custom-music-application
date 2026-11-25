"""The app on a phone: a row of tabs and a small player in place of the sidebar and the player bar, pages in one column that never
scroll sideways, rows that play when they are tapped, and menus and dialogs that come up from the bottom.

The browser is Chromium pretending to be an iPhone (its screen, its touch, its name for itself); the real thing is a person's to try.
"""

from __future__ import annotations

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


def test_a_toast_is_as_wide_as_the_phone_and_clear_of_the_player(browser, live):
    pages, page = open_phone(browser, live)
    add_library(page, live)
    go(page, "/songs")
    page.locator(".trow:not(.skeleton)").first.tap()
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")
    page.evaluate("import('/static/js/state.js').then((m) => m.toast('This is said in passing, and is long enough to need more than one line on a phone.', 'info', 0))")
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
