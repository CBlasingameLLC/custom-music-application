"""Accessibility.

axe-core, the engine behind most accessibility checkers, is run over every screen and dialog of the app, in both themes, against
WCAG 2.0 and 2.1 (levels A and AA), the 2.2 AA rules and axe's own best practices; a screen passes only when axe finds nothing.
Axe sees structure, names, roles, contrast and the size of what is clickable. It cannot see whether a dialog takes the keyboard,
where the focus goes when one closes, whether a table can be walked with the arrow keys, or whether a change of page is announced:
the tests after the sweep do that.

What no test here can say is what a screen reader reads aloud, or how the app feels with one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest
from playwright.sync_api import expect

axe_module = pytest.importorskip("axe_playwright_python.sync_playwright", reason="pip install -e '.[e2e]' for the accessibility checks")

from tests.e2e.conftest import add_library, api, signed_in_page, wait_until  # noqa: E402
from tests.e2e.test_openfiles import SHELL  # noqa: E402
from tests.e2e.test_stats import seed_history  # noqa: E402

expect.set_options(timeout=10_000)

AXE = axe_module.Axe()
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]

# Puts the app back to a plain page between two states: no dialog, menu, drawer, popover or toast.
CLEAR = """() => import('/static/js/state.js').then((s) => {
  s.modals.set({ stack: [] }); s.closeMenu(); s.ui.set({ queueOpen: false, jobsOpen: false }); s.toasts.set({ items: [] });
})"""


def violations(page) -> list[str]:
    """What axe finds wrong on the page as it is now, one line each: the rule, the element and why."""
    found = AXE.run(page, options={"runOnly": {"type": "tag", "values": TAGS}, "resultTypes": ["violations"]}).response["violations"]
    lines = []
    for rule in found:
        for node in rule["nodes"]:
            checks = node.get("any") or node.get("all") or node.get("none") or [{}]
            lines.append(f"{rule['id']} ({rule['impact']}): {' '.join(node['target'])}  {node['html'][:120]}  ->  {checks[0].get('message', '')[:200]}")
    return lines


# True once nothing is loading (no spinner, no placeholder rows) and the page has stopped changing for a moment. The player bar is
# left out of "changing": its clock ticks while a song plays.
QUIET = """() => {
  if (!window.__quiet) {
    window.__quiet = { at: performance.now() };
    // A change of page counts as a change, from the moment it is made: until the new page has drawn something the old one is not "quiet".
    window.addEventListener('hashchange', () => { window.__quiet.at = performance.now(); });
    new MutationObserver((changes) => {
      const real = changes.filter((c) => !(c.target.closest ? c.target : c.target.parentElement)?.closest('.playerbar'));
      if (real.length) {
        window.__quiet.at = performance.now();
        const node = real[0].target;
        window.__quiet.last = real[0].type + ' in ' + (node.outerHTML || node.parentElement?.outerHTML || '').slice(0, 160) + ' (' + (real[0].attributeName || '') + ')';
      }
    }).observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });
    return false;
  }
  return performance.now() - window.__quiet.at > 300 && !document.querySelector('.spinner-wrap, .trow.skeleton, [aria-busy=true]');
}"""


def settled(page) -> None:
    """The page has finished drawing what it was asked to show."""
    try:
        wait_until(page, f"({QUIET})()", 20)
    except AssertionError:
        busy = page.evaluate("[window.__quiet.last, Array.from(document.querySelectorAll('.spinner-wrap, .trow.skeleton, [aria-busy=true]'), (e) => e.outerHTML.slice(0, 100))]")
        raise AssertionError(f"the page never settled; last change: {busy[0]}; still loading: {busy[1]}") from None


def has_a_heading(page, name: str) -> None:
    """Every page names itself with a first-level heading. Wait for it, so that axe is never run on a page that is still being drawn
    (on a slow machine the page may not have started when the clock says it is quiet)."""
    try:
        wait_until(page, "!!document.querySelector('main h1')", 15)
    except AssertionError:
        raise AssertionError(f"[{name}] no first-level heading appeared") from None


def description(page) -> str:
    """The control that has the keyboard, in words."""
    return page.evaluate(
        """() => { const a = document.activeElement; return !a ? '' : (a.getAttribute('aria-label') || a.textContent.trim().slice(0, 40) || a.tagName.toLowerCase()); }"""
    )


class Tour:
    """Puts the app into each state worth checking, the way a person would get there."""

    def __init__(self, page, device_folder: Path) -> None:
        self.page = page
        self.device_folder = device_folder

    def at(self, hash_: str) -> Callable[[], None]:
        def go() -> None:
            self.page.evaluate("(hash) => { location.hash = hash; }", hash_)
            settled(self.page)

        return go

    def states(self) -> list[tuple[str, Callable[[], None]]]:
        page = self.page

        def card(kind: str) -> Callable[[], None]:
            def go() -> None:
                page.evaluate("(kind) => { location.hash = kind === 'album' ? '#/albums' : '#/artists'; }", kind)
                page.locator(".card").first.wait_for()
                page.locator(".card").first.click()
                wait_until(page, f"location.hash.startsWith('#/{kind}/')")  # the click has led to the page, which now loads
                settled(page)

            return go

        def playlist() -> None:
            page.evaluate("location.hash = '#/playlists'")
            page.locator(".card", has_text="Road trip").click()
            wait_until(page, "location.hash.startsWith('#/playlist/')")
            settled(page)

        def now(tab: str) -> Callable[[], None]:
            def go() -> None:
                page.evaluate("location.hash = '#/now'")
                page.get_by_role("tab", name=tab).click()
                settled(page)

            return go

        def songs_selected() -> None:
            page.evaluate("location.hash = '#/songs'")
            settled(page)
            page.locator(".trow").nth(0).locator(".t1").click()
            page.locator(".trow").nth(2).locator(".t1").click(modifiers=["Shift"])
            expect(page.locator(".selection-bar")).to_be_visible()

        def context_menu() -> None:
            page.evaluate("location.hash = '#/songs'")
            settled(page)
            page.locator(".trow", has_text="Drift").locator(".t1").click(button="right")
            expect(page.locator(".menu")).to_be_visible()

        def from_menu(item: str, ready: str) -> Callable[[], None]:
            def go() -> None:
                context_menu()
                page.get_by_role("menuitem", name=item).click()
                expect(page.locator(ready).first).to_be_visible()
                settled(page)

            return go

        def new_playlist_prompt() -> None:
            page.evaluate("location.hash = '#/playlists'")
            page.get_by_role("button", name="New playlist").first.click()
            expect(page.get_by_role("dialog", name="New playlist")).to_be_visible()

        def ask(call: str, title: str) -> Callable[[], None]:
            def go() -> None:  # not awaited: the promise comes back when the dialog is closed
                page.evaluate(f"import('/static/js/dialogs.js').then((d) => {{ {call}; }})")
                expect(page.get_by_role("dialog", name=title)).to_be_visible()

            return go

        def activity_log() -> None:
            page.evaluate("location.hash = '#/settings'")
            page.get_by_role("button", name="View the activity log").click()
            expect(page.get_by_role("dialog", name="Activity log")).to_be_visible()
            settled(page)

        def add_device_dialog() -> None:
            page.evaluate("location.hash = '#/devices'")
            page.locator(".page-actions").get_by_role("button", name="Add a device").click()
            expect(page.get_by_role("dialog", name="Add a device")).to_be_visible()
            settled(page)

        def device_page() -> None:
            add_device_dialog()
            page.locator(".modal").get_by_role("button", name="Choose a folder…").click()
            page.get_by_label("Folder path").fill(str(self.device_folder))
            page.get_by_role("button", name="Choose", exact=True).click()
            expect(page.get_by_role("heading", name=self.device_folder.name, exact=True)).to_be_visible()
            settled(page)

        def toasts() -> None:
            page.evaluate(
                "import('/static/js/state.js').then((s) => { s.toast('Saved', 'success', 0); s.toast('Something went wrong', 'error', 0); "
                "s.toast('Playlist removed', 'info', 0, { label: 'Undo', onClick() {} }); })"
            )
            expect(page.locator(".toast")).to_have_count(3)

        def queue_drawer() -> None:
            page.evaluate("import('/static/js/state.js').then((s) => s.ui.set({ queueOpen: true }))")
            expect(page.locator(".queue-drawer .qrow").first).to_be_visible()

        def tasks() -> None:
            page.get_by_role("button", name="Background tasks").click()
            expect(page.locator(".tasks-popover")).to_be_visible()

        def filter_popover() -> None:
            page.evaluate("location.hash = '#/songs'")
            settled(page)
            page.get_by_role("button", name="Genre").click()
            expect(page.locator(".popover")).to_be_visible()

        return [
            ("home", self.at("#/")),
            ("songs", self.at("#/songs")),
            ("songs with a selection", songs_selected),
            ("a filter popover", filter_popover),
            ("albums", self.at("#/albums")),
            ("an album", card("album")),
            ("artists", self.at("#/artists")),
            ("an artist", card("artist")),
            ("playlists", self.at("#/playlists")),
            ("a playlist", playlist),
            ("favorites", self.at("#/favorites")),
            ("recently played", self.at("#/recent")),
            ("search results", self.at("#/search/glacier")),
            ("mixes", self.at("#/mixes")),
            ("discover", self.at("#/discover")),
            ("stats", self.at("#/stats")),
            ("listening history", self.at("#/history")),
            ("spotify import", self.at("#/history/import")),
            ("library tools", self.at("#/tools")),
            ("fix missing tags", self.at("#/tools/enrich")),
            ("organize files", self.at("#/tools/organize")),
            ("find duplicates", self.at("#/tools/duplicates")),
            ("missing files", self.at("#/tools/missing")),
            ("devices", self.at("#/devices")),
            ("the add-a-device dialog", add_device_dialog),
            ("a device", device_page),
            ("settings", self.at("#/settings")),
            ("now playing: lyrics", now("Lyrics")),
            ("now playing: up next", now("Up next")),
            ("now playing: sound", now("Sound")),
            ("a page that does not exist", self.at("#/nonsense")),
            ("the song menu", context_menu),
            ("the song details dialog", from_menu("Song details", ".modal .info-grid")),
            ("the add-to-playlist dialog", from_menu("Add to playlist…", ".modal .inline-form")),
            ("the tag editor", from_menu("Edit tags…", ".modal .tag-form")),
            ("a prompt", new_playlist_prompt),
            ("a confirmation", ask("d.confirmDialog({ title: 'Remove it?', message: 'Remove this thing?', confirm: 'Remove', danger: true })", "Remove it?")),
            ("a typed confirmation", ask("d.typedConfirm({ title: 'Delete it?', message: 'This cannot be undone.', word: 'delete', confirm: 'Delete' })", "Delete it?")),
            ("the activity log", activity_log),
            ("notifications", toasts),
            ("the queue", queue_drawer),
            ("the background tasks list", tasks),
        ]


def sweep(page, device_folder: Path) -> list[str]:
    problems: list[str] = []
    tour = Tour(page, device_folder)
    for name, enter in tour.states():
        page.evaluate(CLEAR)
        enter()
        try:
            settled(page)
        except AssertionError as error:
            raise AssertionError(f"[{name}] {error}") from None
        has_a_heading(page, name)
        problems += [f"[{name}] {line}" for line in violations(page)]
    page.evaluate(CLEAR)
    return problems


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_no_screen_and_no_dialog_fails_an_automated_accessibility_check(page, live, tmp_path, theme) -> None:
    page.emulate_media(reduced_motion="reduce")  # nothing is mid-fade while axe reads the colours
    add_library(page, live)
    seed_history(live)
    ids = {t["title"]: t["id"] for t in api(page, "/tracks?limit=100")["items"]}
    api(page, "/playlists", "POST", {"name": "Road trip", "track_ids": [ids["Glacier"], ids["Drift"], ids["Wires"]]})
    page.evaluate("import('/static/js/state.js').then((s) => s.loadPlaylists())")
    # Something is playing, so the player bar, the queue and Now Playing have their songs in them.
    page.evaluate("location.hash = '#/songs'")
    settled(page)
    page.locator(".trow", has_text="Glacier").locator(".t1").dblclick()
    expect(page.locator(".playerbar .pb-text .t1")).to_have_text("Glacier")
    page.evaluate("(theme) => import('/static/js/state.js').then((s) => s.setTheme(theme))", theme)
    folder = tmp_path / "walkman" / "MUSIC"
    folder.mkdir(parents=True)

    problems = sweep(page, folder.resolve())

    assert not problems, f"{len(problems)} accessibility problems in the {theme} theme:\n" + "\n".join(problems)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_a_new_install_before_any_music_is_added_passes_too(page, theme) -> None:
    page.emulate_media(reduced_motion="reduce")
    page.evaluate("(theme) => import('/static/js/state.js').then((s) => s.setTheme(theme))", theme)
    problems = []
    for name, route in [("home", "#/"), ("songs", "#/songs"), ("albums", "#/albums"), ("now playing", "#/now"), ("stats", "#/stats"), ("settings", "#/settings")]:
        page.evaluate("(hash) => { location.hash = hash; }", route)
        settled(page)
        has_a_heading(page, name)
        problems += [f"[{name}] {line}" for line in violations(page)]

    assert not problems, "\n".join(problems)


def test_the_settings_a_desktop_window_adds_pass_too(browser, live) -> None:
    script = SHELL % {"opened": "[]", "tray": "true"}
    generator = signed_in_page(browser, live, script)
    page = next(generator)
    try:
        page.evaluate("location.hash = '#/settings'")
        expect(page.get_by_role("heading", name="Window and tray")).to_be_visible()
        assert violations(page) == []
    finally:
        for _ in generator:
            pass


# ------------------------------------------------------------------------------------------------ the keyboard


def test_the_first_stop_of_the_keyboard_is_a_skip_link_that_leads_to_the_content(page) -> None:
    link = page.get_by_role("button", name="Skip to content")
    box = link.bounding_box()
    assert box and box["y"] + box["height"] <= 0, "it waits above the window until someone uses the keyboard"

    page.keyboard.press("Tab")

    expect(link).to_be_focused()
    box = link.bounding_box()
    assert box and box["y"] >= 0 and box["x"] >= 0, "and comes into view when it has the focus"
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.id") == "main"
    assert page.evaluate("document.activeElement.tagName") == "MAIN"


def test_the_page_is_divided_into_landmarks_a_screen_reader_can_jump_between(page) -> None:
    expect(page.get_by_role("navigation", name="Main")).to_have_count(1)
    expect(page.get_by_role("banner")).to_have_count(1)
    expect(page.get_by_role("main")).to_have_count(1)
    expect(page.get_by_role("region", name="Player")).to_have_count(1)


def test_a_dialog_takes_the_keyboard_keeps_it_and_gives_it_back(page) -> None:
    page.evaluate("location.hash = '#/playlists'")
    opener = page.get_by_role("button", name="New playlist").first
    opener.focus()

    page.keyboard.press("Enter")

    dialog = page.get_by_role("dialog", name="New playlist")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("textbox", name="Name")).to_be_focused()
    assert page.evaluate("document.querySelector('.app').inert") is True, "the page behind it cannot be reached, or read out"
    for key in ["Tab"] * 5 + ["Shift+Tab"] * 5:
        page.keyboard.press(key)
        assert page.evaluate("!!document.activeElement.closest('[role=dialog]')"), f"{key} left the dialog"
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    expect(opener).to_be_focused()
    assert page.evaluate("document.querySelector('.app').inert") is False


def test_the_close_button_of_a_dialog_gives_the_keyboard_back_too(page) -> None:
    page.evaluate("location.hash = '#/playlists'")
    opener = page.get_by_role("button", name="New playlist").first
    opener.focus()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog", name="New playlist")
    expect(dialog).to_be_visible()

    dialog.get_by_role("button", name="Close").click()

    expect(dialog).to_have_count(0)
    expect(opener).to_be_focused()


def test_a_dialog_that_leads_to_another_gives_the_keyboard_back_to_where_it_started(page) -> None:
    page.evaluate("location.hash = '#/playlists'")
    opener = page.get_by_role("button", name="New playlist").first
    opener.focus()
    page.evaluate(  # not returned: the promise settles when the dialogs are closed, which is the rest of this test
        """import('/static/js/dialogs.js').then((d) => { (async () => {
          if (await d.confirmDialog({ title: 'First question', message: 'Sure?', confirm: 'Yes' })) await d.typedConfirm({ title: 'Second question', message: 'Really?', word: 'delete', confirm: 'Delete' });
        })(); })"""
    )
    page.get_by_role("dialog", name="First question").get_by_role("button", name="Yes").click()
    second = page.get_by_role("dialog", name="Second question")
    expect(second).to_be_visible()
    expect(page.get_by_role("dialog")).to_have_count(1)

    page.keyboard.press("Escape")

    expect(second).to_have_count(0)
    expect(opener).to_be_focused()


def test_a_dialog_without_a_field_puts_the_keyboard_on_its_first_button_or_on_itself(page, live) -> None:
    page.evaluate("import('/static/js/dialogs.js').then((d) => { d.confirmDialog({ title: 'Remove it?', message: 'Remove this thing?', confirm: 'Remove', danger: true }); })")
    confirmation = page.get_by_role("dialog", name="Remove it?")
    expect(confirmation.get_by_role("button", name="Cancel")).to_be_focused()  # the careful choice, not the one that does it
    page.keyboard.press("Escape")
    expect(confirmation).to_have_count(0)

    add_library(page, live)
    page.locator(".trow").first.locator(".t1").click(button="right")
    page.get_by_role("menuitem", name="Song details").click()
    details = page.get_by_role("dialog", name="Song details")
    expect(details).to_be_visible()
    expect(details).to_be_focused()  # nothing in it to use yet, so the dialog itself: a screen reader reads it from the top


def test_a_dialog_opened_from_a_songs_menu_returns_to_the_song_table(page, live) -> None:
    add_library(page, live)
    grid = page.get_by_role("grid", name="Songs")
    grid.focus()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Shift+F10")
    page.get_by_role("menuitem", name="Song details").click()
    dialog = page.get_by_role("dialog", name="Song details")
    expect(dialog.locator(".info-grid").first).to_be_visible()

    page.keyboard.press("Escape")

    expect(dialog).to_have_count(0)
    expect(grid).to_be_focused()


def test_a_menu_is_driven_by_the_arrow_keys_and_escape_gives_the_focus_back(page, live) -> None:
    add_library(page, live)
    grid = page.get_by_role("grid", name="Songs")
    grid.focus()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")  # the second song
    second = page.locator(".trow.cursor .t1").inner_text()

    page.keyboard.press("Shift+F10")
    menu = page.get_by_role("menu")
    expect(menu).to_be_visible()
    expect(menu.get_by_role("menuitem", name="Play", exact=True)).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(menu.get_by_role("menuitem", name="Play next")).to_be_focused()
    page.keyboard.press("End")
    expect(menu.get_by_role("menuitem").last).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(menu.get_by_role("menuitem", name="Play", exact=True)).to_be_focused()  # round to the top
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0)
    expect(grid).to_be_focused()

    page.keyboard.press("Shift+F10")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")  # Add to queue
    expect(page.get_by_text(f'Added "{second}" to the queue')).to_be_visible()
    expect(page.get_by_role("menu")).to_have_count(0)
    expect(grid).to_be_focused()


def test_tab_leaves_a_menu_and_carries_on_from_where_the_menu_was_opened(page, live) -> None:
    add_library(page, live)
    grid = page.get_by_role("grid", name="Songs")
    grid.focus()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Shift+F10")
    expect(page.get_by_role("menu")).to_be_visible()

    page.keyboard.press("Tab")

    expect(page.get_by_role("menu")).to_have_count(0)
    expect(page.get_by_role("button", name="Number")).to_be_focused()  # the next stop after the table


def test_the_song_table_is_a_grid_with_the_counts_and_the_sort_a_screen_reader_needs(page, live) -> None:
    add_library(page, live)
    grid = page.get_by_role("grid", name="Songs")

    expect(grid).to_have_attribute("aria-rowcount", "10")  # nine songs and the header
    expect(grid).to_have_attribute("aria-multiselectable", "true")
    headers = page.get_by_role("columnheader")
    expect(headers.filter(has_text="Title")).to_have_attribute("aria-sort", "none")
    first = page.locator(".trow").first
    expect(first).to_have_attribute("aria-rowindex", "2")
    expect(first).to_have_attribute("aria-selected", "false")

    page.get_by_role("button", name="Title").click()
    expect(headers.filter(has_text="Title")).to_have_attribute("aria-sort", "ascending")
    expect(headers.filter(has_text="Artist")).to_have_attribute("aria-sort", "none")
    page.get_by_role("button", name="Title").click()
    expect(headers.filter(has_text="Title")).to_have_attribute("aria-sort", "descending")
    expect(page.get_by_role("columnheader", name="Favorite")).to_have_count(1)  # a header with only a picture still has a name


def test_the_song_table_can_be_walked_selected_and_played_from_the_keyboard(page, live) -> None:
    add_library(page, live)
    grid = page.get_by_role("grid", name="Songs")
    title = lambda: page.locator(".trow.cursor .t1").inner_text()  # noqa: E731
    selected = lambda: page.locator(".trow[aria-selected=true] .t1").all_inner_texts()  # noqa: E731

    # One Tab reaches the table, with the cursor on the first row, drawn as a ring.
    for _ in range(80):
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement.getAttribute('role')") == "grid":
            break
    expect(grid).to_be_focused()
    first = title()
    assert page.evaluate("getComputedStyle(document.querySelector('.trow.cursor')).outlineStyle") == "solid"
    assert grid.get_attribute("aria-activedescendant") == page.locator(".trow.cursor").get_attribute("id")

    page.keyboard.press("ArrowDown")
    second = title()
    assert second != first and selected() == [second], "the arrows move the cursor and the selection"
    page.keyboard.press("End")
    assert page.locator(".trow.cursor").get_attribute("aria-rowindex") == "10"
    page.keyboard.press("Home")
    assert title() == first
    page.keyboard.press("PageDown")
    assert page.locator(".trow.cursor").get_attribute("aria-rowindex") == "10", "nine songs fit in a page"

    page.keyboard.press("Home")
    page.keyboard.press("Shift+ArrowDown")
    page.keyboard.press("Shift+ArrowDown")
    assert len(selected()) == 3
    expect(page.locator(".selection-bar")).to_contain_text("3 songs selected")
    expect(page.get_by_role("status").filter(has_text="3 songs selected")).to_have_count(1)  # said aloud too
    page.keyboard.press("Control+ArrowDown")  # moves the cursor and leaves the selection as it is
    assert len(selected()) == 3
    page.keyboard.press("Control+Space")  # toggles the row the cursor is on
    assert len(selected()) == 4
    page.keyboard.press("Escape")
    assert selected() == []

    page.keyboard.press("Home")
    page.keyboard.press("ArrowDown")
    wanted = title()
    page.keyboard.press("Enter")
    expect(page.locator(".playerbar .pb-text .t1")).to_have_text(wanted)
    expect(page.locator(".playerbar").get_by_role("button", name="Pause")).to_be_visible()


def test_the_arrow_keys_in_the_song_table_do_not_also_change_the_volume(page, live) -> None:
    add_library(page, live)
    page.get_by_role("grid", name="Songs").focus()
    volume = lambda: page.evaluate("import('/static/js/player.js').then((p) => p.player.get().volume)")  # noqa: E731
    before = volume()
    assert before > 0.5, "so that turning it down would show"

    page.keyboard.press("ArrowDown")
    page.keyboard.press("Shift+ArrowDown")  # selects a second song, and does not turn the volume down

    assert volume() == before
    expect(page.locator(".trow[aria-selected=true]")).to_have_count(2)


def test_songs_are_rated_from_the_keyboard_with_the_digits(page, live) -> None:
    add_library(page, live)
    page.get_by_role("grid", name="Songs").focus()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")  # the second song
    name = page.locator(".trow.cursor .t1").inner_text()

    page.keyboard.press("4")

    expect(page.get_by_text(f"Rated “{name}”: 4 stars")).to_be_visible()
    wait_until(page, "fetch('/api/tracks?limit=100').then(r => r.json()).then(d => d.items.filter(t => t.rating === 4).length === 1)")
    assert [t["title"] for t in api(page, "/tracks?limit=100")["items"] if t["rating"] == 4] == [name]
    page.keyboard.press("0")
    expect(page.get_by_text(f"Rating cleared for “{name}”")).to_be_visible()
    wait_until(page, "fetch('/api/tracks?limit=100').then(r => r.json()).then(d => d.items.every(t => !t.rating))")


def test_a_song_is_moved_up_and_down_a_playlist_from_the_keyboard(page, live) -> None:
    add_library(page, live)
    ids = {t["title"]: t["id"] for t in api(page, "/tracks?limit=100")["items"]}
    playlist = api(page, "/playlists", "POST", {"name": "Road trip", "track_ids": [ids["Glacier"], ids["Drift"], ids["Wires"]]})
    page.evaluate(f"location.hash = '#/playlist/{playlist['id']}'")
    expect(page.locator(".trow:not(.skeleton)")).to_have_count(3)
    page.get_by_role("grid", name="Songs").focus()
    page.keyboard.press("Home")  # the cursor is on Glacier
    titles = lambda: [t["title"] for t in api(page, f"/tracks?playlist={playlist['id']}")["items"]]  # noqa: E731

    def moved(key: str, title: str, at: int) -> None:
        page.keyboard.press(key)
        wait_until(page, f"fetch('/api/tracks?playlist={playlist['id']}').then(r => r.json()).then(d => d.items[{at}].title === '{title}')")
        expect(page.locator(".trow.cursor .t1")).to_have_text(title)  # the cursor goes with the song, once the list has reloaded
        expect(page.locator(".trow:not(.skeleton)")).to_have_count(3)

    moved("Alt+ArrowDown", "Glacier", 1)
    assert titles() == ["Drift", "Glacier", "Wires"]
    moved("Alt+ArrowDown", "Glacier", 2)
    moved("Alt+ArrowUp", "Glacier", 1)
    assert titles() == ["Drift", "Glacier", "Wires"]
    moved("Alt+ArrowUp", "Glacier", 0)
    page.keyboard.press("Alt+ArrowUp")  # already first: nothing to do, and nothing is sent that the server would refuse (the page fails the test if it is)
    page.wait_for_timeout(300)
    assert titles() == ["Glacier", "Drift", "Wires"]


def test_a_song_is_moved_up_and_down_the_queue_from_the_keyboard(page, live) -> None:
    add_library(page, live)
    page.locator(".trow", has_text="Glacier").locator(".t1").dblclick()
    expect(page.locator(".playerbar").get_by_role("button", name="Pause")).to_be_visible()
    page.evaluate("import('/static/js/state.js').then((s) => s.ui.set({ queueOpen: true }))")
    rows = page.locator(".queue-drawer .qrow")
    expect(rows).to_have_count(9)
    names = rows.locator(".t1").all_inner_texts()
    rows.nth(2).locator(".qplay").focus()

    page.keyboard.press("Alt+ArrowDown")

    expect(rows.locator(".t1").nth(3)).to_have_text(names[2])
    expect(rows.locator(".t1").nth(2)).to_have_text(names[3])
    expect(rows.nth(3).locator(".qplay")).to_be_focused()  # still on the song that moved
    page.keyboard.press("Alt+ArrowUp")
    expect(rows.locator(".t1").nth(2)).to_have_text(names[2])
    expect(rows.nth(2).locator(".qplay")).to_be_focused()


def test_tabs_are_one_stop_and_move_with_the_arrow_keys(page, live) -> None:
    add_library(page, live)
    page.locator(".trow", has_text="Glacier").locator(".t1").dblclick()
    page.evaluate("location.hash = '#/now'")
    tabs = page.get_by_role("tab")
    expect(tabs).to_have_count(3)
    state = lambda: page.evaluate(  # noqa: E731
        "Array.from(document.querySelectorAll('[role=tab]'), (t) => [t.textContent.trim(), t.getAttribute('aria-selected'), t.tabIndex])"
    )
    assert state() == [["Lyrics", "true", 0], ["Up next", "false", -1], ["Sound", "false", -1]]
    expect(page.get_by_role("tabpanel", name="Lyrics")).to_be_visible()

    tabs.first.focus()
    page.keyboard.press("ArrowRight")
    expect(tabs.nth(1)).to_be_focused()
    assert state() == [["Lyrics", "false", -1], ["Up next", "true", 0], ["Sound", "false", -1]]
    expect(page.get_by_role("tabpanel", name="Up next")).to_be_visible()
    page.keyboard.press("End")
    expect(tabs.nth(2)).to_be_focused()
    page.keyboard.press("ArrowRight")  # round to the first
    expect(tabs.first).to_be_focused()
    page.keyboard.press("ArrowLeft")
    expect(tabs.nth(2)).to_be_focused()
    page.keyboard.press("Home")
    expect(tabs.first).to_be_focused()
    expect(page.get_by_role("tabpanel", name="Lyrics")).to_be_visible()


def test_going_to_another_page_by_keyboard_moves_the_focus_there_and_says_which_page(page, live) -> None:
    add_library(page, live)
    link = page.get_by_role("link", name="Albums", exact=True)
    link.focus()

    page.keyboard.press("Enter")

    announcer = page.locator("div.sr-only[role=status]").filter(has_text="Albums")
    expect(announcer).to_have_count(1)
    wait_until(page, "document.activeElement.tagName === 'MAIN'")
    page.get_by_role("link", name="Artists", exact=True).focus()
    page.keyboard.press("Enter")
    expect(page.locator("div.sr-only[role=status]").filter(has_text="Artists")).to_have_count(1)


def test_typing_in_the_search_box_keeps_the_focus_there_while_the_results_change(page, live) -> None:
    add_library(page, live)
    search = page.get_by_label("Search your library")
    search.focus()

    page.keyboard.type("glacier", delay=40)

    expect(page.get_by_text("Results for")).to_be_visible()
    page.wait_for_timeout(500)  # longer than the page takes to claim the focus, if it were going to
    expect(search).to_be_focused()


def test_problems_are_announced_at_once_and_everything_else_politely(page) -> None:
    page.evaluate(
        "import('/static/js/state.js').then((s) => { s.toast('Playlist saved', 'success', 0); s.toast('The disk is full', 'error', 0); })"
    )

    expect(page.locator(".toasts [role=alert]")).to_contain_text("The disk is full")
    expect(page.locator(".toasts [role=alert]")).not_to_contain_text("Playlist saved")
    expect(page.locator(".toasts [role=status][aria-live=polite]")).to_contain_text("Playlist saved")
    expect(page.locator(".toasts [role=status]")).not_to_contain_text("The disk is full")


def test_the_background_tasks_list_and_a_filter_menu_close_with_escape_and_give_the_focus_back(page, live) -> None:
    add_library(page, live)
    tasks = page.get_by_role("button", name="Background tasks")
    tasks.focus()
    page.keyboard.press("Enter")
    expect(page.locator(".tasks-popover")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".tasks-popover")).to_have_count(0)
    expect(tasks).to_be_focused()

    genre = page.get_by_role("button", name="Genre")
    genre.focus()
    page.keyboard.press("Enter")
    expect(page.locator(".popover")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".popover")).to_have_count(0)
    expect(genre).to_be_focused()


def test_the_shortcut_keys_leave_a_dialog_alone(page, live) -> None:
    add_library(page, live)
    page.evaluate("location.hash = '#/playlists'")
    page.get_by_role("button", name="New playlist").first.click()
    dialog = page.get_by_role("dialog", name="New playlist")
    expect(dialog).to_be_visible()

    dialog.get_by_role("button", name="Close").focus()
    for key in ["q", "n", "m", "s", "r"]:
        page.keyboard.press(key)

    expect(page.locator(".queue-drawer")).to_have_count(0)
    assert page.evaluate("location.hash") == "#/playlists"
    assert page.evaluate("import('/static/js/player.js').then((p) => p.player.get().muted)") is False


# ------------------------------------------------------------------------------------------ what the system asks for


# Does the element that has the keyboard show it? A ring of its own, a ring on what it sits in (the search box), or, for the song
# table, a ring on the row the cursor is on.
FOCUS_PROBE = """() => {
  const el = document.activeElement;
  const ringed = (n) => { const s = getComputedStyle(n); return s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0; };
  let ok = false;
  for (let n = el, up = 0; n && up < 3 && !ok; n = n.parentElement, up++) ok = ringed(n) && (n === el || n.matches(':focus-within'));
  if (el.matches('.tgrid')) ok = Boolean(el.querySelector('.trow.cursor')) && ringed(el.querySelector('.trow.cursor'));
  return { ok, tag: el.tagName, name: el.getAttribute('aria-label') || el.textContent.trim().slice(0, 30) || el.tagName };
}"""


def stops_without_a_ring(page, presses: int) -> tuple[int, list[str]]:
    """Press Tab `presses` times; how many different things had the keyboard, and which of them showed no sign of it."""
    stops = []
    for _ in range(presses):
        page.keyboard.press("Tab")
        stops.append(page.evaluate(FOCUS_PROBE))
    stops = [s for s in stops if s["tag"] != "BODY"]  # going all the way round, the keyboard leaves the page for a moment
    return len({s["name"] for s in stops}), [s["name"] for s in stops if not s["ok"]]


def test_every_stop_of_the_keyboard_on_the_home_page_shows_that_it_has_the_focus(page, live) -> None:
    add_library(page, live)
    page.evaluate("location.hash = '#/'")
    expect(page.locator(".shelf").first).to_be_visible()

    different, bare = stops_without_a_ring(page, 90)  # the page, the player, then the sidebar and the top bar

    assert different > 30, "the home page has many stops"
    assert bare == []


def test_and_so_does_every_stop_in_settings_and_on_the_sound_controls_and_in_a_dialog(page, live) -> None:
    add_library(page, live)
    page.evaluate("location.hash = '#/settings'")
    expect(page.get_by_role("heading", name="Appearance", exact=True)).to_be_visible()
    different, bare = stops_without_a_ring(page, 110)  # text fields, selects, switches, colour swatches, buttons
    assert different > 40 and bare == []

    page.evaluate("location.hash = '#/songs'")
    page.locator(".trow", has_text="Glacier").locator(".t1").dblclick()
    page.evaluate("location.hash = '#/now'")
    page.get_by_role("tab", name="Sound").click()
    expect(page.get_by_label("Equalizer preset")).to_be_visible()
    different, bare = stops_without_a_ring(page, 60)  # sliders and a select
    assert different > 15 and bare == []

    page.evaluate("location.hash = '#/playlists'")
    page.get_by_role("button", name="New playlist").first.click()
    expect(page.get_by_role("dialog", name="New playlist")).to_be_visible()
    different, bare = stops_without_a_ring(page, 6)  # a text field and two buttons, round and round
    assert different >= 2 and bare == []


def test_the_visualizer_and_smooth_scrolling_stand_aside_when_less_motion_is_asked_for(page, live) -> None:
    add_library(page, live)
    page.locator(".trow", has_text="Glacier").locator(".t1").dblclick()
    page.evaluate("location.hash = '#/now'")
    expect(page.locator("canvas.visualizer")).to_have_count(1)
    assert page.evaluate("import('/static/js/lib.js').then((l) => l.scrollBehavior())") == "smooth"

    page.emulate_media(reduced_motion="reduce")
    page.evaluate("location.hash = '#/songs'")
    page.evaluate("location.hash = '#/now'")

    expect(page.locator("canvas.visualizer")).to_have_count(0)
    assert page.evaluate("import('/static/js/lib.js').then((l) => l.scrollBehavior())") == "auto"


def test_in_windows_high_contrast_what_is_chosen_is_told_by_more_than_its_colour(page, live) -> None:
    add_library(page, live)
    page.emulate_media(forced_colors="active")
    page.evaluate("location.hash = '#/songs'")
    settled(page)
    page.locator(".trow").first.locator(".t1").click()

    nav = page.locator(".nav-item.active")
    assert nav.evaluate("(el) => getComputedStyle(el).outlineStyle") == "solid"
    assert page.locator(".trow.selected").first.evaluate("(el) => getComputedStyle(el).outlineStyle") == "solid"


def test_more_contrast_turns_the_quiet_greys_into_the_text_colour(page) -> None:
    quiet = page.locator(".nav-label").first
    assert quiet.evaluate("(el) => getComputedStyle(el).color") != page.evaluate("getComputedStyle(document.body).color")

    page.emulate_media(contrast="more")

    assert quiet.evaluate("(el) => getComputedStyle(el).color") == page.evaluate("getComputedStyle(document.body).color")
