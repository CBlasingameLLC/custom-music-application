"""The library tools in the browser: tag editor, enrichment review, organize, duplicates, sync, import."""

from __future__ import annotations

import mutagen
import pytest
from playwright.sync_api import expect

from musictoolkit.ingest import tagger
from tests.conftest import jpeg_bytes, make_track
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


class TestOrganize:
    @pytest.fixture
    def inbox(self, live):
        """Two properly tagged songs dumped in one folder with a lyrics file and a cover; the rest of the library is tidy."""
        inbox = live.library / "inbox"
        for n, title in enumerate(["Inbox One", "Inbox Two"], start=1):
            make_track(inbox, f"track {n}.mp3", "tone_12s.mp3", title=title, artist="Zed Band", albumartist="Zed Band",
                       album="First Album", tracknumber=n)
        (inbox / "track 1.lrc").write_text("[00:01.00]one\n", encoding="utf-8")
        (inbox / "cover.jpg").write_bytes(jpeg_bytes())
        return inbox

    def open_page(self, page, live):
        add_library(page, live)
        page.get_by_role("link", name="Library tools").click()
        page.get_by_role("link", name="Organize files").click()
        expect(page.get_by_role("heading", name="Organize files")).to_be_visible()

    def test_preview_then_move_then_undo(self, page, live, inbox):
        self.open_page(page, live)
        expect(page.get_by_label("Folder and file name, built from these fields")).to_have_value("{album_artist}/{album}/{track:02d} - {title}.{ext}")
        expect(page.locator(".example-box code").first).to_have_text("Aurora Vale/Northern Lights/03 - Glacier.flac")

        page.get_by_role("button", name="Preview changes").click()
        expect(page.locator(".stat", has_text="songs will move").locator("strong")).to_have_text("2")
        expect(page.locator(".stat", has_text="already in place").locator("strong")).to_have_text("9")
        rows = page.locator(".move-row")
        expect(rows).to_have_count(2)
        expect(rows.first.locator(".from")).to_have_text("inbox/track 1.mp3")
        expect(rows.first.locator(".to")).to_have_text("Zed Band/First Album/01 - Inbox One.mp3")
        assert (inbox / "track 1.mp3").exists(), "a preview must not move anything"

        page.get_by_role("button", name="Move 2 songs").click()
        dialog = page.locator(".modal")
        expect(dialog).to_contain_text("Move 2 songs?")
        dialog.get_by_role("button", name="Move 2 songs").click()

        expect(page.get_by_text("Moved 2 songs.")).to_be_visible()
        album = live.library / "Zed Band" / "First Album"
        assert (album / "01 - Inbox One.mp3").exists() and (album / "02 - Inbox Two.mp3").exists()
        assert (album / "01 - Inbox One.lrc").read_text(encoding="utf-8") == "[00:01.00]one\n"
        assert (album / "cover.jpg").exists()
        assert not inbox.exists(), "the emptied folder is tidied away"
        batch = page.locator(".batch-row")
        expect(batch).to_have_count(1)
        expect(batch).to_contain_text("2 songs moved")

        # the library follows the files: nothing is missing and the songs are findable at their new place
        page.evaluate("location.hash = '#/songs'")
        expect(page.locator(".trow", has_text="Inbox One")).to_have_count(1)
        page.go_back()

        batch.get_by_role("button", name="Undo").click()
        expect(page.get_by_text("Put 2 songs back.")).to_be_visible()
        assert (inbox / "track 1.mp3").exists() and (inbox / "track 2.mp3").exists()
        assert (inbox / "track 1.lrc").exists() and (inbox / "cover.jpg").exists()
        assert not (live.library / "Zed Band").exists()
        expect(page.locator(".batch-row")).to_contain_text("Undone")

    def test_changing_the_layout_after_a_preview_asks_for_a_new_preview(self, page, live, inbox):
        self.open_page(page, live)
        page.get_by_role("button", name="Preview changes").click()
        expect(page.get_by_role("button", name="Move 2 songs")).to_be_visible()

        page.get_by_label("Layout preset").select_option(label="Artist / Album / Title")
        expect(page.get_by_text("You changed the folder or layout after previewing")).to_be_visible()
        expect(page.get_by_role("button", name="Move 2 songs")).to_have_count(0)
        page.get_by_role("button", name="Preview changes").click()
        # without track numbers the tidy songs change name too, so look for the inbox song's row
        expect(page.locator(".move-row", has_text="inbox/track 1.mp3").locator(".to")).to_have_text("Zed Band/First Album/Inbox One.mp3")
        assert (inbox / "track 1.mp3").exists()

    def test_a_layout_that_cannot_work_is_explained_and_cannot_be_previewed(self, page, live, inbox):
        self.open_page(page, live)
        box = page.get_by_label("Folder and file name, built from these fields")
        box.fill("{nonsense}/{title}.{ext}")
        expect(page.locator(".example-box.bad")).to_contain_text("Available fields")
        expect(page.get_by_role("button", name="Preview changes")).to_be_disabled()
        box.fill("")
        expect(page.get_by_role("button", name="Preview changes")).to_be_disabled()
        page.get_by_role("button", name="{album}").click()
        page.get_by_role("button", name="{ext}").click()
        expect(box).to_have_value("{album}{ext}")
        expect(page.locator(".example-box code").first).to_have_text("Northern Lightsflac")

    def test_the_hub_links_to_organize(self, page, live, inbox):
        add_library(page, live)
        page.get_by_role("link", name="Library tools").click()
        expect(page.locator(".tool-card", has_text="Organize files")).to_be_visible()


