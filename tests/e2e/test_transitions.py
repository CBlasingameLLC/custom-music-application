"""Gapless playback and crossfade, in a real browser with real audio elements (12-second songs).

The player keeps two audio elements ("decks"): one plays, the other holds the next song ready. `deckState()` shows
what each is doing and how songs changed over: `blends` counts songs that were joined ahead of time, `hardEnds` songs
that simply ended and had the next one loaded afterwards.
"""

from __future__ import annotations

import time

from playwright.sync_api import expect

from tests.e2e.conftest import add_library, play, wait_until

expect.set_options(timeout=10_000)

PLAYER = "import('/static/js/player.js')"


def decks(page) -> dict:
    return page.evaluate(f"{PLAYER}.then((m) => m.deckState())")


def call(page, script: str):
    """Run `script` with the player module as `m`."""
    return page.evaluate(f"{PLAYER}.then((m) => {{ {script} }})")


def title(page) -> str:
    return page.locator(".playerbar .pb-text .t1").inner_text()


def start(page, live, seek_to: float, song: str = "Glacier") -> None:
    add_library(page, live)
    play(page, song)
    expect(page.locator(".playerbar").get_by_role("button", name="Pause")).to_be_visible()
    wait_until(page, f"{PLAYER}.then((m) => m.deckState().decks.some((d) => d.time > 0.2))", 10)
    call(page, f"m.seek({seek_to})")


def test_by_default_one_song_runs_straight_into_the_next_with_no_load_in_between(page, live) -> None:
    start(page, live, seek_to=10.5)

    wait_until(page, "document.querySelector('.playerbar .pb-text .t1').textContent === 'Polar Night'", 10)

    state = decks(page)
    assert state["blends"] == 1 and state["hardEnds"] == 0, state
    assert state["active"] == "b"
    playing = next(d for d in state["decks"] if d["name"] == "b")
    assert not playing["paused"] and playing["time"] > 0
    # Once the join is over the old song's deck is emptied, and the next song after this one is loading into it.
    wait_until(page, f"{PLAYER}.then((m) => !m.deckState().blending)", 5)
    assert decks(page)["decks"][0]["track"] != decks(page)["decks"][1]["track"]


def test_with_gapless_switched_off_a_song_ends_and_the_next_is_loaded_afterwards(page, live) -> None:
    start(page, live, seek_to=10.5)
    call(page, "m.setGapless(false)")

    wait_until(page, "document.querySelector('.playerbar .pb-text .t1').textContent === 'Polar Night'", 10)

    state = decks(page)
    assert state["blends"] == 0 and state["hardEnds"] == 1, state


def test_a_crossfade_overlaps_the_two_songs_and_hands_the_volume_from_one_to_the_other(page, live) -> None:
    start(page, live, seek_to=6.5)
    call(page, "m.setCrossfade(4)")

    # The blend starts four seconds before the end of the 12-second song: at about 8 s.
    wait_until(page, f"{PLAYER}.then((m) => m.deckState().blending)", 10)
    assert title(page) == "Polar Night", "the screen shows the new song as soon as the blend begins"

    seen = []
    deadline = time.time() + 6
    while time.time() < deadline:
        state = decks(page)
        if not state["blending"]:
            break
        a, b = state["decks"]
        seen.append((a["paused"], b["paused"], a["fade"], b["fade"]))
        time.sleep(0.15)
    both_playing = [s for s in seen if not s[0] and not s[1]]
    assert len(both_playing) >= 5, f"both songs sound during the blend: {seen}"
    middle = [s for s in both_playing if 0.2 < s[2] < 0.95 and 0.2 < s[3] < 0.95]
    assert middle, f"the volume moves from one to the other: {both_playing}"
    # Equal power: the two gains squared add up to about one all the way through (a straight line would dip to a half).
    assert all(0.55 < s[2] ** 2 + s[3] ** 2 < 1.45 for s in middle), middle

    wait_until(page, f"{PLAYER}.then((m) => !m.deckState().blending)", 8)
    after = decks(page)
    outgoing, incoming = after["decks"][0], after["decks"][1]
    assert outgoing["fade"] == 1 and incoming["fade"] == 1, "both decks are back at full for the next time"
    assert not incoming["paused"] and incoming["time"] > 2.5
    assert after["blends"] == 1 and after["hardEnds"] == 0


def test_pausing_in_the_middle_of_a_blend_settles_on_the_new_song(page, live) -> None:
    start(page, live, seek_to=6.5)
    call(page, "m.setCrossfade(4)")
    wait_until(page, f"{PLAYER}.then((m) => m.deckState().blending)", 10)

    page.locator(".playerbar").get_by_role("button", name="Pause").click()

    expect(page.locator(".playerbar").get_by_role("button", name="Play", exact=True)).to_be_visible()
    state = decks(page)
    assert not state["blending"]
    assert title(page) == "Polar Night"
    assert sum(1 for d in state["decks"] if d["track"] is not None and not d["paused"]) == 0, "nothing is left playing"
    page.locator(".playerbar").get_by_role("button", name="Play", exact=True).click()
    expect(page.locator(".playerbar").get_by_role("button", name="Pause")).to_be_visible()
    assert title(page) == "Polar Night"


def test_skipping_with_next_is_instant_even_with_a_crossfade_on(page, live) -> None:
    start(page, live, seek_to=2)
    call(page, "m.setCrossfade(6)")

    page.locator(".playerbar").get_by_role("button", name="Next").click()

    expect(page.locator(".playerbar .pb-text .t1")).to_have_text("Polar Night")
    state = decks(page)
    assert not state["blending"] and state["blends"] == 0
    wait_until(page, f"{PLAYER}.then((m) => m.deckState().decks.some((d) => !d.paused && d.time > 0.2))", 10)


