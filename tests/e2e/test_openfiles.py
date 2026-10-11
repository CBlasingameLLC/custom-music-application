"""What the page does with the desktop shell: songs opened with the app, files dropped on the window, what is playing
(for the tray), and the close button's setting. The shell is stood in for by a small `window.mtk` made before the app runs;
what the real shell does with these calls is checked by scripts/smoke_desktop.py and desktop/test."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.conftest import make_track
from tests.e2e.conftest import add_library, api, play, signed_in_page, wait_until

expect.set_options(timeout=10_000)

SHELL = """
(() => {
  const listeners = new Set();
  const shell = { opened: %(opened)s, paths: {}, nowPlaying: [], closeActions: [], quits: 0, info: { tray: %(tray)s, closeAction: 'tray_if_playing', hidden: false } };
  window.__shell = shell;
  shell.send = (files) => listeners.forEach((listener) => listener(files));
  window.mtk = {
    takeOpenFiles: async () => shell.opened.splice(0),
    onOpenFiles: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
    pathForFile: (file) => shell.paths[file.name] || null,
    setNowPlaying: async (info) => { shell.nowPlaying.push(info); return null; },
    setCloseAction: async (value) => { shell.closeActions.push(value); return value; },
    desktopInfo: async () => shell.info,
    quit: async () => { shell.quits += 1; return null; },
    mediaKeys: async () => ({ playpause: true, next: true, previous: true, stop: true }),
    onMediaKey: () => () => {},
  };
})();
"""


@pytest.fixture
def shell(browser, live):
    """A page that has a stand-in desktop shell: `shell(opened=[...paths the app was opened with], tray=True)`."""
    started = []

    def open_page(opened=(), tray=True):
        script = SHELL % {"opened": json.dumps([str(p) for p in opened]), "tray": "true" if tray else "false"}
        generator = signed_in_page(browser, live, script)
        started.append(generator)
        return next(generator)

    yield open_page
    for generator in started:  # finishes each page: closes it and fails the test if the page reported a problem
        for _ in generator:
            pass


@pytest.fixture
def song(tmp_path: Path) -> Path:
    return make_track(tmp_path / "elsewhere", "far.mp3", source="tone_12s.mp3", title="Far Away", artist="Someone", album="Somewhere")


def test_a_song_the_app_was_opened_with_is_added_to_the_library_and_played(shell, song) -> None:
    page = shell(opened=[song])
    expect(page.locator(".playerbar .pb-text .t1")).to_have_text("Far Away")
    expect(page.locator(".playerbar").get_by_role("button", name="Pause")).to_be_visible()
    assert api(page, "/about")["tracks"] == 1
    page.evaluate("location.hash = '#/songs'")
    expect(page.locator(".trow:not(.skeleton)")).to_have_count(1)


def test_a_song_opened_while_the_app_is_running_plays_at_once(shell, song) -> None:
    page = shell()
    page.evaluate("window.__shell.send(%s)" % json.dumps([str(song)]))

    expect(page.locator(".playerbar .pb-text .t1")).to_have_text("Far Away")
    expect(page.locator(".playerbar").get_by_role("button", name="Pause")).to_be_visible()


def test_files_dropped_on_the_window_are_played_and_a_hint_shows_while_dragging(shell, song) -> None:
    page = shell()
    page.evaluate("window.__shell.paths = %s" % json.dumps({"dropped.mp3": str(song)}))

    page.evaluate(
        """() => {
          const data = new DataTransfer();
          data.items.add(new File(['x'], 'dropped.mp3', { type: 'audio/mpeg' }));
          const fire = (type) => window.dispatchEvent(new DragEvent(type, { dataTransfer: data, bubbles: true, cancelable: true }));
          fire('dragenter');
          window.__hinted = document.body.classList.contains('dropping');
          fire('dragover');
          window.__dropped = () => fire('drop');
        }"""
    )
    assert page.evaluate("window.__hinted") is True
    page.evaluate("window.__dropped()")

    expect(page.locator(".playerbar .pb-text .t1")).to_have_text("Far Away")
    assert page.evaluate("document.body.classList.contains('dropping')") is False


def test_something_dragged_that_is_not_a_file_is_left_alone(shell) -> None:
    page = shell()

    page.evaluate(
        """() => {
          const data = new DataTransfer();
          data.setData('text/plain', 'just some words');
          window.dispatchEvent(new DragEvent('dragenter', { dataTransfer: data, bubbles: true, cancelable: true }));
        }"""
    )

    assert page.evaluate("document.body.classList.contains('dropping')") is False


def test_what_cannot_be_opened_is_explained(shell, tmp_path: Path) -> None:
    page = shell()
    notes = tmp_path / "notes.txt"
    notes.write_text("not music")

    page.evaluate("window.__shell.send(%s)" % json.dumps([str(notes)]))

    expect(page.get_by_text("Nothing to play: not an audio file")).to_be_visible()
    expect(page.locator(".playerbar .pb-text .t1")).to_have_count(0)
    assert api(page, "/about")["tracks"] == 0, "nothing was added to the library"


def test_the_shell_is_told_what_is_playing_for_the_tray(shell, live) -> None:
    page = shell()
    add_library(page, live)

    play(page, "Glacier")
    wait_until(page, "window.__shell.nowPlaying.some((n) => n.playing)", 10)
    assert page.evaluate("window.__shell.nowPlaying.at(-1)") == {"title": "Glacier", "artist": "Aurora Vale", "playing": True}
    count = page.evaluate("window.__shell.nowPlaying.length")
    page.wait_for_timeout(900)  # the position moves several times meanwhile
    assert page.evaluate("window.__shell.nowPlaying.length") == count, "the clock ticking is not news: only a change of song or of playing is sent"

    page.locator(".playerbar").get_by_role("button", name="Pause").click()
    wait_until(page, "window.__shell.nowPlaying.at(-1).playing === false", 10)
    assert page.evaluate("window.__shell.nowPlaying.at(-1)") == {"title": "Glacier", "artist": "Aurora Vale", "playing": False}


def test_the_close_button_choice_is_kept_by_the_app_and_passed_to_the_shell(shell) -> None:
    page = shell()
    page.evaluate("location.hash = '#/settings'")

    expect(page.get_by_role("heading", name="Window and tray")).to_be_visible()
    choice = page.get_by_label("When I close the window")
    expect(choice).to_have_value("tray_if_playing")
    choice.select_option("tray")

    wait_until(page, "window.__shell.closeActions.length === 1", 10)
    assert page.evaluate("window.__shell.closeActions") == ["tray"]
    assert api(page, "/settings")["app"]["close_action"] == "tray"

    page.get_by_role("button", name="Quit Music Toolkit").click()
    wait_until(page, "window.__shell.quits === 1", 10)


def test_diagnostics_say_whether_there_is_a_tray_icon(shell) -> None:
    page = shell(tray=False)
    page.evaluate("location.hash = '#/settings'")
    expect(page.get_by_role("heading", name="Window and tray")).to_be_visible()
    expect(page.get_by_text("This computer shows no tray icon, so closing the window always quits the app.").first).to_be_visible()

    page.get_by_role("button", name="Run diagnostics").click()

    expect(page.locator(".diag", has_text="Tray icon")).to_contain_text("closing the window always quits")


def test_a_plain_browser_has_no_window_card_and_no_drop_hint(page) -> None:
    page.evaluate("location.hash = '#/settings'")
    expect(page.get_by_role("heading", name="Music library")).to_be_visible()
    expect(page.get_by_role("heading", name="Window and tray")).to_have_count(0)

    page.evaluate(
        """() => {
          const data = new DataTransfer();
          data.items.add(new File(['x'], 'a.mp3'));
          window.dispatchEvent(new DragEvent('dragenter', { dataTransfer: data, bubbles: true, cancelable: true }));
        }"""
    )
    assert page.evaluate("document.body.classList.contains('dropping')") is False, "a browser cannot say where a dropped file is, so it does not offer"
