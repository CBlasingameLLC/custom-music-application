"""Browser tests: a real Chromium driving the real UI against a live server.

They need Playwright and a Chromium for it, and skip themselves otherwise:

    pip install playwright && playwright install chromium

Set MTK_CHROMIUM to the path of an existing Chromium/Chrome binary to use that
instead of Playwright's own download.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("playwright.sync_api")

import uvicorn  # noqa: E402
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.conftest import build_music_dir, build_web  # noqa: E402

TOKEN = "e2e-token"


@pytest.fixture(scope="session")
def _playwright():
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="session")
def browser(_playwright):
    try:
        instance = _playwright.chromium.launch(
            executable_path=os.environ.get("MTK_CHROMIUM") or None,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
    except PlaywrightError as error:
        pytest.skip(f"Chromium is not available for Playwright: {str(error).splitlines()[0]}")
    yield instance
    instance.close()


@pytest.fixture(scope="session")
def webkit(_playwright):
    """Safari's own engine (Playwright's WebKit build), for what only it can say: that a page works in it, and what it sends. Where it is not
    installed (`playwright install webkit`, and on Linux `playwright install-deps webkit`) the tests that use it skip, unless MTK_REQUIRE_WEBKIT
    is set (as it is in CI): then a WebKit that does not start is an error, not a skip nobody sees."""
    try:
        instance = _playwright.webkit.launch()
    except PlaywrightError as error:
        if os.environ.get("MTK_REQUIRE_WEBKIT"):
            raise
        pytest.skip(f"WebKit is not available for Playwright: {str(error).splitlines()[0]}")
    yield instance
    instance.close()


@pytest.fixture
def live(tmp_path: Path):
    """The app on a real port, nothing added yet (a freshly installed app).

    `live.library` is a ready-made folder of playable songs (12 s each) for tests
    that add it through the UI; its Glacier has a synced-lyrics sidecar.
    """
    library = build_music_dir(tmp_path / "music", source="tone_12s.mp3")
    glacier = next(library.rglob("01 - Glacier.mp3"))
    glacier.with_suffix(".lrc").write_text(
        "[00:00.50]Frozen in the dark\n[00:03.00]Glacier, glacier\n[00:06.00]Slowly moving\n", encoding="utf-8"
    )
    web = build_web(tmp_path, None, token=TOKEN)
    server = uvicorn.Server(uvicorn.Config(web.app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        assert time.time() < deadline, "the test server did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield SimpleNamespace(
        base=f"http://127.0.0.1:{port}", url=f"http://127.0.0.1:{port}/?token={TOKEN}", ctx=web.ctx, library=library
    )
    server.should_exit = True
    thread.join(timeout=10)


def signed_in_page(browser, live, init_script: str | None = None, device: dict | None = None):
    """A page already signed in to `live` (a generator for fixtures). Any console error, uncaught exception or failed
    request fails the test. `init_script` runs before the app does: to stand in for the desktop shell's `window.mtk`.
    `device` replaces what the browser context is made with (a phone: see PHONE)."""
    # One locale and time zone everywhere, so the dates and hours the UI prints (and the days a play belongs to) do not
    # depend on the machine the tests run on.
    context = browser.new_context(**{"viewport": {"width": 1360, "height": 860}, "locale": "en-US", "timezone_id": "UTC", **(device or {})})
    if init_script:
        context.add_init_script(init_script)
    page = context.new_page()
    # MTK_E2E_CPU_SLOWDOWN=4 runs the browser as if on a machine four times slower: how to hunt a test that only fails on a busy CI runner.
    if os.environ.get("MTK_E2E_CPU_SLOWDOWN") and browser.browser_type.name == "chromium":  # (the throttle is Chromium's own: WebKit has no DevTools protocol of that kind)
        context.new_cdp_session(page).send("Emulation.setCPUThrottlingRate", {"rate": float(os.environ["MTK_E2E_CPU_SLOWDOWN"])})
    problems: list[str] = []
    # A song without cover art answers its image request with 404; the UI draws a placeholder.
    # A test that deliberately provokes a refusal adds the status to page.allowed_statuses first.
    page.allowed_statuses = {404}

    def expected(text: str) -> bool:
        return any(f"status of {code}" in text for code in page.allowed_statuses)

    page.on("console", lambda m: problems.append(f"console: {m.text}") if m.type == "error" and not expected(m.text) else None)
    page.on("pageerror", lambda e: problems.append(f"uncaught: {e}"))
    page.on(
        "response",
        lambda r: problems.append(f"HTTP {r.status}: {r.url}")
        if r.status >= 400 and "/api/art/" not in r.url and r.status not in page.allowed_statuses
        else None,
    )
    page.goto(live.url)
    page.wait_for_selector("#main")  # (the app has drawn: its sidebar on a computer, its row of tabs on a phone)
    yield page
    context.close()
    assert not problems, "the page reported problems:\n" + "\n".join(problems)


@pytest.fixture
def page(browser, live):
    """A page already signed in to `live`."""
    yield from signed_in_page(browser, live)


# An iPhone as far as the page can tell: its browser's name, its small touch screen and its pixel density (Chromium stands in for Safari).
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
PHONE = {
    "user_agent": IPHONE_UA,
    "viewport": {"width": 390, "height": 844},  # (an installed app: the whole screen is the page)
    "screen": {"width": 390, "height": 844},
    "device_scale_factor": 3,
    "is_mobile": True,
    "has_touch": True,
}


@pytest.fixture
def phone(browser, live):
    """A page already signed in to `live`, on an iPhone."""
    yield from signed_in_page(browser, live, device=PHONE)


def wait_until(page, expression: str, timeout: float = 10.0):
    """Poll a JavaScript expression until it is truthy.

    page.wait_for_function() evaluates a string inside the page, which the app's
    Content-Security-Policy (rightly) forbids, so poll over the DevTools protocol.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = page.evaluate(expression)
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"timed out after {timeout}s waiting for {expression!r} (last value {last!r})")


