"""Importing a Spotify export in the browser: choose it, see what is in it, add it, send it on, undo it."""

from __future__ import annotations

import zipfile
from pathlib import Path

from playwright.sync_api import expect

from musictoolkit.history import spotify_import
from tests.e2e.conftest import api
from tests.test_spotify_import import SAMPLE_RECORDS, _write_export_zip

expect.set_options(timeout=15_000)


def open_import(page) -> None:
    page.get_by_role("link", name="Listening history", exact=True).click()
    expect(page.get_by_role("heading", name="Listening history", exact=True)).to_be_visible()
    page.locator(".page-actions").get_by_role("button", name="Import Spotify history").click()
    expect(page.get_by_role("heading", name="Import Spotify history", exact=True)).to_be_visible()


def choose_zip(page, path: Path) -> None:
    page.get_by_role("button", name="Choose the ZIP file…").click()
    page.get_by_label("File path").fill(str(path))  # the browser build asks for a typed path instead of a native picker
    page.get_by_role("button", name="Choose", exact=True).click()


def test_choose_look_add_see_and_undo(page, live, tmp_path):
    open_import(page)
    choose_zip(page, _write_export_zip(tmp_path, SAMPLE_RECORDS))

    expect(page.locator(".stat", has_text="plays of music").locator("strong")).to_have_text("2")
    expect(page.locator(".stat", has_text="podcast rows left out").locator("strong")).to_have_text("1")
    expect(page.locator(".bar-row", has_text="Artist A")).to_contain_text("2")
    assert api(page, "/history/imports")["items"] == [], "looking at an export adds nothing"

    page.get_by_role("button", name="Add 2 plays to my history").click()
    expect(page.locator(".callout", has_text="Added 2 plays")).to_be_visible()
    expect(page.locator(".batch-row")).to_have_count(1)
    expect(page.locator(".batch-row")).to_contain_text("2 plays")
    expect(page.get_by_text("Add your ListenBrainz token in")).to_be_visible()

    page.get_by_role("link", name="See your listening history").click()
    expect(page.locator(".bar-row", has_text="Artist A")).to_be_visible()
    expect(page.locator(".recent-row", has_text="Song One")).to_be_visible()

    open_import(page)
    page.get_by_role("button", name="Undo").click()
    dialog = page.locator(".modal")
    expect(dialog).to_contain_text("Remove 2 imported plays?")
    dialog.get_by_role("button", name="Remove them").click()
    expect(page.get_by_text("Removed 2 plays.")).to_be_visible()
    expect(page.locator(".batch-row")).to_have_count(0)


def test_imported_history_goes_to_listenbrainz_when_asked(page, live, tmp_path, monkeypatch):
    sent: list[list[dict]] = []
    monkeypatch.setattr(spotify_import.listenbrainz_client, "submit_listens", lambda token, listens, listen_type="import": sent.append(listens))
    api(page, "/settings", "PUT", {"listenbrainz": {"user_token": "tok-1"}})
    open_import(page)
    choose_zip(page, _write_export_zip(tmp_path, SAMPLE_RECORDS))
    page.get_by_role("button", name="Add 2 plays to my history").click()
    expect(page.locator(".callout", has_text="Added 2 plays")).to_be_visible()

    page.get_by_role("button", name="Send 2 plays to ListenBrainz").click()

    expect(page.get_by_text("Sent 2 plays to ListenBrainz.")).to_be_visible()
    expect(page.get_by_text("Everything you imported has been sent.")).to_be_visible()
    assert [l["track_metadata"]["track_name"] for l in sent[0]] == ["Song One", "Song Two"]
    expect(page.locator(".batch-row")).to_contain_text("2 sent to ListenBrainz")


def test_the_wrong_download_is_explained(page, live, tmp_path):
    account_data = tmp_path / "my_spotify_data.zip"  # "Account data" rather than the extended streaming history
    with zipfile.ZipFile(account_data, "w") as archive:
        archive.writestr("Userdata.json", "{}")
    open_import(page)

    choose_zip(page, account_data)

    expect(page.locator(".callout.warn")).to_contain_text("Extended streaming history")
    expect(page.get_by_role("button", name="Choose the ZIP file…")).to_be_enabled()
