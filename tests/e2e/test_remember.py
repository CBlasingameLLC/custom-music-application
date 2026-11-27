"""A device whose browser lost its cookie is signed in again by the key the app kept (remember.py); what the page says when that cannot be done; signing out; a link
from another site (the cookie is Lax); and the worker having the browser make the page's request. In a real browser, against a server that keeps devices signed in
for 90 days."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import TOKEN, wait_until
from tests.e2e.support import Elsewhere, serve_app, stop_app

expect.set_options(timeout=10_000)
KEY = "mtk.remember.v1"
DAY = 86400


@pytest.fixture
def served(tmp_path: Path):
    """A server that keeps devices signed in for 90 days (the app's own default for `mtk serve`)."""
    served = serve_app(tmp_path)
    yield served
    stop_app(served)


def _device(browser, **options):
    """A browser on its own: its own cookies, its own storage."""
    context = browser.new_context(**{"viewport": {"width": 1360, "height": 860}, "locale": "en-US", "timezone_id": "UTC", **options})
    problems: list[str] = []
    page = context.new_page()
    # (a page that asks for the token is a 401, a link to something that is refused a 403: Chromium writes those down as console errors)
    page.on("console", lambda m: problems.append(f"console: {m.text}") if m.type == "error" and "status of 4" not in m.text else None)
    page.on("pageerror", lambda e: problems.append(f"uncaught: {e}"))
    return context, page, problems


def _sign_in(page, served) -> None:
    """Open the sign-in link, and wait until the app has put its key away."""
    page.goto(f"{served.base}/?token={TOKEN}")
    page.wait_for_selector(".sidebar")
    wait_until(page, f"localStorage.getItem('{KEY}') !== null", 10)


def _saved(page) -> dict:
    return page.evaluate(f"JSON.parse(localStorage.getItem('{KEY}'))")


def _calls(served, method: str, path: str) -> int:
    return len([entry for entry in served.log if entry[:2] == (method, path)])


def _signed_out(context) -> bool:
    return not any(cookie["name"] == "mtk_session" for cookie in context.cookies())


# ---------------------------------------------------------------------------------------------------- keeping the key


def test_the_app_keeps_a_key_when_a_device_signs_in_and_renews_it_when_it_is_nearly_out(browser, served):
    context, page, problems = _device(browser)
    _sign_in(page, served)
    saved = _saved(page)
    assert saved["key"].startswith("r1.") and abs(saved["expires"] - (time.time() + 90 * DAY)) < 3600
    assert [entry for entry in served.log if entry[:2] == ("POST", "/api/session/remember")] == [("POST", "/api/session/remember", 200, "same-origin", False)]

    page.evaluate(f"localStorage.setItem('{KEY}', JSON.stringify({{key: 'r1.old', expires: Date.now() / 1000 + 5 * 86400}}))")  # (nearly out)
    page.reload()
    page.wait_for_selector(".sidebar")
    wait_until(page, f"JSON.parse(localStorage.getItem('{KEY}')).key !== 'r1.old'", 10)
    assert _saved(page)["expires"] > time.time() + 80 * DAY

    asked, looked = _calls(served, "POST", "/api/session/remember"), _calls(served, "GET", "/api/session")
    page.evaluate(f"localStorage.setItem('{KEY}', JSON.stringify({{key: 'r1.keep', expires: Date.now() / 1000 + 60 * 86400}}))")  # (plenty left)
    page.reload()
    page.wait_for_selector(".sidebar")
    deadline = time.time() + 10
    while _calls(served, "GET", "/api/session") == looked:
        assert time.time() < deadline, "the app did not ask how this device is signed in"
        time.sleep(0.05)
    page.wait_for_timeout(300)
    assert _saved(page)["key"] == "r1.keep" and _calls(served, "POST", "/api/session/remember") == asked
    context.close()
    assert not problems, problems


# ---------------------------------------------------------------------------------------------------- losing the cookie


def test_a_device_whose_browser_lost_the_cookie_is_signed_in_again_by_the_saved_key_without_anyone_typing(browser, served):
    context, page, problems = _device(browser)
    _sign_in(page, served)
    restores: list[str] = []
    page.on("request", lambda request: restores.append(request.url) if request.url.endswith("/signin/restore") else None)
    context.clear_cookies()
    assert _signed_out(context)
    page.reload()
    page.wait_for_selector(".sidebar")
    assert restores == [served.base + "/signin/restore"] and page.url == served.base + "/"
    cookie = next(c for c in context.cookies() if c["name"] == "mtk_session")
    assert cookie["httpOnly"] and cookie["sameSite"] == "Lax" and cookie["expires"] > time.time() + 80 * DAY
    expect(page.get_by_label("Access token")).to_have_count(0)

    context.clear_cookies()  # (and once more, in a window of its own, as when the app is closed and opened again: nothing of the last window is left)
    again = context.new_page()
    again.goto(served.base + "/")
    again.wait_for_selector(".sidebar")
    assert not _signed_out(context)
    context.close()
    assert not problems, problems


def test_a_saved_key_the_server_does_not_take_is_thrown_away_and_the_page_asks_for_the_token(browser, served):
    context, page, problems = _device(browser)
    _sign_in(page, served)
    key = _saved(page)["key"]
    bent = key[:-1] + ("A" if key[-1] != "A" else "B")
    context.clear_cookies()
    page.evaluate(f"localStorage.setItem('{KEY}', JSON.stringify({{key: {bent!r}, expires: Date.now() / 1000 + 86400}}))")
    page.reload()
    expect(page.get_by_label("Access token")).to_be_visible()
    expect(page.locator("#restore-note")).to_contain_text("no good any more")
    assert page.evaluate(f"localStorage.getItem('{KEY}')") is None and _signed_out(context)

    page.get_by_label("Access token").fill(TOKEN)  # (the token still signs the device in, and the app puts a new key away)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_selector(".sidebar")
    wait_until(page, f"localStorage.getItem('{KEY}') !== null", 10)
    assert _saved(page)["key"].startswith("r1.")
    context.close()
    assert not problems, problems


def test_a_saved_key_that_has_run_out_is_not_even_sent(browser, served):
    context, page, problems = _device(browser)
    _sign_in(page, served)
    sent: list[str] = []
    page.on("request", lambda request: sent.append(request.url) if request.url.endswith("/signin/restore") else None)
    context.clear_cookies()
    page.evaluate(f"localStorage.setItem('{KEY}', JSON.stringify({{key: {_saved(page)['key']!r}, expires: Date.now() / 1000 - 10}}))")
    page.reload()
    expect(page.get_by_label("Access token")).to_be_visible()
    expect(page.locator("#restore-note")).to_contain_text("has run out")
    assert sent == [] and page.evaluate(f"localStorage.getItem('{KEY}')") is None
    context.close()
    assert not problems, problems


def test_a_key_whose_cookie_the_browser_does_not_keep_is_tried_once_and_the_page_does_not_go_round_and_round(browser, served):
    context, page, problems = _device(browser, service_workers="block")  # (so that the browser's own request interception sees every request)
    _sign_in(page, served)
    context.clear_cookies()
    tried: list[str] = []

    def answer(route) -> None:
        tried.append(route.request.url)
        route.fulfill(status=204)  # (a good answer that gives no cookie: a browser that does not keep it)

    context.route("**/signin/restore", answer)
    page.reload()
    expect(page.locator("#restore-note")).to_contain_text("tried a moment ago")
    page.wait_for_timeout(600)
    assert len(tried) == 1
    expect(page.get_by_label("Access token")).to_be_visible()
    assert _saved(page)["key"].startswith("r1.")  # (the key is not thrown away: nothing said it was no good)
    context.close()
    assert not problems, problems


# ---------------------------------------------------------------------------------------------------- signing out


def test_signing_out_of_this_device_forgets_the_key_and_the_cookie_and_nothing_signs_it_in_again(browser, served):
    context, page, problems = _device(browser)
    _sign_in(page, served)
    page.evaluate("location.hash = '#/settings'")
    page.wait_for_selector('#main[data-route="/settings"]')
    card = page.locator("section.card-panel", has=page.get_by_role("heading", name="How this device signs in"))
    expect(card).to_contain_text("It stays signed in for 90 days.")
    expect(card).to_contain_text("the key is good until")
    card.get_by_role("button", name="Sign out of this device").click()
    expect(page.get_by_label("Access token")).to_be_visible()
    assert page.evaluate(f"localStorage.getItem('{KEY}')") is None and _signed_out(context)
    page.reload()
    expect(page.get_by_label("Access token")).to_be_visible()  # (no key: nothing tried)
    expect(page.locator("#restore-note")).to_be_hidden()
    context.close()
    assert not problems, problems


def test_the_card_says_when_this_browser_keeps_no_key(browser, served):
    context, page, problems = _device(browser)
    _sign_in(page, served)
    page.evaluate(f"localStorage.removeItem('{KEY}')")
    page.evaluate("location.hash = '#/settings'")
    page.wait_for_selector('#main[data-route="/settings"]')
    card = page.locator("section.card-panel", has=page.get_by_role("heading", name="How this device signs in"))
    expect(card).to_contain_text("This browser does not keep the key")
    expect(card.get_by_role("button", name="Sign out of this device")).to_be_visible()
    context.close()
    assert not problems, problems


# ---------------------------------------------------------------------------------------------------- a link from another site


def test_a_link_on_another_site_opens_the_app_with_the_device_signed_in_and_a_link_to_its_api_gets_nothing(browser, served):
    context, page, problems = _device(browser)
    _sign_in(page, served)
    elsewhere = Elsewhere(served.base, links=True)
    try:
        served.pages.clear()
        page.goto(elsewhere.url)
        page.click("#app")
        page.wait_for_selector(".sidebar")
        assert [(m, s, site, cookie) for m, s, _, site, cookie in served.pages] == [("GET", 200, "cross-site", True)]  # (what Strict would have left out)

        page.goto(elsewhere.url)
        served.log.clear()
        with page.expect_response(lambda response: response.url.endswith("/api/about")) as asked:
            page.click("#api")
        assert asked.value.status == 403 and "this request came from a page of another site" in page.content()
        assert served.log == [("GET", "/api/about", 403, "cross-site", False)]
    finally:
        elsewhere.stop()
    page.goto(served.base + "/")
    page.wait_for_selector(".sidebar")  # (nothing was taken from the device)
    assert not _signed_out(context)
    context.close()
    assert not problems, problems


# ---------------------------------------------------------------------------------------------------- the worker


def test_the_worker_has_the_browser_make_the_pages_request_and_does_not_make_another(browser, served):
    context, page, problems = _device(browser)
    _sign_in(page, served)
    wait_until(page, "navigator.serviceWorker.controller !== null", 10)
    wait_until(page, "navigator.serviceWorker.ready.then(r => r.navigationPreload.getState()).then(s => s.enabled)", 10)
    served.pages.clear()
    page.reload()
    page.wait_for_selector(".sidebar")
    assert [(m, s, preload, cookie) for m, s, preload, _, cookie in served.pages] == [("GET", 200, "true", True)]  # (one request: the browser's own, started early)
    context.close()
    assert not problems, problems


def test_the_page_that_asks_for_the_token_says_what_the_browser_sent(browser, served):
    context, page, problems = _device(browser)
    page.goto(served.base + "/")
    page.get_by_text("Why does it ask for the token?").click()
    expect(page.locator("details code")).to_have_text("cookie=none site=none mode=navigate dest=document preload=no")
    expect(page.locator("details")).to_contain_text("No sign-in cookie came with this request.")

    _sign_in(page, served)  # (and, with the worker in charge, as the app is opened from its icon after the browser lost everything but the worker)
    wait_until(page, "navigator.serviceWorker.controller !== null", 10)
    page.evaluate(f"localStorage.removeItem('{KEY}')")
    context.clear_cookies()
    page.reload()
    page.get_by_text("Why does it ask for the token?").click()
    expect(page.locator("details code")).to_contain_text("cookie=none")
    expect(page.locator("details code")).to_contain_text("mode=navigate dest=document preload=yes")
    context.close()
    assert not problems, problems
