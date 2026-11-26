"""The menu of a row closes when the page scrolls, but not for a scroll event that is only now arriving."""

from __future__ import annotations

from playwright.sync_api import expect

from tests.e2e.conftest import add_library

expect.set_options(timeout=10_000)

SCROLL = "document.querySelector('#main').dispatchEvent(new Event('scroll'))"


def test_a_scroll_event_just_after_a_menu_opens_does_not_close_it_and_a_later_one_does(page, live) -> None:
    """A browser sends a scroll event a frame after the page scrolled, so one for a scroll that came before the menu opened (a click that
    brought its row into view) reaches an open menu: it is not someone leaving it. The clock is the test's, so that the moments are exact."""
    add_library(page, live)
    page.evaluate("window.__now = 1000; performance.now = () => window.__now;")
    page.locator(".trow:not(.skeleton)").first.get_by_role("button", name="More actions").click()
    menu = page.get_by_role("menu", name="Actions")
    expect(menu).to_be_visible()

    for moment in (1000, 1001, 1100, 1250):  # (up to a quarter of a second after it opened, the end of that included)
        page.evaluate(f"window.__now = {moment}; {SCROLL}")
        page.wait_for_timeout(100)
        expect(menu).to_be_visible()
    page.evaluate(f"window.__now = 1251; {SCROLL}")
    expect(menu).to_have_count(0)  # (a scroll after that: the page is being moved, and the menu is left behind)


def test_a_menu_that_is_opened_again_has_a_new_quarter_of_a_second(page, live) -> None:
    add_library(page, live)
    page.evaluate("window.__now = 1000; performance.now = () => window.__now;")
    more = page.locator(".trow:not(.skeleton)").first.get_by_role("button", name="More actions")
    more.click()
    page.evaluate(f"window.__now = 1300; {SCROLL}")
    expect(page.get_by_role("menu", name="Actions")).to_have_count(0)
    page.evaluate("window.__now = 5000")
    more.click()  # (opened at 5000: a scroll at 5100 is within its own quarter of a second, though 4100 after the first)
    expect(page.get_by_role("menu", name="Actions")).to_be_visible()
    page.evaluate(f"window.__now = 5100; {SCROLL}")
    page.wait_for_timeout(100)
    expect(page.get_by_role("menu", name="Actions")).to_be_visible()
