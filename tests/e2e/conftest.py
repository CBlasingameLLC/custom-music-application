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
def browser():
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(
                executable_path=os.environ.get("MTK_CHROMIUM") or None,
                args=["--autoplay-policy=no-user-gesture-required"],
            )
        except PlaywrightError as error:
            pytest.skip(f"Chromium is not available for Playwright: {str(error).splitlines()[0]}")
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


@pytest.fixture
def page(browser, live):
    """A page already signed in to `live`. Any console error, uncaught exception or failed request fails the test."""
    context = browser.new_context(viewport={"width": 1360, "height": 860})
    page = context.new_page()
    problems: list[str] = []
    # A song without cover art answers its image request with 404; the UI draws a placeholder.
    page.on(
        "console",
        lambda m: problems.append(f"console: {m.text}") if m.type == "error" and "status of 404" not in m.text else None,
    )
    page.on("pageerror", lambda e: problems.append(f"uncaught: {e}"))
    page.on(
        "response",
        lambda r: problems.append(f"HTTP {r.status}: {r.url}") if r.status >= 400 and "/api/art/" not in r.url else None,
    )
    page.goto(live.url)
    page.wait_for_selector(".sidebar")
    yield page
    context.close()
    assert not problems, "the page reported problems:\n" + "\n".join(problems)


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
