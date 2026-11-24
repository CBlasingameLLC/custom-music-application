"""Signing in with a real browser: the page that asks for the token, the cookie that keeps a device in, and the link that leaves no token behind."""

from __future__ import annotations

from playwright.sync_api import expect

from tests.e2e.conftest import TOKEN

expect.set_options(timeout=10_000)


def _new_page(browser):
    context = browser.new_context(viewport={"width": 1360, "height": 860}, locale="en-US", timezone_id="UTC")
    problems: list[str] = []
    page = context.new_page()
    # (the page that asks for the token is itself a 401, and a browser asks for a favicon: Chromium writes both down as console errors)
    page.on("console", lambda m: problems.append(f"console: {m.text}") if m.type == "error" and not m.text.startswith("Failed to load resource: the server responded with a status of 4") else None)
    page.on("pageerror", lambda e: problems.append(f"uncaught: {e}"))
    return context, page, problems


def test_a_browser_with_no_cookie_is_asked_for_the_token_and_stays_signed_in_once_it_has_given_it(browser, live):
    context, page, problems = _new_page(browser)
    page.goto(live.base + "/")
    expect(page.get_by_role("heading", name="Music Toolkit")).to_be_visible()
    expect(page.locator(".sidebar")).to_have_count(0)  # (the app itself is not there)
    box = page.get_by_label("Access token")
    expect(box).to_have_attribute("type", "password")
    expect(box).to_be_focused()

    box.fill("not-the-token")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("alert")).to_have_text("That is not the token.")
    expect(page.locator(".sidebar")).to_have_count(0)
    assert "not-the-token" not in page.content()  # (what was typed is not shown again)

    page.get_by_label("Access token").fill(TOKEN)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_selector(".sidebar")
    assert page.url == live.base + "/"  # (sent on to the app's own address: nothing of the form in it)

    other = context.new_page()  # (another tab, a bookmark opened later: the cookie is what keeps it in)
    other.goto(live.base + "/")
    other.wait_for_selector(".sidebar")
    context.close()
    assert not problems, problems


def test_the_sign_in_link_signs_a_browser_in_and_the_page_takes_the_token_out_of_its_address(browser, live):
    context, page, problems = _new_page(browser)
    page.goto(live.url)  # http://127.0.0.1:PORT/?token=...
    page.wait_for_selector(".sidebar")
    assert page.url == live.base + "/" and "token" not in page.url
    history = page.evaluate("history.length")
    page.reload()
    page.wait_for_selector(".sidebar")
    assert page.url == live.base + "/" and page.evaluate("history.length") == history  # (the address was replaced, not added to)
    other = context.new_page()  # (what else is in the address stays: a route, and any other part of the query)
    other.goto(live.url + "&x=1#/songs")
    other.wait_for_selector(".sidebar")
    assert other.url == live.base + "/?x=1#/songs"
    context.close()
    assert not problems, problems


def test_the_desktop_window_keeps_its_address_as_it_was_because_the_shell_and_its_checks_find_the_window_by_the_token_in_it(browser, live):
    context = browser.new_context(viewport={"width": 900, "height": 600}, locale="en-US", timezone_id="UTC")
    context.add_init_script("window.mtk = {}")  # (what the shell's preload puts in a page: only its presence matters here)
    page = context.new_page()
    page.goto(live.url)
    page.wait_for_load_state("load")
    assert page.url == live.url and "token=" in page.url
    context.close()


def test_a_wrong_link_shows_the_page_that_asks_and_too_many_wrong_tokens_wait_a_minute(browser, live):
    context, page, problems = _new_page(browser)
    page.goto(live.base + "/?token=wrong-link")
    expect(page.get_by_role("alert")).to_have_text("That is not the token.")
    for _ in range(7):  # (eight wrong tokens in all)
        page.get_by_label("Access token").fill("still-wrong")
        page.get_by_role("button", name="Sign in").click()
        expect(page.get_by_role("alert")).to_have_text("That is not the token.")
    page.get_by_label("Access token").fill(TOKEN)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("alert")).to_contain_text("Too many wrong tokens. Try again in ")
    expect(page.locator(".sidebar")).to_have_count(0)  # (even the right one waits)
    context.close()
    assert not problems, problems
