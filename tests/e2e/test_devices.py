"""Devices in the browser: add a card, choose what goes on it, preview, copy, and the safety around removals."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest
from playwright.sync_api import expect

from musictoolkit.sync import device_detect, mtp
from tests.e2e.conftest import add_library, api
from tests.test_mtp import FAKE, SERIAL, control, write_device

expect.set_options(timeout=15_000)

PREVIEW = "Preview what will be copied"


def files_on(card: Path) -> list[str]:
    return sorted(p.relative_to(card).as_posix() for p in card.rglob("*") if p.is_file())


@pytest.fixture
def card(tmp_path: Path) -> Path:
    """A Walkman-like player: a drive whose music lives in its MUSIC folder."""
    folder = tmp_path / "walkman" / "MUSIC"
    folder.mkdir(parents=True)
    return folder.resolve()


def open_devices(page) -> None:
    page.get_by_role("link", name="Devices", exact=True).click()
    expect(page.get_by_role("heading", name="Devices", exact=True)).to_be_visible()


def add_through_the_screen(page, live, card: Path, library_ready: bool = False) -> None:
    if not library_ready:
        add_library(page, live)
    open_devices(page)
    page.locator(".page-actions").get_by_role("button", name="Add a device").click()
    page.locator(".modal").get_by_role("button", name="Choose a folder…").click()
    page.get_by_label("Folder path").fill(str(card))  # the browser build asks for a typed path instead of a native picker
    page.get_by_role("button", name="Choose", exact=True).click()
    expect(page.get_by_role("heading", name=card.name, exact=True)).to_be_visible()


def choose_genre(page, genre: str) -> None:
    page.get_by_label("Kind of music to add").select_option(label="A genre")
    page.get_by_label("Genre", exact=True).select_option(genre)
    page.get_by_role("button", name="Add", exact=True).click()
    expect(page.locator(".source-chip", has_text=f"Genre: {genre}")).to_be_visible()


def stat(page, text: str):
    return page.locator(".stat", has_text=text).locator("strong")


def copy_folk(page) -> None:
    choose_genre(page, "Folk")
    page.get_by_role("button", name=PREVIEW).click()
    page.get_by_role("button", name=re.compile(r"^Copy 2 songs")).click()
    expect(page.locator(".callout", has_text="Copied 2 songs")).to_be_visible()


class TestDevices:
    def test_add_a_card_choose_music_preview_and_copy(self, page, live, card):
        add_through_the_screen(page, live, card)
        expect(page.get_by_text("Nothing chosen yet")).to_be_visible()
        expect(page.get_by_role("button", name=PREVIEW)).to_be_disabled()

        choose_genre(page, "Folk")
        page.get_by_role("button", name=PREVIEW).click()
        expect(stat(page, "chosen")).to_have_text("2")
        expect(stat(page, "to copy")).to_have_text("2")
        rows = page.locator(".sync-row")
        expect(rows).to_have_count(2)
        expect(rows.first.locator(".where")).to_have_text("The Fernwoods/Back Roads/01 - Gravel Dust.mp3")
        assert files_on(card) == [], "a preview must not write anything to the device"

        page.get_by_role("button", name=re.compile(r"^Copy 2 songs")).click()
        expect(page.locator(".callout", has_text="Copied 2 songs")).to_be_visible()
        assert files_on(card) == [
            "The Fernwoods/Back Roads/01 - Gravel Dust.mp3",
            "The Fernwoods/Back Roads/02 - Porch Light.mp3",
        ]
        expect(page.locator(".device-status")).to_contain_text("2 songs copied")

        page.get_by_role("button", name=PREVIEW).click()
        expect(page.get_by_role("heading", name="The device is up to date")).to_be_visible()
        expect(page.get_by_role("button", name=re.compile(r"^Copy "))).to_have_count(0)

    def test_the_choices_are_remembered_and_changing_them_asks_for_a_new_preview(self, page, live, card):
        add_through_the_screen(page, live, card)
        choose_genre(page, "Folk")
        page.get_by_role("button", name=PREVIEW).click()
        expect(stat(page, "to copy")).to_have_text("2")

        choose_genre(page, "Electronic")  # after the preview: what was previewed is no longer what is chosen
        expect(page.get_by_text("You changed the choices after previewing")).to_be_visible()
        expect(page.get_by_role("button", name=re.compile(r"^Copy "))).to_have_count(0)

        page.get_by_role("button", name=PREVIEW).click()
        expect(stat(page, "chosen")).to_have_text("5")

        page.get_by_role("button", name="All devices").click()
        page.get_by_role("link", name=card.name, exact=True).click()
        expect(page.locator(".source-chip")).to_have_count(2)
        expect(page.locator(".source-chip", has_text="Genre: Electronic")).to_be_visible()

    def test_songs_no_longer_chosen_are_removed_only_after_confirming_the_count(self, page, live, card):
        add_through_the_screen(page, live, card)
        copy_folk(page)

        # swap Folk for Electronic: three new songs, and the two old ones are no longer chosen
        page.get_by_role("button", name="Remove Genre: Folk").click()
        choose_genre(page, "Electronic")
        page.get_by_role("button", name=PREVIEW).click()
        expect(stat(page, "to copy")).to_have_text("3")
        expect(stat(page, "no longer chosen")).to_have_text("2")
        expect(page.get_by_label(re.compile("Also remove the 2 songs"))).not_to_be_checked()
        page.get_by_role("button", name=re.compile(r"^Copy 3 songs")).click()
        expect(page.locator(".callout", has_text="Copied 3 songs")).to_be_visible()
        assert len(files_on(card)) == 5, "with the box left alone nothing is removed from the device"

        page.get_by_role("button", name=PREVIEW).click()
        expect(stat(page, "no longer chosen")).to_have_text("2")
        expect(page.get_by_role("heading", name="Nothing new to copy")).to_be_visible()
        page.get_by_label(re.compile("Also remove the 2 songs")).check()
        page.get_by_role("button", name="Remove 2 songs from the device").click()
        dialog = page.locator(".modal")
        expect(dialog).to_contain_text("Remove 2 songs from MUSIC?")
        dialog.get_by_role("button", name="Cancel").click()
        assert len(files_on(card)) == 5

        page.get_by_role("button", name="Remove 2 songs from the device").click()
        page.locator(".modal").get_by_role("button", name="Remove 2 songs").click()
        expect(page.locator(".callout", has_text="Removed 2 songs from MUSIC")).to_be_visible()
        names = files_on(card)
        assert len(names) == 3 and all(n.startswith("Aurora Vale/Northern Lights/") for n in names)
        assert not (card / "The Fernwoods").exists(), "the folders the removal leaves empty are tidied away"

    def test_a_playlist_goes_to_the_device_as_an_m3u8_file(self, page, live, card):
        add_library(page, live)
        ids = {i["title"]: i["id"] for i in api(page, "/tracks?limit=1000")["items"]}
        api(page, "/playlists", "POST", {"name": "Road trip", "track_ids": [ids["Glacier"], ids["Drift"]]})
        add_through_the_screen(page, live, card, library_ready=True)
        page.get_by_label("Kind of music to add").select_option(label="A playlist")
        page.get_by_label("Playlist", exact=True).select_option(label="Road trip · 2 songs")
        page.get_by_role("button", name="Add", exact=True).click()
        expect(page.locator(".source-chip", has_text="Playlist: Road trip")).to_be_visible()
        expect(page.get_by_label(re.compile("Also write each chosen playlist"))).to_be_checked()

        page.get_by_role("button", name=PREVIEW).click()
        page.get_by_role("button", name=re.compile(r"^Copy 2 songs")).click()
        expect(page.locator(".callout", has_text="wrote 1 playlist")).to_be_visible()
        playlist = (card / "Playlists" / "Road trip.m3u8").read_text(encoding="utf-8").splitlines()
        songs = [line for line in playlist if line and not line.startswith("#")]
        assert len(songs) == 2 and all(not Path(s).is_absolute() and (card / "Playlists" / s).resolve().is_file() for s in songs)

    def test_an_unplugged_device_says_so_and_cannot_be_synced(self, page, live, card):
        add_through_the_screen(page, live, card)
        choose_genre(page, "Folk")
        page.get_by_role("button", name=PREVIEW).click()
        expect(stat(page, "to copy")).to_have_text("2")

        shutil.rmtree(card.parent)  # pulled out of the computer
        page.get_by_role("button", name="All devices").click()
        expect(page.locator(".device-card.offline")).to_contain_text("Not connected")
        page.get_by_role("link", name=card.name, exact=True).click()
        expect(page.get_by_text("This device is not connected")).to_be_visible()
        expect(page.get_by_role("button", name=PREVIEW)).to_be_disabled()
        expect(page.locator(".source-chip", has_text="Genre: Folk")).to_be_visible()  # what was chosen is still there

    def test_forgetting_a_device_leaves_its_songs_alone(self, page, live, card):
        add_through_the_screen(page, live, card)
        copy_folk(page)

        page.get_by_role("button", name="All devices").click()
        page.get_by_role("button", name="More about MUSIC").click()
        page.get_by_role("menuitem", name="Forget this device").click()
        dialog = page.locator(".modal")
        expect(dialog).to_contain_text("Forget MUSIC?")
        dialog.get_by_role("button", name="Forget device").click()
        expect(page.get_by_role("heading", name="No devices yet")).to_be_visible()
        assert len(files_on(card)) == 2

    def test_a_plugged_in_player_is_offered_and_its_music_folder_is_used(self, page, live, card, monkeypatch):
        player = card.parent
        monkeypatch.setattr(
            device_detect, "list_candidate_devices",
            lambda: [device_detect.DeviceCandidate(str(player), "E:", "exFAT", 64 * 10**9, 40 * 10**9, True)],
        )
        add_library(page, live)
        open_devices(page)
        offer = page.locator(".callout.nearby")
        expect(offer).to_contain_text("This looks like a player or card")
        expect(offer).to_contain_text(f"songs go into {card}")
        offer.get_by_role("button", name="Use it").click()
        expect(page.get_by_role("heading", name="MUSIC", exact=True)).to_be_visible()
        expect(page.locator(".page-header")).to_contain_text(str(card))


class TestPhones:
    """A phone without a drive letter: found by the helper (here the stand-in), added from the dialog, copied to."""

    @pytest.fixture
    def phones(self, tmp_path, monkeypatch) -> Path:
        root = tmp_path / "phones"
        root.mkdir()
        monkeypatch.setenv("MTK_MTP_HELPER", json.dumps([sys.executable, str(FAKE), str(root)]))
        mtp.forget_listing()
        yield root
        mtp.forget_listing()

    def open_add_dialog(self, page):
        open_devices(page)
        page.locator(".page-actions").get_by_role("button", name="Add a device").click()
        return page.locator(".modal").get_by_label("Phones and players without a drive letter")

    def test_a_phone_is_added_from_the_dialog_and_gets_the_songs(self, page, live, phones):
        write_device(phones)
        add_library(page, live)

        section = self.open_add_dialog(page)
        expect(section).to_contain_text("Pixel (Pixel 8) · Internal storage")
        expect(section).to_contain_text("experimental")
        section.get_by_role("button", name="Add", exact=True).click()
        expect(page.get_by_role("heading", name="Pixel", exact=True)).to_be_visible()
        expect(page.get_by_text("has not been tried on every model")).to_be_visible()
        expect(page.get_by_role("button", name="Open folder")).to_have_count(0)

        choose_genre(page, "Folk")
        page.get_by_role("button", name=PREVIEW).click()
        expect(stat(page, "to copy")).to_have_text("2")
        assert files_on(phones / SERIAL / "s1") == [], "a preview must not write anything to the phone"
        page.get_by_role("button", name=re.compile(r"^Copy 2 songs")).click()
        expect(page.locator(".callout", has_text="Copied 2 songs")).to_be_visible()
        assert files_on(phones / SERIAL / "s1" / "Music") == [
            "The Fernwoods/Back Roads/01 - Gravel Dust.mp3",
            "The Fernwoods/Back Roads/02 - Porch Light.mp3",
        ]
        expect(page.locator(".device-status")).to_contain_text("2 songs copied")

        page.get_by_role("button", name="All devices").click()
        card = page.locator(".device-card", has_text="Pixel")
        expect(card).to_contain_text("Internal storage / Music")
        expect(card.locator(".badge", has_text="experimental")).to_be_visible()
        card.get_by_role("button", name="More about Pixel").click()
        expect(page.get_by_role("menuitem", name="Choose its folder again…")).to_have_count(0)

    def test_the_music_folder_on_the_phone_can_be_chosen(self, page, live, phones):
        write_device(phones)
        add_library(page, live)

        section = self.open_add_dialog(page)
        section.get_by_label("Folder for the music on the phone").fill("Tunes/Gym")
        section.get_by_role("button", name="Add", exact=True).click()

        expect(page.locator(".page-header")).to_contain_text("Internal storage / Tunes/Gym")

    def test_nothing_plugged_in_says_what_to_do_and_looking_again_finds_it(self, page, live, phones):
        add_library(page, live)
        section = self.open_add_dialog(page)
        expect(section).to_contain_text("unlock its screen, and choose “File transfer”")

        write_device(phones)  # the person plugs it in and unlocks it
        section.get_by_role("button", name="Look again").click()

        expect(section).to_contain_text("Pixel (Pixel 8) · Internal storage")

    def test_a_locked_phone_is_listed_with_what_to_do(self, page, live, phones):
        write_device(phones, "LOCKED", name="Locked phone")
        info = json.loads((phones / "LOCKED" / "device.json").read_text())
        info["error"] = "The device is locked"
        (phones / "LOCKED" / "device.json").write_text(json.dumps(info))
        add_library(page, live)

        section = self.open_add_dialog(page)

        expect(section).to_contain_text("Locked phone")
        expect(section).to_contain_text("The device is locked")
        expect(section).to_contain_text("Unlock it, choose “File transfer”, then look again")
        expect(section.get_by_role("button", name="Add", exact=True)).to_have_count(0)

    def test_an_unplugged_phone_says_what_to_do_and_cannot_be_synced(self, page, live, phones):
        write_device(phones)
        add_library(page, live)
        added = api(page, "/devices/mtp", "POST", {"serial": SERIAL, "storage": "s1"})
        control(phones, unplugged=[SERIAL])
        mtp.forget_listing()

        open_devices(page)
        card = page.locator(".device-card", has_text="Pixel")
        expect(card.locator(".badge.warn", has_text="Not connected")).to_be_visible()
        expect(card).to_contain_text("choose File transfer")

        page.get_by_role("link", name="Pixel", exact=True).click()
        expect(page.get_by_role("alert")).to_contain_text("Plug it in, unlock it, and choose File transfer")
        expect(page.get_by_role("button", name=PREVIEW)).to_be_disabled()
        assert added["kind"] == "mtp"
