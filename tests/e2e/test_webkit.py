"""The app in Safari's own engine (Playwright's WebKit build), as on an iPhone. Chromium stands in for Safari everywhere else in these tests; this
is the one place that asks the engine itself: that the app starts and every screen fits, that signing in works, that the worker installs and says
when the server cannot be reached, that a device whose cookie went is signed in again by the key the app kept, that a link from another site opens the app signed in,
that a song converted on the way is answered to the questions Safari asks first (the first bytes, a seek, the whole), and, because the protection of a Tailscale
sign-in rests on it, that Safari says where each request came from.

It skips where WebKit is not installed (`playwright install webkit`, and on Linux `playwright install-deps webkit`), unless MTK_REQUIRE_WEBKIT is set."""

from __future__ import annotations

import base64
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import TOKEN, add_library, api, signed_in_page, wait_until
from tests.e2e.support import Elsewhere, serve_app, stop_app
from tests.e2e.test_phone import OVERFLOW, ROUTES

expect.set_options(timeout=10_000)
YOU = "you@example.com"
SERVE = {"Tailscale-User-Login": YOU, "Tailscale-User-Name": "You", "Tailscale-Headers-Info": "https://tailscale.com/s/serve-headers"}


@pytest.fixture
def iphone(_playwright) -> dict:
    """What Playwright knows of an iPhone: Safari's name, the small touch screen, the pixel density."""
    return dict(_playwright.devices["iPhone 15"])


def _device(webkit, iphone, headers: dict[str, str] | None = None):
    context = webkit.new_context(**{**iphone, "locale": "en-US", "timezone_id": "UTC"})
    if headers:
        context.set_extra_http_headers(headers)
    problems: list[str] = []
    page = context.new_page()
    # (a page that asks for the token is a 401, a song without a cover a 404: they are written down as console errors)
    page.on("console", lambda m: problems.append(f"console: {m.text}") if m.type == "error" and "status of 4" not in m.text and "401" not in m.text and "403" not in m.text else None)
    page.on("pageerror", lambda e: problems.append(f"uncaught: {e}"))
    return context, page, problems


def test_every_screen_of_the_app_starts_and_fits_the_width_of_an_iphone_in_safaris_engine(webkit, live, iphone):
    pages = signed_in_page(webkit, live, device=iphone)  # (fails the test at its end if the page wrote an error in the console, or a request failed)
    page = next(pages)
    assert page.evaluate("document.documentElement.dataset.layout") == "phone" and page.locator(".tabbar").count() == 1
    add_library(page, live)
    api(page, "/playlists", "POST", {"name": "Evening", "track_ids": [1, 2, 3]})
    album = api(page, "/albums?limit=1")["items"][0]["key"]
    problems = []
    for route in ROUTES + [f"/album/{album}", "/artist/Aurora%20Vale", "/playlist/1"]:
        page.evaluate(f"location.hash = '#{route}'")
        page.wait_for_selector(f'#main[data-route="{route}"]')
        page.wait_for_timeout(250)
        got = page.evaluate(OVERFLOW)
        if got["scroll"] > got["view"] or got["page"] > got["window"] or got["offenders"]:
            problems.append(f"{route}: {got}")
    assert not problems, "pages wider than the phone:\n" + "\n".join(problems)
    next(pages, None)


def test_the_page_that_asks_for_the_token_signs_a_device_in_in_safaris_engine(webkit, live, iphone):
    context, page, problems = _device(webkit, iphone)
    page.goto(live.base + "/")
    expect(page.get_by_role("heading", name="Music Toolkit")).to_be_visible()
    expect(page.locator(".tabbar")).to_have_count(0)
    page.get_by_label("Access token").fill("not-the-token")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("alert")).to_have_text("That is not the token.")
    page.get_by_label("Access token").fill(TOKEN)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_selector(".tabbar")
    assert page.url == live.base + "/"
    cookie = next(c for c in context.cookies() if c["name"] == "mtk_session")
    assert cookie["httpOnly"] and cookie["sameSite"] == "Lax"
    context.close()
    assert not problems, problems


def test_the_worker_installs_in_safaris_engine_and_a_server_that_cannot_be_reached_is_said_so(webkit, live, iphone):
    context, page, problems = _device(webkit, iphone)
    page.goto(live.url)
    page.wait_for_selector(".tabbar")
    got = page.evaluate("navigator.serviceWorker.ready.then(r => ({scope: r.scope, script: r.active.scriptURL, state: r.active.state}))")
    assert got == {"scope": live.base + "/", "script": live.base + "/sw.js", "state": "activated"}
    wait_until(page, "navigator.serviceWorker.controller !== null", 10)
    context.set_offline(True)
    page.reload()
    expect(page.get_by_role("heading", name="Can't reach your music server")).to_be_visible()
    context.set_offline(False)
    page.get_by_role("link", name="Try again").click()
    page.wait_for_selector(".tabbar")  # (the same address, the same cookie: the app, with nobody signing in again)
    context.close()
    assert not problems, problems


