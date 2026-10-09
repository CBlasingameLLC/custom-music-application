"""The library tools in the browser: tag editor, enrichment review, organize, duplicates, sync, import."""

from __future__ import annotations

import mutagen
import pytest
from playwright.sync_api import expect

from musictoolkit.ingest import tagger
from tests.conftest import make_track
from tests.e2e.conftest import add_library, api, row, wait_until

expect.set_options(timeout=15_000)


def tags_of(path) -> dict:
    tags = mutagen.File(path, easy=True).tags or {}
    return {key: tags[key][0] for key in tags.keys()}


def song_file(live, name: str):
    return next(live.library.rglob(f"*{name}*.mp3"))


class TestTagEditor:
    def test_several_songs_are_edited_in_the_files_and_the_edit_can_be_undone(self, page, live):
        add_library(page, live)
        row(page, "Glacier").locator(".t1").click()
        row(page, "Polar Night").locator(".t1").click(modifiers=["Control"])
        page.get_by_role("button", name="Edit tags").click()

        dialog = page.locator(".modal")
        expect(dialog).to_contain_text("Edit tags for 2 songs")
        expect(dialog.get_by_label("Title")).to_have_count(0)  # a per-song field makes no sense in bulk
        expect(dialog.get_by_label("Artist", exact=True)).to_have_value("Aurora Vale")
        dialog.get_by_label("Genre").fill("Ambient")
        dialog.get_by_label("Year").fill("2021")
        dialog.get_by_role("button", name="Save").click()

        expect(page.get_by_text("Updated tags on 2 songs")).to_be_visible()
        assert tags_of(song_file(live, "Glacier"))["genre"] == "Ambient"
        assert tags_of(song_file(live, "Polar Night"))["date"] == "2021"
        assert tags_of(song_file(live, "Drift"))["genre"] == "Electronic"  # not selected, not touched

        page.get_by_role("button", name="Undo").click()
        expect(page.get_by_text("Undone: 2 songs restored")).to_be_visible()
        assert tags_of(song_file(live, "Glacier"))["genre"] == "Electronic"
        assert tags_of(song_file(live, "Glacier"))["date"] == "2019"

    def test_one_song_is_edited_from_its_menu_and_the_table_follows(self, page, live):
        add_library(page, live)
        row(page, "Wires").locator(".t1").click(button="right")
        page.get_by_role("menuitem", name="Edit tags…").click()
        dialog = page.locator(".modal")
        expect(dialog.get_by_label("Title")).to_have_value("Wires")
        expect(dialog.locator(".path")).to_contain_text("Wires.mp3")
        expect(dialog.get_by_role("button", name="Save")).to_be_disabled()  # nothing changed yet
        dialog.get_by_label("Title").fill("Cables")
        dialog.get_by_label("Track number").fill("7")
        dialog.get_by_role("button", name="Save").click()

        expect(page.get_by_text("Updated tags on 1 song")).to_be_visible()
        expect(page.locator(".trow", has_text="Cables")).to_have_count(1)
        expect(page.locator(".trow", has_text="Wires")).to_have_count(0)
        assert tags_of(song_file(live, "Wires"))["title"] == "Cables"
        assert tags_of(song_file(live, "Wires"))["tracknumber"] == "7"

    def test_clearing_a_field_removes_it_and_a_refused_edit_keeps_the_dialog_open(self, page, live):
        add_library(page, live)
        row(page, "Drift").locator(".t1").click(button="right")
        page.get_by_role("menuitem", name="Edit tags…").click()
        dialog = page.locator(".modal")
        page.allowed_statuses.add(422)  # the refusal below is the point of this test
        dialog.get_by_label("Genre").fill("")
        dialog.get_by_label("Title").fill("x" * 501)
        dialog.get_by_role("button", name="Save").click()
        # the server refuses and says why; the dialog stays open so nothing typed is lost
        expect(page.get_by_text("title is too long")).to_be_visible()
        expect(dialog).to_be_visible()
        dialog.get_by_label("Title").fill("Drift")  # back to what it was: nothing to send for it
        dialog.get_by_role("button", name="Save").click()
        expect(page.get_by_text("Updated tags on 1 song")).to_be_visible()
        assert "genre" not in tags_of(song_file(live, "Drift"))
        assert tags_of(song_file(live, "Drift"))["title"] == "Drift"


class TestFixMissingTags:
    @pytest.fixture
    def loose_songs(self, live, monkeypatch):
        """Two songs with no tags at all, and a pretend MusicBrainz that knows them."""
        loose = live.library / "loose"
        make_track(loose, "01 - Mystery Song.mp3", "tone_12s.mp3")
        make_track(loose, "Some Artist - Other Song.mp3", "tone_12s.mp3")
        known = {
            "Mystery Song": {"id": "mb-1", "title": "Mystery Song", "ext:score": "98", "artist-credit-phrase": "The Mystics",
                             "release-list": [{"id": "rel-1", "title": "Secrets"}]},
            "Other Song": {"id": "mb-2", "title": "Other Song", "ext:score": "72", "artist-credit-phrase": "Some Artist",
                           "release-list": [{"id": "rel-2", "title": "Odds and Ends"}]},
        }
        monkeypatch.setattr(tagger.musicbrainz_client, "best_match", lambda artist, title: known.get(title))
        return loose

    def test_lookup_review_apply_and_dismiss(self, page, live, loose_songs):
        add_library(page, live)
        page.get_by_role("link", name="Library tools").click()
        expect(page.get_by_role("heading", name="Library tools")).to_be_visible()
        page.get_by_role("link", name="Fix missing tags").click()
        expect(page.locator(".stat", has_text="to look up").locator("strong")).to_have_text("2")

        # MusicBrainz wants a contact email before the first lookup; the page asks for it right there
        lookup = page.get_by_role("button", name="Look up 2 songs")
        expect(lookup).to_be_disabled()
        page.get_by_label("Contact email").fill("me@example.com")
        page.get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_label("Contact email")).to_have_count(0)
        expect(lookup).to_be_enabled()
        lookup.click()

        rows = page.locator(".enrich-row")
        expect(rows).to_have_count(2)
        expect(rows.first).to_contain_text("The Mystics")  # best match first
        expect(rows.first.locator(".chip")).to_have_text("98%")
        expect(rows.nth(1).locator(".chip")).to_have_text("72%")
        expect(page.locator(".stat", has_text="matches to review").locator("strong")).to_have_text("2")

        page.get_by_role("button", name="Apply all 90%+ (1)").click()
        expect(page.get_by_text("Updated tags on 1 song")).to_be_visible()
        expect(rows).to_have_count(1)
        tags = mutagen.File(next(live.library.rglob("*Mystery Song.mp3")), easy=True).tags
        assert (tags["title"][0], tags["artist"][0], tags["album"][0]) == ("Mystery Song", "The Mystics", "Secrets")

        rows.first.locator("input[type=checkbox]").check()
        page.locator(".enrich-bar").get_by_role("button", name="Dismiss").click()
        expect(page.get_by_text("Dismissed 1 match")).to_be_visible()
        expect(page.get_by_text("All caught up")).to_be_visible()
        assert "title" not in tags_of(next(live.library.rglob("*Other Song.mp3")))  # the dismissed one is untouched

    def test_the_hub_reports_what_needs_attention(self, page, live, loose_songs):
        add_library(page, live)
        page.get_by_role("link", name="Library tools").click()
        expect(page.locator(".tool-card", has_text="Fix missing tags")).to_contain_text("2 songs to look up")
