"""Playing on an iPhone, as far as a test browser can say.

Safari stops the Web Audio of a page when the screen locks, and the music stops with it; an <audio> element carries on. So on an iPhone or
an iPad the player never builds the Web Audio graph and plays everything through one element, loading the next song into it when a song
ends. These tests pretend to be an iPhone (its name for itself, its touch screen) and count what the page asks of Web Audio.

What they cannot say is what Safari does with the screen locked: that is for a person with an iPhone (see the notes of the release).
"""

from __future__ import annotations

from contextlib import contextmanager

from playwright.sync_api import expect

from tests.e2e.conftest import COUNT_WEB_AUDIO, PHONE, WATCH_MEDIA_SESSION, add_library, signed_in_page, wait_until

expect.set_options(timeout=10_000)

PLAYER = "import('/static/js/player.js')"


@contextmanager
def opened(browser, live, **options):
    """A page signed in to `live` (see signed_in_page), and when the block has finished without a failure, its own check that the page
    reported no problem."""
    pages = signed_in_page(browser, live, **options)
    yield next(pages)
    next(pages, None)


def web_audio(page) -> dict:
    return page.evaluate("window.__webAudio")


def deck_state(page) -> dict:
    return page.evaluate(f"{PLAYER}.then((m) => m.deckState())")


def play_songs(page, first: int = 0) -> None:
    """Queue the library as the page lists it, and play from `first`: what a tap on a row does."""
    page.evaluate(
        f"""{PLAYER}.then(async (m) => {{
          const {{ items }} = await fetch('/api/tracks?limit=50&sort=title').then((r) => r.json());
          await m.playQueue(items, {first});
        }})"""
    )
    wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing && m.deckState().decks.some((d) => d.time > 0.2))", 15)


def test_on_an_iphone_the_songs_are_played_by_the_plain_element_and_web_audio_is_never_used(browser, live):
    with opened(browser, live, init_script=COUNT_WEB_AUDIO, device=PHONE) as page:
        add_library(page, live)
        assert page.evaluate(f"{PLAYER}.then((m) => m.PLAIN)") is True
        play_songs(page)
        state = deck_state(page)
        assert web_audio(page) == {"contexts": 0, "elementSources": 0}
        playing = state["decks"][0]
        assert not playing["paused"] and playing["time"] > 0 and playing["src"].startswith("/api/tracks/")
        assert state["decks"][1]["src"] is None and state["decks"][1]["track"] is None  # (the spare is never given a song)
        assert state["blends"] == 0 and state["decks"][0]["levelling"] is None and state["decks"][0]["fade"] is None  # (no graph to level or fade in)
        # not even the one place that asks for the graph for its own sake, the picture that moves with the sound
        page.evaluate("location.hash = '#/now'")
        page.wait_for_selector(".nowplaying")
        assert page.locator("canvas.visualizer").count() == 0
        assert web_audio(page) == {"contexts": 0, "elementSources": 0}


def test_when_a_song_ends_on_an_iphone_the_next_is_loaded_into_the_same_element_and_plays(browser, live):
    with opened(browser, live, init_script=COUNT_WEB_AUDIO, device=PHONE) as page:
        add_library(page, live)
        play_songs(page)
        first = page.evaluate(f"{PLAYER}.then((m) => m.player.get().items[m.player.get().order[m.player.get().pos]].title)")
        page.evaluate(f"{PLAYER}.then((m) => m.seek(m.deckState().decks[0].duration - 1.2))")
        wait_until(page, f"{PLAYER}.then((m) => m.player.get().pos === 1)", 10)
        wait_until(page, f"{PLAYER}.then((m) => m.deckState().decks[0].time > 0.2 && !m.deckState().decks[0].paused)", 10)
        state = deck_state(page)
        assert state["hardEnds"] == 1 and state["blends"] == 0  # (it ended, and the next was loaded after: the one thing iOS allows)
        assert state["active"] == "a" and state["decks"][1]["src"] is None  # (the same element throughout)
        assert page.evaluate(f"{PLAYER}.then((m) => m.player.get().items[m.player.get().order[m.player.get().pos]].title)") != first
        assert web_audio(page) == {"contexts": 0, "elementSources": 0}