class TestDuplicates:
    @pytest.fixture
    def twin(self, live):
        """A bare second copy of Glacier in Downloads (with a lyrics file); the album-folder copy is the richer one."""
        copy = make_track(live.library / "Downloads", "Glacier (copy).mp3", "tone_12s.mp3", title="Glacier", artist="Aurora Vale")
        copy.with_suffix(".lrc").write_text("[00:01.00]hi\n", encoding="utf-8")
        return copy

    @pytest.fixture(autouse=True)
    def private_recycle_bin(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))  # keep the test off the real trash folder

    def open_page(self, page, live):
        add_library(page, live)
        page.get_by_role("link", name="Library tools").click()
        page.get_by_role("link", name="Find duplicates").click()
        expect(page.get_by_role("heading", name="Find duplicates")).to_be_visible()

    def test_find_move_restore_and_delete(self, page, live, twin, tmp_path):
        self.open_page(page, live)
        page.get_by_role("button", name="Find duplicates").click()

        expect(page.locator(".stat", has_text="found more than once").locator("strong")).to_have_text("1")
        group = page.locator(".dup-group")
        expect(group).to_have_count(1)
        members = group.locator(".dup-member")
        expect(members).to_have_count(2)
        expect(members.first.locator(".dup-path")).to_contain_text("01 - Glacier.mp3")
        expect(members.first.locator(".dup-verdict")).to_have_text("Keep · suggested")
        expect(members.nth(1).locator(".dup-verdict")).to_have_text("Move to review folder")

        page.get_by_role("button", name="Move to review folder").click()
        dialog = page.locator(".modal")
        expect(dialog).to_contain_text("Move 1 copy to the review folder?")
        dialog.get_by_role("button", name="Move 1 copy").click()

        expect(page.get_by_text("Moved 1 copy to the review folder.")).to_be_visible()
        parked = live.library / "_duplicates_review" / "Downloads" / "Glacier (copy).mp3"
        assert parked.exists() and parked.with_suffix(".lrc").exists() and not twin.exists()
        expect(page.get_by_text("No duplicates found")).to_be_visible()  # the list refreshed itself
        page.evaluate("location.hash = '#/songs'")
        expect(page.locator(".trow", has_text="Glacier")).to_have_count(1)
        page.go_back()

        page.get_by_role("tab", name="Review folder").click()
        expect(page.locator(".review-row")).to_have_count(1)
        expect(page.locator(".review-row .path")).to_contain_text("Downloads/Glacier (copy).mp3")
        page.get_by_role("button", name="Restore all").click()
        expect(page.get_by_text("Restored 1 song.")).to_be_visible()
        assert twin.exists() and not parked.exists()
        expect(page.get_by_text("The review folder is empty")).to_be_visible()

        # park it again, then delete it for good (to the Recycle Bin) behind a typed confirmation
        page.get_by_role("tab", name="Find").click()
        page.get_by_role("button", name="Search again").click()
        expect(page.locator(".dup-group")).to_have_count(1)
        page.get_by_role("button", name="Move to review folder").click()
        page.locator(".modal").get_by_role("button", name="Move 1 copy").click()
        expect(page.get_by_text("Moved 1 copy to the review folder.")).to_be_visible()

        page.get_by_role("tab", name="Review folder").click()
        page.get_by_role("button", name="Delete all…").click()
        dialog = page.locator(".modal")
        confirm = dialog.get_by_role("button", name="Delete all 1 song")
        expect(confirm).to_be_disabled()
        dialog.get_by_label("Type delete to confirm").fill("nope")
        expect(confirm).to_be_disabled()
        dialog.get_by_label("Type delete to confirm").fill("DELETE")
        confirm.click()
        expect(page.get_by_text("Moved 1 song to the Recycle Bin.")).to_be_visible()
        assert not parked.exists() and not (live.library / "_duplicates_review").exists()
        assert (live.library / "Aurora Vale" / "Northern Lights" / "01 - Glacier.mp3").exists()

    def test_picking_the_other_copy_to_keep(self, page, live, twin):
        self.open_page(page, live)
        page.get_by_role("button", name="Find duplicates").click()
        members = page.locator(".dup-group .dup-member")
        expect(members).to_have_count(2)
        members.nth(1).locator("input[type=radio]").check()  # keep the Downloads copy instead
        expect(members.nth(1).locator(".dup-verdict")).to_have_text("Keep")
        expect(members.first.locator(".dup-verdict")).to_have_text("Move to review folder")
        page.get_by_role("button", name="Move to review folder").click()
        page.locator(".modal").get_by_role("button", name="Move 1 copy").click()
        expect(page.get_by_text("Moved 1 copy to the review folder.")).to_be_visible()
        assert twin.exists()
        assert (live.library / "_duplicates_review" / "Aurora Vale" / "Northern Lights" / "01 - Glacier.mp3").exists()

    def test_different_songs_can_be_marked_so_and_brought_back(self, page, live, twin):
        self.open_page(page, live)
        page.get_by_role("button", name="Find duplicates").click()
        page.get_by_role("button", name="These are different songs").click()
        expect(page.locator(".dup-group")).to_have_count(0)
        page.get_by_role("button", name="Search again").click()
        expect(page.get_by_text("No duplicates found")).to_be_visible()
        expect(page.get_by_text("1 match you marked as different was left out.")).to_be_visible()
        page.get_by_role("button", name="Check the 1 I marked as different again").click()
        page.get_by_role("button", name="Search again").click()
        expect(page.locator(".dup-group")).to_have_count(1)

    def test_a_group_can_be_left_out_of_the_move(self, page, live, twin):
        self.open_page(page, live)
        page.get_by_role("button", name="Find duplicates").click()
        page.locator(".dup-head input[type=checkbox]").uncheck()
        expect(page.get_by_role("button", name="Move to review folder")).to_be_disabled()
        expect(page.locator(".dup-bar")).to_contain_text("0 copies in 0 groups")

    def test_the_hub_counts_what_waits_in_the_review_folder(self, page, live, twin):
        self.open_page(page, live)
        page.get_by_role("button", name="Find duplicates").click()
        page.get_by_role("button", name="Move to review folder").click()
        page.locator(".modal").get_by_role("button", name="Move 1 copy").click()
        expect(page.get_by_text("Moved 1 copy to the review folder.")).to_be_visible()
        page.get_by_role("link", name="Library tools").click()
        expect(page.locator(".tool-card", has_text="Find duplicates")).to_contain_text("1 copy in the review folder")


