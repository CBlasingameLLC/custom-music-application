"""A switch in Settings, and the order in which the server's answers reach the page.

The page asks for the settings when Settings opens, and saves a switch the moment it is turned. The server answers them in whatever order the network brings them, and a
phone on mobile data takes a good part of a second over each. These tests hold the answers of the server back and give them to the page in the order that went wrong: a switch
that is turned must stay as it was turned until its save is answered, an older answer must not put it back, and a save that failed must put it back and say so.
"""

from __future__ import annotations

from playwright.sync_api import expect

from tests.e2e.conftest import api

expect.set_options(timeout=10_000)
RADAR = "Look for new releases by artists I play, about once a day"
LYRICS = "Look up lyrics on lrclib.net when a song has none"


def hold(page, *methods: str) -> dict[str, list]:
    """The server answers the calls of the page to /api/settings of these methods at once, but the page is given the answers only when the test says so.
    `held[method]` is a list of (the call, the answer), in the order they were made."""
    held: dict[str, list] = {method: [] for method in methods}

    def handler(route):
        method = route.request.method
        if method in held:
            held[method].append((route, route.fetch()))
        else:
            route.continue_()

    page.route("**/api/settings", handler)
    return held


def wait_for(page, held: dict, method: str, count: int = 1) -> None:
    for _ in range(100):
        if len(held[method]) >= count:
            return
        page.wait_for_timeout(50)
    raise AssertionError(f"the page made {len(held[method])} {method} calls to /api/settings, not {count}")


def give(held: dict, method: str, index: int = 0) -> None:
    call, answer = held[method][index]
    call.fulfill(response=answer)


def kept(page) -> dict:
    """What the server has, asked without the holding."""
    page.unroute("**/api/settings")
    return api(page, "/settings")["app"]


def open_settings_and_turn_on(page, held: dict):
    """Settings opens (its reading of the settings is made and answered by the server, and held), and the switch is turned on, which is saved (and held)."""
    page.evaluate("location.hash = '#/settings'")
    wait_for(page, held, "GET")
    box = page.get_by_label(RADAR)
    box.check()
    wait_for(page, held, "PUT")
    return box


def test_an_older_answer_that_arrives_while_a_switch_is_being_saved_does_not_turn_it_back(page, live):
    held = hold(page, "GET", "PUT")
    box = open_settings_and_turn_on(page, held)

    give(held, "GET")  # (what the page asked when it opened, before the click: the switch is off in it)
    page.wait_for_timeout(300)
    expect(box).to_be_checked()

    give(held, "PUT")
    expect(page.get_by_text("Saved")).to_be_visible()
    expect(box).to_be_checked()
    assert kept(page)["release_radar"] is True


def test_an_older_answer_that_arrives_after_the_save_does_not_take_the_switch_back(page, live):
    held = hold(page, "GET", "PUT")
    box = open_settings_and_turn_on(page, held)

    give(held, "PUT")
    expect(page.get_by_text("Saved")).to_be_visible()
    expect(box).to_be_checked()

    give(held, "GET")  # (the answer to the question asked before the click, after the answer to the save)
    page.wait_for_timeout(300)
    expect(box).to_be_checked()
    assert kept(page)["release_radar"] is True


def test_a_switch_stays_as_it_was_turned_when_the_page_is_drawn_again_before_its_save_is_answered(page, live):
    held = hold(page, "PUT")
    page.evaluate("location.hash = '#/settings'")
    box = page.get_by_label(RADAR)
    box.check()
    wait_for(page, held, "PUT")

    # Something else on the page is answered (the server's About, as it is when Settings opens), and Settings is drawn again with the setting as the page still has it.
    page.evaluate("import('/static/js/state.js').then((state) => state.loadAbout())")
    page.wait_for_timeout(300)
    expect(box).to_be_checked()

    give(held, "PUT")
    expect(page.get_by_text("Saved")).to_be_visible()
    expect(box).to_be_checked()
    assert kept(page)["release_radar"] is True


def test_a_switch_turned_while_the_save_of_another_is_not_answered_yet_stays_turned(page, live):
    held = hold(page, "PUT")
    page.evaluate("location.hash = '#/settings'")
    radar, lyrics = page.get_by_label(RADAR), page.get_by_label(LYRICS)
    radar.check()
    wait_for(page, held, "PUT", 1)
    lyrics.check()
    wait_for(page, held, "PUT", 2)

    give(held, "PUT", 0)  # (the save of the first is answered: in what it says, the second is still off)
    page.wait_for_timeout(300)
    expect(radar).to_be_checked()
    expect(lyrics).to_be_checked()

    give(held, "PUT", 1)
    expect(page.get_by_text("Saved").first).to_be_visible()
    page.wait_for_timeout(300)
    expect(radar).to_be_checked()
    expect(lyrics).to_be_checked()
    app = kept(page)
    assert (app["release_radar"], app["lyrics_lrclib"]) == (True, True)


def test_a_switch_whose_save_failed_goes_back_as_it_was_and_says_why(page, live):
    page.allowed_statuses.add(500)
    page.route(
        "**/api/settings",
        lambda route: route.fulfill(status=500, json={"detail": "The settings could not be written."}) if route.request.method == "PUT" else route.continue_(),
    )
    page.evaluate("location.hash = '#/settings'")
    box = page.get_by_label(RADAR)

    box.check()

    expect(page.locator(".toast.error")).to_contain_text("The settings could not be written.")
    expect(box).not_to_be_checked()
    assert kept(page)["release_radar"] is False
