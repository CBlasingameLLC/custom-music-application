"""The app as an installed app, in a real browser: it is installable, its worker is in charge of the whole app, a server that cannot be
reached is said so (with a way to try again), a server's refusal is not hidden behind that, and the worker keeps nothing."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import expect

from tests.conftest import build_web
from tests.e2e.conftest import TOKEN, wait_until

expect.set_options(timeout=10_000)


def _context(browser, live, *, init_script: str | None = None):
    """A browser signed in to `live`, on its own (so a test can take it offline, or take its cookie away)."""
    context = browser.new_context(viewport={"width": 1360, "height": 860}, locale="en-US", timezone_id="UTC")
    if init_script:
        context.add_init_script(init_script)
    page = context.new_page()
    problems: list[str] = []
    # (a song without cover art is a 404 the page draws a placeholder for; a page that asks for the token is a 401)
    page.on("console", lambda m: problems.append(f"console: {m.text}") if m.type == "error" and "status of 4" not in m.text else None)
    page.on("pageerror", lambda e: problems.append(f"uncaught: {e}"))
    page.goto(live.url)
    page.wait_for_selector(".sidebar")
    return context, page, problems


def _ready(page) -> dict:
    """Wait for the worker to be in charge of the page, and say what it is."""
    return page.evaluate(
        "navigator.serviceWorker.ready.then(r => ({scope: r.scope, script: r.active.scriptURL, state: r.active.state}))"
    )


def test_the_page_is_an_app_a_browser_will_install(browser, live):
    """What Chromium itself says about installing it: the manifest reads without a fault, and there is nothing in the way."""
    context, page, problems = _context(browser, live)
    _ready(page)
    cdp = context.new_cdp_session(page)
    manifest = cdp.send("Page.getAppManifest")
    assert manifest["errors"] == [] and manifest["url"] == live.base + "/static/manifest.webmanifest"
    assert '"display": "standalone"' in manifest["data"]
    # (the only thing in the way is the test browser's own profile: Chromium does not offer to install from a private window)
    assert [e["errorId"] for e in cdp.send("Page.getInstallabilityErrors")["installabilityErrors"]] == ["in-incognito"]
    assert page.evaluate("document.querySelector('meta[name=theme-color]').content") == "#0d0f14"
    context.close()
    assert not problems, problems


def test_the_worker_is_in_charge_of_the_whole_app(browser, live):
    context, page, problems = _context(browser, live)
    got = _ready(page)
    assert got == {"scope": live.base + "/", "script": live.base + "/sw.js", "state": "activated"}
    assert page.evaluate("navigator.serviceWorker.controller === null") in (True, False)  # (in charge from the next load, or at once: it claims)
    wait_until(page, "navigator.serviceWorker.controller !== null", 10)
    context.close()
    assert not problems, problems


def test_the_worker_keeps_nothing(browser, live):
    """No copy of any page or script in the browser's Cache Storage, with a signed-in page, and with a sign-in address (a token in it) opened."""
    context, page, problems = _context(browser, live)
    _ready(page)
    page.goto(live.url + "&x=1")
    page.wait_for_selector(".sidebar")
    assert page.evaluate("caches.keys()") == []
    assert page.evaluate("navigator.storage.estimate().then(e => (e.usageDetails || {}).caches || 0)") == 0
    context.close()
    assert not problems, problems


def test_a_server_that_cannot_be_reached_is_said_so_and_trying_again_gets_the_app_back(browser, live):
    context, page, _problems = _context(browser, live)
    _ready(page)
    wait_until(page, "navigator.serviceWorker.controller !== null", 10)
    context.set_offline(True)
    page.reload()
    expect(page.get_by_role("heading", name="Can't reach your music server")).to_be_visible()
    expect(page.get_by_text("Tailscale")).to_be_visible()
    assert page.locator(".sidebar").count() == 0
    context.set_offline(False)
    page.get_by_role("link", name="Try again").click()
    page.wait_for_selector(".sidebar")  # (the same address, the same cookie: the app, with nobody signing in again)
    context.close()


def test_a_server_that_answers_with_a_refusal_is_not_hidden_behind_that_page(browser, live):
    """A device whose cookie has gone is asked for the token, as without the worker: the server did answer."""
    context, page, _problems = _context(browser, live)
    _ready(page)
    wait_until(page, "navigator.serviceWorker.controller !== null", 10)
    context.clear_cookies()
    page.reload()
    expect(page.get_by_label("Access token")).to_be_visible()
    assert "Can't reach" not in page.content()
    page.get_by_label("Access token").fill(TOKEN)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_selector(".sidebar")
    context.close()


