"""Signing in with the Tailscale identity, in a real browser. `tailscale serve` adds the account's login to every request that comes from a
device on the person's own tailnet; the test browsers do the same through the headers of their context, and Chromium sends the real
`Sec-Fetch-Site` of each request, which is what keeps a page of another site from using the identity of the person who has it open."""

from __future__ import annotations

import http.server
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.conftest import build_web
from tests.e2e.conftest import PHONE, TOKEN

expect.set_options(timeout=10_000)
YOU = "you@example.com"
SERVE = {"Tailscale-User-Login": YOU, "Tailscale-User-Name": "You", "Tailscale-Headers-Info": "https://tailscale.com/s/serve-headers"}


class _Recorder:
    """Writes down what the server was sent and what it answered, for each call to the API: (method, path, status, Sec-Fetch-Site, whether
    the request carried the login Tailscale writes). A browser does not let a page read the headers it adds itself, so the server does."""

    def __init__(self, app, log: list) -> None:
        self.app, self.log = app, log

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        headers = {key.decode(): value.decode() for key, value in scope["headers"]}

        async def sending(message):
            if message["type"] == "http.response.start":
                self.log.append((scope["method"], scope["path"], message["status"], headers.get("sec-fetch-site", ""), "tailscale-user-login" in headers))
            await send(message)

        await self.app(scope, receive, sending)


def _serve(tmp_path: Path, **options) -> SimpleNamespace:
    web = build_web(tmp_path, None, token=TOKEN, login_days=90, **options)
    log: list = []
    web.app.add_middleware(_Recorder, log=log)
    server = uvicorn.Server(uvicorn.Config(web.app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        assert time.time() < deadline, "the test server did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    return SimpleNamespace(base=f"http://127.0.0.1:{port}", port=port, server=server, thread=thread, ctx=web.ctx, log=log)


def _stop(served: SimpleNamespace) -> None:
    served.server.should_exit = True
    served.thread.join(timeout=10)


@pytest.fixture
def listed(tmp_path: Path):
    """A server that signs YOU in with Tailscale."""
    served = _serve(tmp_path, tailscale_logins=[YOU])
    yield served
    _stop(served)


@pytest.fixture
def unlisted(tmp_path: Path):
    """A server that signs nobody in with Tailscale (its own settings are the default ones)."""
    served = _serve(tmp_path)
    yield served
    _stop(served)


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
    assert cookie["httpOnly"] and cookie["sameSite"] == "Strict" and cookie["expires"] > time.time() + 80 * 86400  # (90 days)

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
    expect(page.locator("p.note").first).to_contain_text("Tailscale says this device belongs to mallory@example.com. This server does not sign in that account.")
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


class _Elsewhere:
    """A page of another site (another host name, as far as the browser can tell) that asks the server for things."""

    def __init__(self, target: str) -> None:
        body = f"""<!doctype html><title>elsewhere</title><script>
        const target = {target!r};
        window.done = (async () => {{
          await fetch(target + '/api/about', {{ mode: 'no-cors', credentials: 'include' }});
          await fetch(target + '/api/library/scan', {{ method: 'POST', mode: 'no-cors', credentials: 'include' }});
          return true;
        }})();
        </script>""".encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://localhost:{self.http.server_address[1]}/"  # (`localhost`, not 127.0.0.1: a different site)

    def stop(self) -> None:
        self.http.shutdown()
        self.thread.join(timeout=5)


def test_a_page_of_another_site_cannot_use_the_identity_of_the_person_whose_browser_it_runs_in(browser, listed):
    context, page, problems = _device(browser, SERVE)
    elsewhere = _Elsewhere(listed.base)
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