@pytest.fixture
def listed(tmp_path: Path):
    served = serve_app(tmp_path, tailscale_logins=[YOU])
    yield served
    stop_app(served)


def test_a_device_tailscale_vouches_for_is_signed_in_in_safaris_engine_and_stays_so_without_the_header(webkit, listed, iphone):
    context, page, problems = _device(webkit, iphone, SERVE)
    page.goto(listed.base + "/")
    page.wait_for_selector(".tabbar")
    expect(page.get_by_label("Access token")).to_have_count(0)
    cookie = next(c for c in context.cookies() if c["name"] == "mtk_session")
    assert cookie["value"].startswith("tailnet.") and cookie["httpOnly"] and cookie["sameSite"] == "Lax" and TOKEN not in cookie["value"]
    context.set_extra_http_headers({})
    page.reload()
    page.wait_for_selector(".tabbar")  # (the cookie alone)
    context.close()
    assert not problems, problems
    # What Safari said of each request it made, as the server saw it: an address typed or tapped is "none", the page's own requests "same-origin".
    assert listed.log and {site for _, _, _, site, _ in listed.log} <= {"same-origin", "none"}, listed.log
    assert {status for _, _, status, _, _ in listed.log} == {200}


def test_safari_says_a_request_came_from_another_site_and_so_such_a_page_cannot_use_the_identity_of_the_person_whose_browser_it_runs_in(webkit, listed, iphone):
    context, page, problems = _device(webkit, iphone, SERVE)
    elsewhere = Elsewhere(listed.base)
    try:
        page.goto(elsewhere.url)
        assert page.evaluate("window.done") is True
        page.wait_for_timeout(300)
    finally:
        elsewhere.stop()
    assert listed.log == [
        ("GET", "/api/about", 401, "cross-site", True),
        ("POST", "/api/library/scan", 401, "cross-site", True),
    ], listed.log
    assert not any(c["name"] == "mtk_session" for c in context.cookies())
    listed.log.clear()
    page.goto(listed.base + "/")
    page.wait_for_selector(".tabbar")
    assert listed.log and {status for _, _, status, _, _ in listed.log} == {200}
    context.close()
    assert not problems, problems


# ---------------------------------------------------------------------------------------------------- staying signed in

KEY = "mtk.remember.v1"


@pytest.fixture
def kept(tmp_path: Path):
    """A server that keeps devices signed in for 90 days."""
    served = serve_app(tmp_path)
    yield served
    stop_app(served)


def _signed_in(page, served) -> None:
    page.goto(f"{served.base}/?token={TOKEN}")
    page.wait_for_selector(".tabbar")
    wait_until(page, f"localStorage.getItem('{KEY}') !== null", 10)


def test_a_device_whose_safari_lost_the_cookie_is_signed_in_again_by_the_key_the_app_kept(webkit, kept, iphone):
    context, page, problems = _device(webkit, iphone)
    _signed_in(page, kept)
    context.clear_cookies()
    page.reload()
    page.wait_for_selector(".tabbar")  # (nobody typed: the key signed the device in)
    cookie = next(c for c in context.cookies() if c["name"] == "mtk_session")
    assert cookie["httpOnly"] and cookie["sameSite"] == "Lax" and cookie["expires"] > time.time() + 80 * 86400
    expect(page.get_by_label("Access token")).to_have_count(0)
    context.close()
    assert not problems, problems


def test_safari_opens_the_app_signed_in_from_a_link_on_another_site_and_a_link_to_its_api_gets_nothing(webkit, kept, iphone):
    context, page, problems = _device(webkit, iphone)
    _signed_in(page, kept)
    wait_until(page, "navigator.serviceWorker.controller !== null", 10)  # (the worker is in charge, as it is when the app is opened from its icon)
    elsewhere = Elsewhere(kept.base, links=True)
    try:
        kept.pages.clear()
        page.goto(elsewhere.url)
        page.click("#app")
        page.wait_for_selector(".tabbar")
        assert [(m, s, site, cookie) for m, s, _, site, cookie in kept.pages] == [("GET", 200, "cross-site", True)], kept.pages
        page.goto(elsewhere.url)
        kept.log.clear()
        with page.expect_response(lambda response: response.url.endswith("/api/about")) as asked:
            page.click("#api")
        assert asked.value.status == 403
        assert kept.log == [("GET", "/api/about", 403, "cross-site", False)], kept.log
    finally:
        elsewhere.stop()
    context.close()
    assert not problems, problems