def test_the_desktop_window_does_not_register_a_worker(browser, live):
    """The shell's own window (it has `window.mtk`) is on a port of its own each time and has no use for one."""
    context, page, problems = _context(browser, live, init_script="window.mtk = { version: 'test' };")
    page.wait_for_load_state("load")
    page.wait_for_timeout(1000)
    assert page.evaluate("navigator.serviceWorker.getRegistrations().then(r => r.length)") == 0
    context.close()
    assert not problems, problems


# ---------------------------------------------------------------------------------------------------- the help in Settings


def test_settings_tells_how_to_put_the_app_on_a_phone_and_a_mac(browser, live):
    context, page, problems = _context(browser, live)
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live.base)
    page.evaluate("location.hash = '#/settings'")
    card = page.locator(".card-panel", has_text="Use it on your phone or Mac")
    expect(card).to_be_visible()
    box = card.get_by_label("This server's address")
    assert box.input_value() == live.base and box.get_attribute("readonly") is not None
    expect(card.get_by_role("heading", name="iPhone or iPad")).to_be_visible()
    expect(card.get_by_role("heading", name="Mac", exact=True)).to_be_visible()
    expect(card).to_contain_text("Add to Home Screen")
    expect(card).to_contain_text("Add to Dock")
    expect(card).to_contain_text("only on the computer that runs the app")  # (this test's address is the computer's own)
    assert "tailscale serve" not in card.inner_text()  # (that is for an address that is http and not the computer's own)
    card.get_by_role("button", name="Copy").click()
    expect(page.locator(".toast")).to_contain_text("Address copied")
    assert page.evaluate("navigator.clipboard.readText()") == live.base
    context.close()
    assert not problems, problems


def test_the_installed_app_is_not_told_how_to_install_itself(browser, live):
    context, page, problems = _context(browser, live, init_script="Object.defineProperty(navigator, 'standalone', { value: true });")
    page.evaluate("location.hash = '#/settings'")
    card = page.locator(".card-panel", has_text="Use it on your phone or Mac")
    expect(card).to_contain_text("This is the installed app")
    assert page.get_by_role("heading", name="iPhone or iPad").count() == 0 and "Add to Home Screen" not in card.inner_text()
    expect(card.get_by_label("This server's address")).to_be_visible()  # (the address is still there, for a second device)
    context.close()
    assert not problems, problems


def test_the_desktop_window_has_no_use_for_it(browser, live):
    context, page, problems = _context(browser, live, init_script="window.mtk = { version: 'test' };")
    page.evaluate("location.hash = '#/settings'")
    page.wait_for_selector(".settings")
    assert page.locator(".card-panel", has_text="Use it on your phone or Mac").count() == 0
    context.close()
    assert not problems, problems


def test_the_colour_of_the_frame_on_a_phone_follows_the_theme(browser, live):
    context, page, problems = _context(browser, live)
    colour = "document.querySelector('meta[name=theme-color]').content"
    assert page.evaluate(colour) == "#0d0f14"
    page.get_by_role("button", name="Switch to light theme").click()
    assert page.evaluate(colour) == "#f5f6fb"
    page.reload()
    page.wait_for_selector(".sidebar")
    page.get_by_role("button", name="Switch to dark theme").click()
    assert page.evaluate(colour) == "#0d0f14"
    context.close()
    assert not problems, problems


def test_an_address_that_is_http_and_not_the_computers_own_is_told_what_it_lacks(browser, tmp_path: Path):
    """A server reached by a name of its own (a LAN name, a Tailscale name) but not over https: a phone can put it on its Home Screen, but it is a
    bookmark there. The test's server answers to `musicpi.test`, and a browser of its own is told that the name is this computer."""
    web = build_web(tmp_path, None, token=TOKEN, allowed_hosts=["musicpi.test"])
    server = uvicorn.Server(uvicorn.Config(web.app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        assert time.time() < deadline, "the test server did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    chromium = browser.browser_type.launch(executable_path=os.environ.get("MTK_CHROMIUM") or None, args=["--host-resolver-rules=MAP musicpi.test 127.0.0.1"])
    try:
        page = chromium.new_page(viewport={"width": 1360, "height": 860})
        page.goto(f"http://musicpi.test:{port}/?token={TOKEN}#/settings")
        card = page.locator(".card-panel", has_text="Use it on your phone or Mac")
        expect(card).to_be_visible()
        expect(card.get_by_label("This server's address")).to_have_value(f"http://musicpi.test:{port}")
        expect(card).to_contain_text("This address is http, not https")
        expect(card).to_contain_text("tailscale serve")
        assert "only on the computer that runs the app" not in card.inner_text()
        expect(card).to_contain_text("Add to Home Screen")  # (the steps are still given)
    finally:
        chromium.close()
        server.should_exit = True
        thread.join(timeout=10)
