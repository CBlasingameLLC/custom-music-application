"""New releases (the release radar) on Discover: setting it up, looking, dismissing, and what it says when there is nothing."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from musictoolkit.integrations import listenbrainz_client
from tests.e2e.conftest import api
from tests.fake_services import serve

expect.set_options(timeout=15_000)

A, B, C = ("aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002", "cccccccc-0000-4000-8000-000000000003")
ARTIST = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
TODAY = date.today()


def entry(mbid, artist, title, day, kind="Album"):
    return {"artist_credit_name": artist, "artist_mbids": [ARTIST], "caa_id": None, "caa_release_mbid": None, "release_date": day.isoformat(),
            "release_group_mbid": mbid, "release_group_primary_type": kind, "release_group_secondary_type": None, "release_mbid": mbid, "release_name": title}


@pytest.fixture
def listenbrainz(monkeypatch):
    releases = [entry(A, "Tycho", "Infinite Mile", TODAY - timedelta(days=3)), entry(B, "Bonobo", "Fragments", TODAY - timedelta(days=40), kind="EP"),
                entry(C, "Emancipator", "Koda", TODAY + timedelta(days=12), kind="Single")]
    with serve({"/user/cayl/fresh_releases": (200, {"payload": {"releases": releases}})}) as service:
        monkeypatch.setattr(listenbrainz_client, "_BASE_URL", service.url)
        yield service


def open_releases(page) -> None:
    page.evaluate("location.hash = '#/discover?tab=releases'")
    expect(page.get_by_role("tab", name="New releases")).to_have_attribute("aria-selected", "true")


def named(day: date) -> str:
    return f"{day:%b} {day.day}" + ("" if day.year == TODAY.year else f", {day.year}")


def test_without_anywhere_to_look_it_says_what_to_add_and_leads_to_settings(page, live):
    open_releases(page)

    expect(page.get_by_role("heading", name="New releases from artists you play")).to_be_visible()
    expect(page.get_by_role("button", name="Check for new releases")).to_be_disabled()
    page.get_by_role("button", name="Open Settings").click()
    expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible()


def test_a_look_fills_the_list_and_says_when_each_one_comes_out(page, live, listenbrainz):
    api(page, "/settings", "PUT", {"listenbrainz": {"username": "cayl"}})
    open_releases(page)
    expect(page.get_by_role("heading", name="Nothing checked yet")).to_be_visible()

    page.get_by_role("button", name="Check for new releases").click()

    cards = page.locator("article.release")
    expect(cards).to_have_count(3)
    expect(cards.nth(0)).to_contain_text("Emancipator - Koda")
    expect(cards.nth(0)).to_contain_text("Coming soon")
    expect(cards.nth(0)).to_contain_text(f"Out in 12 days ({named(TODAY + timedelta(days=12))})")
    expect(cards.nth(1)).to_contain_text("Released 3 days ago")
    expect(cards.nth(2)).to_contain_text(f"Released {named(TODAY - timedelta(days=40))}")
    expect(cards.nth(2).locator(".badge.kind")).to_have_text("EP")
    expect(page.get_by_role("tab", name="New releases")).to_contain_text("3")
    expect(page.get_by_text("Last checked just now (ListenBrainz)")).to_be_visible()

    links = cards.nth(1).locator(".reco-links a")
    assert [a.inner_text().strip() for a in links.all()] == ["MusicBrainz", "Bandcamp", "YouTube"]
    assert links.first.get_attribute("href") == f"https://musicbrainz.org/release-group/{A}"
    assert links.first.get_attribute("target") == "_blank" and "noopener" in links.first.get_attribute("rel")


def test_a_release_can_be_dismissed_and_brought_back(page, live, listenbrainz):
    api(page, "/settings", "PUT", {"listenbrainz": {"username": "cayl"}})
    open_releases(page)
    page.get_by_role("button", name="Check for new releases").click()
    expect(page.locator("article.release")).to_have_count(3)

    page.locator("article.release", has_text="Koda").get_by_role("button", name="Dismiss").click()

    expect(page.locator("article.release")).to_have_count(2)
    expect(page.get_by_role("tab", name="New releases")).to_contain_text("2")
    page.get_by_role("button", name="Dismissed (1)").click()
    expect(page.locator("article.release")).to_have_count(1)
    page.locator("article.release", has_text="Koda").get_by_role("button", name="Move back").click()
    expect(page.get_by_role("heading", name="Nothing dismissed")).to_be_visible()
    page.get_by_role("button", name="Back to new releases").click()
    expect(page.locator("article.release")).to_have_count(3)


def test_when_nothing_is_new_it_says_so_and_why(page, live, listenbrainz):
    listenbrainz.routes["/user/cayl/fresh_releases"] = (200, {"payload": {"releases": []}})
    api(page, "/settings", "PUT", {"listenbrainz": {"username": "cayl"}})
    open_releases(page)

    page.get_by_role("button", name="Check for new releases").click()

    expect(page.get_by_role("heading", name="No new releases right now")).to_be_visible()
    expect(page.get_by_text("ListenBrainz has no new releases for you yet")).to_be_visible()


def test_a_service_that_cannot_be_reached_is_reported_and_nothing_is_marked_as_checked(page, live, listenbrainz):
    listenbrainz.routes["/user/cayl/fresh_releases"] = (503, {"error": "busy"})
    api(page, "/settings", "PUT", {"listenbrainz": {"username": "cayl"}})
    open_releases(page)

    page.get_by_role("button", name="Check for new releases").click()

    expect(page.locator(".toast.error")).to_contain_text("Looking for new releases failed")
    expect(page.get_by_role("heading", name="Nothing checked yet")).to_be_visible()


def test_the_automatic_check_has_a_switch_in_settings(page, live):
    page.evaluate("location.hash = '#/settings'")

    page.get_by_label("Look for new releases by artists I play, about once a day").check()

    expect(page.get_by_text("Saved")).to_be_visible()
    assert api(page, "/settings")["app"]["release_radar"] is True