def api(page, path: str, method: str = "GET", body=None):
    """Call the app's own API from inside the signed-in page."""
    return page.evaluate(
        "([path, method, body]) => fetch('/api' + path, {method, headers: {'Content-Type': 'application/json'}, "
        "body: body === null ? undefined : JSON.stringify(body)}).then(r => r.json())",
        [path, method, body],
    )


def add_library(page, live) -> None:
    """Add the test music folder through the API and wait for the scan, like the folder picker would."""
    result = api(page, "/library/roots", "POST", {"path": str(live.library)})
    wait_until(page, f"fetch('/api/jobs/{result['job']['id']}').then(r => r.json()).then(j => j.status === 'done')", 60)
    page.evaluate("location.hash = '#/songs'")
    page.wait_for_selector(".trow:not(.skeleton)")


def row(page, title: str):
    """The table row for a song, found by title (so tests do not depend on sort order)."""
    return page.locator(".trow", has_text=title).first


def play(page, title: str) -> None:
    """Double-click the song's title, like a user would."""
    row(page, title).locator(".t1").dblclick()


# Counts what the page asks of Web Audio (put before the page runs): contexts made, and elements wired into one.
COUNT_WEB_AUDIO = """
window.__webAudio = { contexts: 0, elementSources: 0 };
for (const name of ['AudioContext', 'webkitAudioContext']) {
  const Base = window[name];
  if (!Base) continue;
  window[name] = class extends Base {
    constructor(...args) { super(...args); window.__webAudio.contexts += 1; }
    createMediaElementSource(element) { window.__webAudio.elementSources += 1; return super.createMediaElementSource(element); }
  };
}
"""

# Remembers what the page registers with the system's media controls (the lock screen, the media keys): for each action the last handler it
# was given, as 'function' or null when it was cleared (`__handlers`), and the handler itself, to be pressed like the lock screen would (`__press`).
WATCH_MEDIA_SESSION = """
window.__handlers = {};
window.__press = {};
const original = navigator.mediaSession.setActionHandler.bind(navigator.mediaSession);
navigator.mediaSession.setActionHandler = (action, handler) => {
  window.__handlers[action] = handler ? 'function' : null;
  window.__press[action] = handler;
  return original(action, handler);
};
"""