def test_the_worker_in_safaris_engine_has_the_browser_make_the_pages_request_where_the_engine_can_and_never_asks_twice(webkit, kept, iphone):
    context, page, problems = _device(webkit, iphone)
    _signed_in(page, kept)
    wait_until(page, "navigator.serviceWorker.controller !== null", 10)
    can = page.evaluate("'navigationPreload' in ServiceWorkerRegistration.prototype")
    if can:
        wait_until(page, "navigator.serviceWorker.ready.then(r => r.navigationPreload.getState()).then(s => s.enabled)", 10)
    kept.pages.clear()
    page.reload()
    page.wait_for_selector(".tabbar")
    assert [(m, s, preload, cookie) for m, s, preload, _, cookie in kept.pages] == [("GET", 200, "true" if can else "", True)], (can, kept.pages)
    context.close()
    assert not problems, problems


PROBE = """async ({ id, range }) => {
  const r = await fetch(`/api/tracks/${id}/stream?kbps=32`, { headers: range ? { Range: range } : {} });
  const bytes = new Uint8Array(await r.arrayBuffer());
  let text = '';
  for (let i = 0; i < bytes.length; i += 8192) text += String.fromCharCode(...bytes.subarray(i, i + 8192));
  const h = (name) => r.headers.get(name);
  return { status: r.status, type: h('content-type'), range: h('content-range'), ranges: h('accept-ranges'), length: h('content-length'), body: btoa(text) };
}"""


def _asked(page, track: int, range: str | None = None) -> dict:
    """What the page was told when it asked for a song at 32 kbps (`range`: the part it asked for), with the bytes it was given."""
    got = page.evaluate(PROBE, {"id": track, "range": range})
    got["body"] = base64.b64decode(got["body"])
    return got


def _made(cache: Path, before: set[Path]) -> Path:
    """The file the server kept of the song asked for last (it is kept a moment after the song is whole)."""
    for _ in range(100):
        new = set(cache.glob("*-32.mp3")) - before
        if new:
            [made] = new
            return made
        time.sleep(0.1)
    raise AssertionError(f"no song was kept in {cache}: {sorted(cache.glob('*'))}")


def test_safaris_engine_is_answered_with_the_real_length_when_it_asks_for_the_first_bytes_of_a_song_that_is_converted_on_the_way(webkit, live, iphone):
    """Safari asks for the first byte or two of a song before it plays anything, and wants the whole length in the answer. A song that is converted on the way
    is not made yet when it asks, so the server holds that question until the song is whole and answers it from the kept file with its real length; a seek
    is a part of the kept file; a player that asks for the whole song at once is handed it as it is made. Those questions are asked with fetch(), which goes
    through the engine's own network stack, and judged by what the page was told. Playing is not asked of the engine: Playwright's WebKit on Linux cannot
    load any audio (a page with nothing but an audio element of a `data:` address crashes it), so that a converted song plays is the Chromium test's."""
    from musictoolkit.transcode import Transcoder

    if not Transcoder().available():
        pytest.skip("no ffmpeg with an MP3 encoder (sudo apt install ffmpeg)")
    context, page, problems = _device(webkit, iphone)
    page.goto(live.url)
    page.wait_for_selector(".tabbar")
    add_library(page, live)
    first, second = (item["id"] for item in api(page, "/tracks?limit=5&sort=title")["items"][:2])
    cache = live.ctx.data_dir / "cache" / "transcode"
    assert not cache.exists() or not list(cache.glob("*-32.mp3"))  # (nothing is made yet when the first question comes)

    probe = _asked(page, first, "bytes=0-1")
    made = _made(cache, set())
    kept, size = made.read_bytes(), made.stat().st_size
    assert size > 10_000 and kept[:3] != b"ID3"
    assert (probe["status"], probe["type"], probe["ranges"], probe["length"]) == (206, "audio/mpeg", "bytes", "2"), probe
    assert probe["range"] == f"bytes 0-1/{size}" and probe["body"] == kept[:2], (probe, size)

    seek = _asked(page, first, "bytes=10000-10099")
    assert (seek["status"], seek["range"], seek["length"]) == (206, f"bytes 10000-10099/{size}", "100"), seek
    assert seek["body"] == kept[10000:10100]

    whole = _asked(page, first)
    assert (whole["status"], whole["type"], whole["ranges"], whole["length"], whole["range"]) == (200, "audio/mpeg", "bytes", str(size), None), whole
    assert whole["body"] == kept

    live_answer = _asked(page, second)  # (a player that asks for the whole song at once: no length is known yet, and none is promised)
    assert (live_answer["status"], live_answer["type"], live_answer["ranges"], live_answer["length"], live_answer["range"]) == (200, "audio/mpeg", "none", None, None), live_answer
    assert live_answer["body"] == _made(cache, {made}).read_bytes()
    context.close()
    assert not problems, problems
