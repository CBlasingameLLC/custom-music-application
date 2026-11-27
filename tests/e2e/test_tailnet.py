"""Signing in with the Tailscale identity, in a real browser. `tailscale serve` adds the account's login to every request that comes from a
device on the person's own tailnet; the test browsers do the same through the headers of their context, and Chromium sends the real
`Sec-Fetch-Site` of each request, which is what keeps a page of another site from using the identity of the person who has it open."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import PHONE, TOKEN
from tests.e2e.support import Elsewhere, serve_app, stop_app

expect.set_options(timeout=10_000)
YOU = "you@example.com"
SERVE = {"Tailscale-User-Login": YOU, "Tailscale-User-Name": "You", "Tailscale-Headers-Info": "https://tailscale.com/s/serve-headers"}


@pytest.fixture
def listed(tmp_path: Path):
    """A server that signs YOU in with Tailscale."""
    served = serve_app(tmp_path, tailscale_logins=[YOU])
    yield served
    stop_app(served)


@pytest.fixture
def unlisted(tmp_path: Path):
    """A server that signs nobody in with Tailscale (its own settings are the default ones)."""
    served = serve_app(tmp_path)
    yield served
    stop_app(served)


def _device(browser, headers: dict[str, str], **device):
    """A browser on a device whose requests carry `headers`, as everything from a tailnet device does behind `tailscale serve`."""
    context = browser.new_context(**{"viewport": {"width": 1360, "height": 860}, "locale": "en-US", "timezone_id": "UTC", **device})
    context.set_extra_http_headers(headers)
    problems: list[str] = []
    page = context.new_page()
    # (a page that asks for the token is a 401, a song without a cover a 404: Chromium writes those down as console errors)
    page.on("console", lambda m: problems.append(f"console: {m.text}") if m.type == "error" and "status of 4" not in m.text else None)
    page.on("pageerror", lambda e: problems.append(f"uncaught: {e}"))
    return context, page, problems


def _settings(page) -> None:
    page.evaluate("location.hash = '#/settings'")
    page.wait_for_selector('#main[data-route="/settings"]')


def test_a_device_that_tailscale_vouches_for_opens_the_app_with_no_token_and_stays_in_without_the_header(browser, listed):
    context, page, problems = _device(browser, SERVE)
    page.goto(listed.base + "/")
    page.wait_for_selector(".sidebar")
    expect(page.get_by_label("Access token")).to_have_count(0)  # (the page that asks for the token was never shown)
    cookie = next(c for c in context.cookies() if c["name"] == "mtk_session")
    assert cookie["value"].startswith("tailnet.") and TOKEN not in cookie["value"] and YOU not in cookie["value"]
    assert cookie["httpOnly"] and cookie["sameSite"] == "Lax" and cookie["expires"] > time.time() + 80 * 86400  # (90 days)

    context.set_extra_http_headers({})  # (Tailscale is off, or the address is the local one: the cookie alone does it)
    page.reload()
    page.wait_for_selector(".sidebar")
    _settings(page)
    card = page.locator("section.card-panel", has=page.get_by_role("heading", name="How this device signs in"))
    expect(card).to_contain_text("Tailscale signed this device in, and it stays signed in")  # (not "with the token": it is the cookie Tailscale's word bought)
    expect(card).not_to_contain_text("signed in with the token")
    context.set_extra_http_headers(SERVE)
    page.reload()
    page.wait_for_selector(".sidebar")
    _settings(page)
    expect(card).to_contain_text("Tailscale signs this device in as you@example.com. No token is needed")
    expect(card).to_contain_text("take it off tailscale_logins")
    context.close()
    assert not problems, problems


def test_the_installed_app_on_a_phone_opens_straight_into_the_app_signed_in(browser, listed):
    context, page, problems = _device(browser, SERVE, **PHONE)
    page.goto(listed.base + "/")
    page.wait_for_selector(".tabbar")
    expect(page.get_by_label("Access token")).to_have_count(0)
    assert page.evaluate("document.documentElement.dataset.layout") == "phone"
    assert any(c["name"] == "mtk_session" and c["value"].startswith("tailnet.") for c in context.cookies())
    context.close()
    assert not problems, problems


def test_an_account_the_server_does_not_sign_in_is_named_on_the_sign_in_page_and_the_token_still_works(browser, listed):
    context, page, problems = _device(browser, {"Tailscale-User-Login": "mallory@example.com"})
    page.goto(listed.base + "/")
    expect(page.get_by_role("heading", name="Music Toolkit")).to_be_visible()
    expect(page.locator(".sidebar")).to_have_count(0)
    expect(page.locator("p.note", has_text="Tailscale says")).to_contain_text("Tailscale says this device belongs to mallory@example.com. This server does not sign in that account.")
    expect(page.locator("body")).not_to_contain_text(YOU)  # (who is let in is not told to someone who is not)
    assert not any(c["name"] == "mtk_session" for c in context.cookies())

    page.get_by_label("Access token").fill(TOKEN)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_selector(".sidebar")
    _settings(page)
    card = page.locator("section.card-panel", has=page.get_by_role("heading", name="How this device signs in"))
    expect(card).to_contain_text("This device is signed in with the token. Tailscale says it belongs to mallory@example.com, which this server does not sign in.")
    expect(card.locator("pre.line")).to_have_text('"mallory@example.com"')
    context.close()
    assert not problems, problems


def test_a_server_that_signs_nobody_in_offers_the_line_that_would_and_the_card_can_copy_it(browser, unlisted):
    context, page, problems = _device(browser, SERVE)
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=unlisted.base)
    page.goto(unlisted.base + "/")
    expect(page.get_by_role("heading", name="Music Toolkit")).to_be_visible()
    expect(page.locator("pre")).to_have_text('tailscale_logins = ["you@example.com"]')  # (the sign-in page says it too: the token is asked for meanwhile)
    expect(page.locator("body")).to_contain_text("This server can sign this device in through Tailscale, with no token.")
    page.get_by_label("Access token").fill(TOKEN)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_selector(".sidebar")
    _settings(page)
    card = page.locator("section.card-panel", has=page.get_by_role("heading", name="How this device signs in"))
    expect(card).to_contain_text("The server can sign that account in without a token, on every device of yours.")
    expect(card.locator("pre.line")).to_have_text('tailscale_logins = ["you@example.com"]')
    card.get_by_role("button", name="Copy the line").click()
    expect(page.locator(".toasts")).to_contain_text("Line copied")
    assert page.evaluate("navigator.clipboard.readText()") == 'tailscale_logins = ["you@example.com"]'
    context.close()
    assert not problems, problems


def test_a_device_that_tailscale_does_not_name_is_told_to_open_the_servers_own_address(browser, listed):
    context, page, problems = _device(browser, {})
    page.goto(f"{listed.base}/?token={TOKEN}")
    page.wait_for_selector(".sidebar")
    _settings(page)
    card = page.locator("section.card-panel", has=page.get_by_role("heading", name="How this device signs in"))
    expect(card).to_contain_text("Tailscale did not say who it is")
    expect(card).to_contain_text("tailscale serve")
    context.close()
    assert not problems, problems


def test_an_app_without_a_token_has_nothing_to_say_about_signing_in(browser, tmp_path):
    served = serve_app(tmp_path, token=None, tailscale_logins=[YOU])
    try:
        context, page, problems = _device(browser, SERVE)
        page.goto(served.base + "/")
        page.wait_for_selector(".sidebar")
        _settings(page)
        page.wait_for_selector(".settings")
        assert page.evaluate("fetch('/api/session').then(r => r.json()).then(j => j.by)") == "open"  # (the card has what it needs to draw itself)
        page.wait_for_timeout(500)
        expect(page.get_by_role("heading", name="How this device signs in")).to_have_count(0)
        assert not any(c["name"] == "mtk_session" for c in context.cookies())  # (and nothing was given: there is nothing to be signed in to)
        context.close()
        assert not problems, problems
    finally:
        stop_app(served)


def test_the_desktop_window_has_no_use_for_the_card_either(browser, listed):
    context, page, problems = _device(browser, {})
    context.add_init_script("window.mtk = { version: 'test' };")  # (the stand-in for the desktop shell's bridge)
    page.goto(f"{listed.base}/?token={TOKEN}")
    page.wait_for_selector(".sidebar")
    _settings(page)
    page.wait_for_selector(".settings")
    page.wait_for_timeout(500)
    expect(page.get_by_role("heading", name="How this device signs in")).to_have_count(0)
    assert all(path != "/api/session" for _, path, *_ in listed.log)  # (nor did the window ask the server how it signed in)
    context.close()
    assert not problems, problems


def test_a_page_of_another_site_cannot_use_the_identity_of_the_person_whose_browser_it_runs_in(browser, listed):
    context, page, problems = _device(browser, SERVE)
    elsewhere = Elsewhere(listed.base)
    try:
        page.goto(elsewhere.url)
        assert page.evaluate("window.done") is True
        page.wait_for_timeout(300)
    finally:
        elsewhere.stop()
    # (the server saw the identity in both requests, and that the browser said another site made them: and refused both)
    assert listed.log == [
        ("GET", "/api/about", 401, "cross-site", True),
        ("POST", "/api/library/scan", 401, "cross-site", True),
    ]
    assert not any(c["name"] == "mtk_session" for c in context.cookies())  # (nothing was given to the other site's request)

    listed.log.clear()
    page.goto(listed.base + "/")  # (the same browser, the same headers, the app's own address: signed in)
    page.wait_for_selector(".sidebar")
    assert any(c["name"] == "mtk_session" for c in context.cookies())
    assert listed.log and {status for _, _, status, _, _ in listed.log} == {200} and {site for _, _, _, site, _ in listed.log} <= {"same-origin", "none"}
    context.close()
    assert not problems, problems
