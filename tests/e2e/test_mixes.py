"""Mixes on Home and their pages, and Start radio from a song's menu and from the player bar."""

from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.e2e.conftest import add_library, api, play, row

expect.set_options(timeout=15_000)


def player_bar(page):
    return page.locator(".playerbar")


def mix_titles(page) -> list[str]:
    return [m["title"] for m in api(page, "/mixes")["items"]]


def open_mix(page, title: str) -> None:
    page.locator(".mix-card", has=page.get_by_text(title, exact=True)).first.click()
    expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()


# ------------------------------------------------------------------------------------------- Home and the mix pages


def test_home_offers_the_mixes_that_have_enough_songs(page, live):
    add_library(page, live)
    page.evaluate("location.hash = '#/'")

    expect(page.get_by_role("heading", name="Made for you")).to_be_visible()
    titles = mix_titles(page)
    assert "Never played" in titles and "New in your library" in titles, "nine songs, none played, all just added"
    for title in titles:
        expect(page.locator(".shelf .mix-card .card-title", has_text=title)).to_be_visible()
    assert not any(t in titles for t in ("On repeat", "Rediscover")), "nothing has been played yet"


def test_a_mix_page_plays_in_order_shuffles_and_is_saved_as_a_playlist(page, live):
    add_library(page, live)
    page.evaluate("location.hash = '#/'")
    expect(page.get_by_role("heading", name="Made for you")).to_be_visible()
    open_mix(page, "Never played")

    mix_id = re.search(r"#/mix/([^/?]+)$", page.url).group(1)
    songs = api(page, f"/mixes/{mix_id}")["tracks"]
    expect(page.locator(".hero-text")).to_contain_text("9 songs")
    expect(page.locator(".trow:not(.skeleton)")).to_have_count(len(songs))

    actions = page.locator(".hero-actions")
    actions.get_by_role("button", name="Play", exact=True).click()
    expect(player_bar(page).locator(".pb-text .t1")).to_have_text(songs[0]["title"])
    expect(player_bar(page).get_by_role("button", name="Shuffle: off")).to_be_visible()

    actions.get_by_role("button", name="Shuffle", exact=True).click()
    expect(player_bar(page).get_by_role("button", name="Shuffle: on")).to_be_visible()

    actions.get_by_role("button", name="Save as playlist").click()
    dialog = page.locator(".modal")
    expect(dialog.get_by_label("Name")).to_have_value("Never played")
    dialog.get_by_label("Name").fill("My unplayed")
    dialog.get_by_role("button", name="Save", exact=True).click()

    expect(page.get_by_role("heading", name="My unplayed", exact=True)).to_be_visible()
    saved = next(p for p in api(page, "/playlists")["items"] if p["name"] == "My unplayed")
    assert saved["tracks"] == len(songs)
    expect(page.locator(".sidebar").get_by_role("link", name="My unplayed")).to_be_visible()


def test_a_mix_card_plays_without_opening_the_page(page, live):
    add_library(page, live)
    page.evaluate("location.hash = '#/'")
    card = page.locator(".mix-card", has=page.get_by_text("Never played", exact=True)).first

    card.hover()
    card.get_by_role("button", name="Play Never played").click()

    expect(player_bar(page).get_by_role("button", name="Pause")).to_be_visible()
    assert page.url.endswith("#/") or page.url.endswith("/#/"), "the page did not change"


def test_all_the_mixes_have_a_page_of_their_own_and_an_empty_one_says_so(page, live):
    add_library(page, live)
    page.evaluate("location.hash = '#/mixes'")
    expect(page.get_by_role("heading", name="Made for you", exact=True)).to_be_visible()
    expect(page.locator(".grid .mix-card")).to_have_count(len(mix_titles(page)))

    page.evaluate("location.hash = '#/mix/on-repeat'")  # nothing has been played
    expect(page.get_by_role("heading", name="That mix is empty today")).to_be_visible()
    page.get_by_role("link", name="See the other mixes").click()
    expect(page.locator(".grid .mix-card").first).to_be_visible()


def test_without_music_the_mixes_page_explains_and_points_to_the_import(page, live):
    page.evaluate("location.hash = '#/mixes'")

    expect(page.get_by_role("heading", name="No mixes yet")).to_be_visible()
    page.get_by_role("button", name="Import Spotify history").click()
    expect(page.get_by_role("heading", name="Import Spotify history", exact=True)).to_be_visible()


# ------------------------------------------------------------------------------------------- radio


def test_start_radio_from_a_songs_menu(page, live):
    add_library(page, live)
    row(page, "Glacier").locator(".t1").click(button="right")

    page.get_by_role("menuitem", name="Start radio").click()

    expect(page.locator(".toast", has_text='Radio from "Glacier"')).to_be_visible()
    expect(player_bar(page).locator(".pb-text .t1")).to_have_text("Glacier")
    player_bar(page).get_by_role("button", name="Queue", exact=True).click()
    queue = page.locator(".queue-drawer .qrow")
    assert queue.count() > 1, "a radio is more than the one song it started from"
    expect(queue.first).to_contain_text("Glacier")


def test_a_radio_is_only_offered_for_a_single_song(page, live):
    add_library(page, live)
    row(page, "Glacier").locator(".t1").click()
    row(page, "Drift").locator(".t1").click(modifiers=["Control"])

    row(page, "Drift").locator(".t1").click(button="right")

    expect(page.locator(".menu")).to_be_visible()
    labels = page.locator(".menu-item").all_inner_texts()
    assert not any("Start radio" in label for label in labels), labels


def test_start_radio_from_the_player_bar_for_the_song_playing(page, live):
    add_library(page, live)
    play(page, "Wires")
    expect(player_bar(page).locator(".pb-text .t1")).to_have_text("Wires")

    player_bar(page).get_by_role("button", name="Start radio from this song").click()

    expect(page.locator(".toast", has_text='Radio from "Wires"')).to_be_visible()
    player_bar(page).get_by_role("button", name="Queue", exact=True).click()
    expect(page.locator(".queue-drawer .qrow").first).to_contain_text("Wires")
    assert page.locator(".queue-drawer .qrow").count() > 1


def test_the_player_bar_has_no_radio_button_when_nothing_is_playing(page, live):
    add_library(page, live)

    expect(player_bar(page).get_by_role("button", name="Start radio from this song")).to_have_count(0)
