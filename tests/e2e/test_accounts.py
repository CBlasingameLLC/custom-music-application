"""Accounts in the browser: the Test connection buttons, the scrobbling status, and what is announced while a song plays.

ListenBrainz, Last.fm and MusicBrainz are faked in-process (the server runs in this process too), so nothing
here touches the network.
"""

from __future__ import annotations

from playwright.sync_api import expect

from musictoolkit.integrations import lastfm_client, listenbrainz_client, musicbrainz_client
from musictoolkit.integrations.listenbrainz_client import ListenBrainzError
from tests.e2e.conftest import add_library, api, play

expect.set_options(timeout=15_000)


def open_settings(page) -> None:
    page.get_by_role("link", name="Settings", exact=True).click()
    expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible()


def service(page, name: str):
    return page.locator(".service", has_text=name)


def test_listenbrainz_is_tested_with_what_was_just_typed(page, live, monkeypatch):
    monkeypatch.setattr(
        listenbrainz_client, "validate_token", lambda token: {"valid": token == "tok-good", "user_name": "Cayl", "message": ""}
    )
    open_settings(page)
    box = service(page, "ListenBrainz")

    box.get_by_placeholder("Paste your token").fill("tok-bad")
    box.get_by_role("button", name="Test connection").click()
    expect(box.get_by_text("does not recognise this token")).to_be_visible()
    assert api(page, "/settings")["listenbrainz"]["has_token"] is True, "testing saves what was typed first"

    box.get_by_placeholder("Enter a new token to replace it").fill("tok-good")
    box.get_by_role("button", name="Test connection").click()
    expect(box.get_by_text("Connected as Cayl.")).to_be_visible()
    assert "tok-good" not in str(api(page, "/settings"))


def test_lastfm_and_musicbrainz_report_what_their_services_said(page, live, monkeypatch):
    monkeypatch.setattr(lastfm_client, "check_key", lambda key: (key == "key-1", "Last.fm accepted the key." if key == "key-1" else "Last.fm says: Invalid API key"))
    monkeypatch.setattr(
        musicbrainz_client, "ping", lambda app, version, contact: (False, "MusicBrainz is busy right now (it limits how fast apps may ask). Try again in a minute.")
    )
    open_settings(page)

    last = service(page, "Last.fm")
    last.get_by_placeholder("API key").fill("key-1")
    last.get_by_role("button", name="Test connection").click()
    expect(last.get_by_text("Last.fm accepted the key.")).to_be_visible()

    brainz = service(page, "MusicBrainz")
    brainz.get_by_role("button", name="Test connection").click()  # nothing saved yet
    expect(brainz.get_by_text("give a contact email")).to_be_visible()
    brainz.get_by_label("MusicBrainz contact email").fill("me@example.com")
    brainz.get_by_role("button", name="Test connection").click()
    expect(brainz.get_by_text("MusicBrainz is busy right now")).to_be_visible()


def test_a_rejected_token_is_explained_and_sending_resumes_when_asked_again(page, live, monkeypatch):
    def refuse(token, listens, listen_type="import"):
        raise ListenBrainzError("unauthorized", status=401)

    monkeypatch.setattr(listenbrainz_client, "submit_listens", refuse)
    add_library(page, live)
    ids = {i["title"]: i["id"] for i in api(page, "/tracks?limit=1000")["items"]}
    api(page, "/plays", "POST", {"track_id": ids["Glacier"], "ms_played": 200_000})
    api(page, "/settings", "PUT", {"listenbrainz": {"user_token": "tok-1"}})

    open_settings(page)
    status = page.get_by_label("Scrobbling status")
    expect(status).to_contain_text("does not accept this token")
    expect(status.get_by_role("button", name="Try again")).to_be_visible()

    sent: list[list[dict]] = []
    monkeypatch.setattr(listenbrainz_client, "submit_listens", lambda token, listens, listen_type="import": sent.append(listens))
    status.get_by_role("button", name="Try again").click()

    expect(status).to_contain_text("Up to date")
    expect(status).to_contain_text("1 play sent")
    assert [l["track_metadata"]["track_name"] for l in sent[0]] == ["Glacier"]


def test_what_is_playing_is_announced_only_once_that_is_switched_on(page, live, monkeypatch):
    announced: list[dict] = []
    monkeypatch.setattr(listenbrainz_client, "playing_now", lambda token, meta: announced.append(meta))
    add_library(page, live)
    api(page, "/settings", "PUT", {"listenbrainz": {"user_token": "tok-1"}})

    play(page, "Glacier")
    expect(page.locator(".playerbar").get_by_role("button", name="Pause")).to_be_visible()
    assert announced == [], "off by default"

    open_settings(page)
    page.get_by_label("Also show what I'm playing right now").check()
    expect(page.get_by_text("Saved").first).to_be_visible()
    assert api(page, "/settings")["listenbrainz"]["now_playing"] is True

    page.get_by_role("link", name="Songs", exact=True).click()
    page.locator(".trow", has_text="Drift").first.locator(".t1").dblclick()
    expect(page.locator(".playerbar .pb-text .t1")).to_have_text("Drift")
    deadline_ok = False
    for _ in range(100):
        if announced:
            deadline_ok = True
            break
        page.wait_for_timeout(100)
    assert deadline_ok and announced[0]["track_name"] == "Drift" and announced[0]["artist_name"] == "Aurora Vale"