def test_on_a_computer_the_graph_is_still_built_and_the_second_element_still_gets_the_next_song(page, live):
    """The other side of it: nothing about the computer changed (the equalizer, loudness levelling and crossfade are made of the graph)."""
    page.add_init_script(COUNT_WEB_AUDIO)
    page.reload()
    page.wait_for_selector(".sidebar")
    add_library(page, live)
    assert page.evaluate(f"{PLAYER}.then((m) => m.PLAIN)") is False
    play_songs(page)
    wait_until(page, f"{PLAYER}.then((m) => m.deckState().decks[1].track !== null)", 10)  # (the next song is ready on the spare, ahead of time)
    assert web_audio(page)["contexts"] == 1 and web_audio(page)["elementSources"] == 2


def test_the_sound_panel_says_what_an_iphone_does_not_have_and_keeps_the_sleep_timer(browser, live):
    with opened(browser, live, device=PHONE) as page:
        add_library(page, live)
        play_songs(page)
        page.evaluate("location.hash = '#/now'")
        page.get_by_role("tab", name="Sound").click()
        expect(page.get_by_text("the volume is set with the device's own buttons", exact=False)).to_be_visible()
        assert page.get_by_role("heading", name="Equalizer").count() == 0
        assert page.get_by_role("heading", name="Between songs").count() == 0
        assert page.get_by_label("Crossfade length in seconds").count() == 0
        expect(page.get_by_label("Sleep timer")).to_be_visible()


def test_the_lock_screen_is_told_the_song_and_its_picture_and_gets_the_buttons_for_a_song(browser, live):
    with opened(browser, live, init_script=WATCH_MEDIA_SESSION) as page:
        add_library(page, live)
        play_songs(page)  # (alphabetical: Drift is the first song, and it is one of the three that have a picture)
        wait_until(page, "navigator.mediaSession.metadata && navigator.mediaSession.metadata.artwork.length > 0", 10)
        meta = page.evaluate(
            "({title: navigator.mediaSession.metadata.title, artist: navigator.mediaSession.metadata.artist,"
            " album: navigator.mediaSession.metadata.album, art: navigator.mediaSession.metadata.artwork.map((a) => ({src: a.src, type: a.type}))})"
        )
        assert (meta["title"], meta["artist"], meta["album"]) == ("Drift", "Aurora Vale", "Northern Lights")
        assert len(meta["art"]) == 1 and meta["art"][0]["src"].startswith("blob:") and meta["art"][0]["type"] == "image/jpeg"
        handlers = page.evaluate("window.__handlers")
        assert handlers["play"] == handlers["pause"] == handlers["previoustrack"] == handlers["nexttrack"] == handlers["seekto"] == "function"
        assert handlers["seekbackward"] is None and handlers["seekforward"] is None  # (a song gets previous and next, not the skip by seconds)


def test_the_picture_of_the_last_song_does_not_stay_on_the_lock_screen_for_a_song_that_has_none(page, live):
    add_library(page, live)
    play_songs(page)  # Drift: has a picture
    wait_until(page, "navigator.mediaSession.metadata.artwork.length === 1", 10)
    page.evaluate(f"{PLAYER}.then((m) => m.next())")  # Glacier: has one too
    wait_until(page, "navigator.mediaSession.metadata.title === 'Glacier'", 10)
    page.evaluate(f"{PLAYER}.then((m) => m.next())")  # Gravel Dust: has none
    wait_until(page, "navigator.mediaSession.metadata.title === 'Gravel Dust'", 10)
    page.wait_for_timeout(600)  # (long enough for a picture of the song before to arrive late, which it must not be put up for this one)
    assert page.evaluate("navigator.mediaSession.metadata.artwork.length") == 0