class TestMissingFiles:
    @pytest.fixture
    def lost(self, live):
        """Two songs deleted behind the app's back, then noticed by a scan."""
        add_library_files = [song_file(live, "Wires"), song_file(live, "Neon Rain")]
        return add_library_files

    def open_page(self, page, live, lost):
        add_library(page, live)
        for path in lost:
            path.unlink()
        result = api(page, "/library/scan", "POST")
        wait_until(page, f"fetch('/api/jobs/{result['job']['id']}').then(r => r.json()).then(j => j.status === 'done')", 60)
        page.get_by_role("link", name="Library tools").click()
        expect(page.locator(".tool-card", has_text="Missing files")).to_contain_text("2 songs cannot be found")
        page.get_by_role("link", name="Missing files").click()
        expect(page.get_by_role("heading", name="Missing files")).to_be_visible()

    def test_look_at_missing_songs_and_forget_them(self, page, live, lost):
        self.open_page(page, live, lost)
        expect(page.locator(".stat", has_text="cannot be found").locator("strong")).to_have_text("2")
        expect(page.locator(".folder-card")).to_have_count(1)
        expect(page.locator(".folder-card .chip")).to_have_text("Connected")
        rows = page.locator(".review-row")
        expect(rows).to_have_count(2)
        expect(rows.first).to_contain_text("Wires")  # listed by path

        rows.first.locator("input[type=checkbox]").check()
        page.get_by_role("button", name="Forget selected…").click()
        dialog = page.locator(".modal")
        expect(dialog).to_contain_text("Forget 1 song?")
        dialog.get_by_role("button", name="Forget 1 song").click()
        expect(page.get_by_text("Forgot 1 song.")).to_be_visible()
        expect(rows).to_have_count(1)

        page.get_by_role("button", name="Forget all…").click()
        page.locator(".modal").get_by_role("button", name="Forget all 1 song").click()
        expect(page.get_by_text("Nothing is missing")).to_be_visible()

    def test_a_song_whose_file_returns_comes_back_after_checking_again(self, page, live, lost, tmp_path):
        import shutil

        backup = tmp_path / "backup.mp3"
        shutil.copy(lost[0], backup)
        self.open_page(page, live, lost)
        shutil.copy(backup, lost[0])
        page.get_by_role("button", name="Check again").click()
        expect(page.locator(".review-row")).to_have_count(1)
        expect(page.locator(".stat", has_text="cannot be found").locator("strong")).to_have_text("1")