def test_skipping_in_the_middle_of_a_blend_cuts_straight_to_the_song_after(page, live) -> None:
    start(page, live, seek_to=6.5)
    call(page, "m.setCrossfade(4)")
    wait_until(page, f"{PLAYER}.then((m) => m.deckState().blending)", 10)

    page.locator(".playerbar").get_by_role("button", name="Next").click()

    expect(page.locator(".playerbar .pb-text .t1")).to_have_text("Drift")
    state = decks(page)
    assert not state["blending"]
    wait_until(page, f"{PLAYER}.then((m) => m.deckState().decks.filter((d) => !d.paused && d.time > 0.1).length === 1)", 10)
    state = decks(page)
    sounding = [d for d in state["decks"] if not d["paused"]]
    assert len(sounding) == 1 and sounding[0]["fade"] == 1


def test_the_last_song_of_the_queue_just_ends(page, live) -> None:
    add_library(page, live)
    play(page, "Porch Light")  # the last song in the list (by artist, album, track)
    expect(page.locator(".playerbar").get_by_role("button", name="Pause")).to_be_visible()
    wait_until(page, f"{PLAYER}.then((m) => m.deckState().decks.some((d) => d.time > 0.2))", 10)
    call(page, "m.seek(10.5)")

    expect(page.locator(".playerbar").get_by_role("button", name="Play", exact=True)).to_be_visible(timeout=15_000)
    state = decks(page)
    assert state["blends"] == 0 and state["hardEnds"] == 1 and title(page) == "Porch Light"


def test_the_choices_are_in_the_sound_panel_and_are_remembered(page, live) -> None:
    add_library(page, live)
    play(page, "Glacier")
    page.evaluate("location.hash = '#/now'")
    page.get_by_role("tab", name="Sound").click()

    expect(page.get_by_label("Gapless playback")).to_be_checked()
    slider = page.get_by_label("Crossfade length in seconds")
    slider.evaluate("el => { el.value = 5; el.dispatchEvent(new Event('input', {bubbles: true})); }")
    expect(page.locator("output", has_text="5 s")).to_be_visible()
    expect(page.get_by_label("Gapless playback")).to_be_disabled()  # a crossfade is already gapless

    wait_until(page, "JSON.parse(localStorage.getItem('mtk.player.v1') || '{}').crossfade === 5", 5)
    page.reload()
    page.wait_for_selector(".sidebar")
    assert call(page, "return m.player.get().crossfade") == 5


def test_the_queue_and_the_place_in_it_are_saved_while_a_song_plays(page, live) -> None:
    # The position changes four times a second while playing; saving must not wait for a quiet moment that never comes.
    add_library(page, live)
    play(page, "Glacier")
    expect(page.locator(".playerbar").get_by_role("button", name="Pause")).to_be_visible()

    wait_until(page, "(JSON.parse(localStorage.getItem('mtk.player.v1') || '{}').position || 0) > 0.5", 8)
    saved = page.evaluate("JSON.parse(localStorage.getItem('mtk.player.v1'))")
    assert len(saved["ids"]) == 9 and saved["pos"] == 1 and saved["order"][:3] == [0, 1, 2]


# ------------------------------------------------------------------------------------------- the arithmetic


def test_how_long_songs_blend_and_where_the_queue_goes_next(page, live) -> None:
    result = page.evaluate(
        """import('/static/js/transitions.js').then((t) => {
          const [out, into] = t.equalPowerCurves(65);
          const energy = Array.from(out, (v, i) => v * v + into[i] * into[i]);
          return {
            off: t.blendSeconds({ crossfade: 0, gapless: false }, 200, 200),
            gapless: t.blendSeconds({ crossfade: 0, gapless: true }, 200, 200),
            crossfade: t.blendSeconds({ crossfade: 6, gapless: true }, 200, 200),
            shortSong: t.blendSeconds({ crossfade: 12, gapless: true }, 20, 200),
            tinySong: t.blendSeconds({ crossfade: 6, gapless: true }, 0.9, 200),
            unknown: t.blendSeconds({ crossfade: 6, gapless: true }, NaN, 200),
            minEnergy: Math.min(...energy), maxEnergy: Math.max(...energy), start: [out[0], into[0]], end: [out[64], into[64]],
            middle: t.planNext({ pos: 0, order: [0, 1, 2], items: [1, 2, 3], repeat: 'off', shuffle: false }),
            last: t.planNext({ pos: 2, order: [0, 1, 2], items: [1, 2, 3], repeat: 'off', shuffle: false }),
            wrap: t.planNext({ pos: 2, order: [0, 1, 2], items: [1, 2, 3], repeat: 'all', shuffle: false }),
            wrapShuffledAhead: t.planNext({ pos: 2, order: [0, 1, 2], items: [1, 2, 3], repeat: 'all', shuffle: true }, { preview: true }),
          };
        })"""
    )

    assert result["off"] == 0
    assert 0 < result["gapless"] < 0.1
    assert result["crossfade"] == 6
    assert result["shortSong"] == 8, "never more than 40% of the shorter song"
    assert result["tinySong"] == result["gapless"], "a blend too short to hear is just a seam"
    assert result["unknown"] == 0
    assert result["minEnergy"] > 0.999 and result["maxEnergy"] < 1.001
    assert result["start"] == [1, 0] and result["end"][0] < 1e-6 and abs(result["end"][1] - 1) < 1e-6
    assert result["middle"] == {"pos": 1} and result["last"] is None
    assert result["wrap"]["pos"] == 0 and result["wrap"]["order"] == [0, 1, 2]
    assert result["wrapShuffledAhead"] is None