def test_on_an_iphone_the_loudness_of_each_song_is_not_asked_for_because_there_is_nothing_to_apply_it_to(browser, live):
    asked: list[str] = []
    with opened(browser, live, device=PHONE) as page:
        page.on("request", lambda r: asked.append(r.url) if r.url.endswith("/info") else None)
        add_library(page, live)
        play_songs(page)
        page.evaluate(f"{PLAYER}.then((m) => m.next())")
        wait_until(page, f"{PLAYER}.then((m) => m.player.get().pos === 1 && m.deckState().decks[0].time > 0.2)")
        assert asked == []


def test_on_a_computer_the_loudness_of_each_song_is_asked_for(page, live):
    asked: list[str] = []
    page.on("request", lambda r: asked.append(r.url) if r.url.endswith("/info") else None)
    add_library(page, live)
    play_songs(page)
    wait_until(page, f"{PLAYER}.then((m) => m.deckState().decks[0].time > 0.2)")
    assert len(asked) >= 1


def test_the_system_is_told_that_the_page_plays_music(browser, live):
    """Safari 16.4 and later: with the audio session set to playback the sound carries on with the screen locked and the ring switch does not silence it."""
    with opened(browser, live, init_script="navigator.audioSession = { type: 'auto' };") as page:
        assert page.evaluate("navigator.audioSession.type") == "playback"


IPAD_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"  # (what an iPad's Safari calls itself)


def _plain(browser, live, touch_points: int) -> bool:
    script = f"Object.defineProperty(navigator, 'platform', {{ get: () => 'MacIntel' }}); Object.defineProperty(navigator, 'maxTouchPoints', {{ get: () => {touch_points} }});"
    with opened(browser, live, init_script=script, device={"user_agent": IPAD_UA}) as page:
        return page.evaluate(f"{PLAYER}.then((m) => m.PLAIN)")


def test_an_ipad_is_known_by_its_touch_screen_since_its_browser_says_it_is_a_mac(browser, live):
    assert _plain(browser, live, touch_points=5) is True
    assert _plain(browser, live, touch_points=0) is False  # (a Mac)
    assert _plain(browser, live, touch_points=1) is False  # (one touch point is a laptop's, not a tablet's: Apple's own test is more than one)


def test_the_buttons_of_the_lock_screen_drive_the_player(browser, live):
    with opened(browser, live, init_script=WATCH_MEDIA_SESSION) as page:
        add_library(page, live)
        play_songs(page)
        position = f"{PLAYER}.then((m) => m.player.get().pos)"

        page.evaluate("window.__press.play()")  # (a Play while it is playing is not a Pause)
        page.wait_for_timeout(300)
        assert page.evaluate(f"{PLAYER}.then((m) => m.player.get().playing)") is True
        page.evaluate("window.__press.pause()")
        wait_until(page, f"{PLAYER}.then((m) => !m.player.get().playing)")
        page.evaluate("window.__press.pause()")  # (and a Pause while it is paused is not a Play)
        page.wait_for_timeout(300)
        assert page.evaluate(f"{PLAYER}.then((m) => m.player.get().playing)") is False
        page.evaluate("window.__press.play()")
        wait_until(page, f"{PLAYER}.then((m) => m.player.get().playing)")

        page.evaluate("window.__press.nexttrack()")
        wait_until(page, f"{position}.then((pos) => pos === 1)")
        page.evaluate("window.__press.previoustrack()")
        wait_until(page, f"{position}.then((pos) => pos === 0)")
        page.evaluate("window.__press.seekto({ seekTime: 6 })")
        wait_until(page, f"{PLAYER}.then((m) => m.deckState().decks.some((d) => d.time > 5.5 && d.time < 8))")
        page.evaluate("window.__press.stop()")
        wait_until(page, f"{PLAYER}.then((m) => !m.player.get().playing)")
