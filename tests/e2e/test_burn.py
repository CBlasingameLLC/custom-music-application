"""The Burn tab of the Discs screen in a real browser: a stand-in recorder (tests/fake_disc_helper.py) with a blank disc in it takes
songs from the library onto a music CD or a data disc, makes an ISO file, erases a rewritable disc; the real UI puts the songs on a list,
checks it, asks before anything is written, shows the burn, says what became of the disc, and keeps the history."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from playwright.sync_api import expect

from musictoolkit.discs import helper as disc_helper
from tests.e2e.conftest import add_library, api, row, wait_until
from tests.fake_disc_helper import add_drive, audio_disc, blank_disc, set_control

expect.set_options(timeout=15_000)

FAKE = Path(__file__).resolve().parent.parent / "fake_disc_helper.py"
ALBUM = ("Wires", "Neon Rain", "Signal Fade")  # an album of the test library (12 seconds a song)
BURN = "Burn a disc"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Drive E: a recorder with a blank CD-R in it."""
    root = tmp_path / "drives"
    add_drive(root, "E", name="Stand-in recorder", writer=True, disc=blank_disc(type="CD-R"))
    monkeypatch.setenv("MTK_DISC_HELPER", json.dumps([sys.executable, str(FAKE), str(root)]))
    disc_helper.forget_listing()
    yield root
    disc_helper.forget_listing()


def disc_head(page):
    """The strip that names the drive and says what is in it (the Rip tab has one too, hidden)."""
    return page.locator(".disc-head:visible")


def disc_of(world: Path, letter: str = "E") -> dict:
    return json.loads((world / letter / "disc.json").read_text())


def put_on_list(page, *titles: str) -> None:
    """Songs on the burn list, the way the menus do it."""
    found = {item["title"]: item for item in api(page, "/tracks?limit=1000")["items"]}
    page.evaluate("(tracks) => import('/static/js/state.js').then((s) => s.addToBurnList(tracks))", [found[title] for title in titles])


def open_burn(page) -> None:
    page.evaluate("location.hash = '#/discs'")
    expect(page.get_by_role("heading", name="Discs", level=1)).to_be_visible()
    page.get_by_role("tab", name=BURN).click()
    expect(page.get_by_role("radiogroup", name="What to make")).to_be_visible()


def expect_titles(page, titles: list[str]) -> None:
    """The burn list shows exactly these songs, in this order (waits for the page to catch up)."""
    expect(page.locator(".burn-songs li .rip-song strong")).to_have_text(titles)


def kind(page, name: str) -> None:
    page.get_by_role("radio", name=re.compile(rf"^{name}")).check()


def eject_box(page):
    return page.get_by_role("checkbox", name="Eject the disc when it is done")


def start_button(page, name: str = "Burn the music CD…"):
    return page.get_by_role("button", name=name)


def confirm(page, title: str, button: str) -> None:
    dialog = page.get_by_role("dialog", name=title)
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name=button).click()


# ---------------------------------------------------------------------------------------- the list


def test_songs_are_put_on_the_list_from_a_menu_an_album_and_a_playlist_and_put_in_order(page, live, world) -> None:
    add_library(page, live)
    row(page, "Drift").locator(".t1").click(button="right")
    page.get_by_role("menuitem", name="Add to burn list").click()
    expect(page.locator(".toast", has_text='Added "Drift" to the burn list')).to_be_visible()

    page.evaluate("location.hash = '#/albums'")
    page.get_by_role("link", name=re.compile("Static Hearts")).first.click()
    page.get_by_role("button", name="Add to burn list").click()
    expect(page.locator(".toast", has_text="Added 3 songs to the burn list")).to_be_visible()

    found = {item["title"]: item["id"] for item in api(page, "/tracks?limit=1000")["items"]}
    playlist = api(page, "/playlists", "POST", {"name": "Road trip", "track_ids": [found["Gravel Dust"], found["Porch Light"]]})
    page.evaluate(f"location.hash = '#/playlist/{playlist['id']}'")
    page.get_by_role("button", name="Add to burn list").click()
    page.locator(".toast", has_text="Added 2 songs to the burn list").get_by_role("button", name="Open the list").click()

    wait_until(page, "location.hash === '#/discs'")
    expect(page.get_by_role("tab", name=BURN)).to_have_attribute("aria-selected", "true")
    expect_titles(page, ["Drift", *ALBUM, "Gravel Dust", "Porch Light"])
    expect(page.locator(".burn-total")).to_contain_text("6 songs")

    page.get_by_role("button", name="Move Drift down").click()
    expect_titles(page, [ALBUM[0], "Drift", *ALBUM[1:], "Gravel Dust", "Porch Light"])
    expect(page.get_by_role("button", name="Move Drift down")).to_be_focused()  # the keyboard stays with the song it moved
    expect(page.get_by_role("button", name=f"Move {ALBUM[0]} up")).to_be_disabled()
    expect(page.get_by_role("button", name="Move Porch Light down")).to_be_disabled()

    page.get_by_role("button", name="Take Porch Light off the list").click()
    expect_titles(page, [ALBUM[0], "Drift", *ALBUM[1:], "Gravel Dust"])

    page.reload()  # (the list is kept for the session: a reload does not lose it)
    page.wait_for_selector(".sidebar")
    page.evaluate("location.hash = '#/discs'")
    page.get_by_role("tab", name=BURN).click()
    expect_titles(page, [ALBUM[0], "Drift", *ALBUM[1:], "Gravel Dust"])

    page.get_by_role("button", name="Clear the list").click()
    expect(page.get_by_role("heading", name="No songs on the list yet")).to_be_visible()
    expect(start_button(page)).to_be_disabled()


# ---------------------------------------------------------------------------------------- a music CD


def test_a_music_cd_is_checked_confirmed_burned_and_remembered(page, live, world) -> None:
    add_library(page, live)
    put_on_list(page, *ALBUM)
    open_burn(page)
    expect(disc_head(page)).to_contain_text("E: Stand-in recorder")
    expect(disc_head(page)).to_contain_text("Blank CD-R, 1 hr 20 min free")
    expect(page.locator(".burn-songs li .reason")).to_have_text([re.compile("Will be (burned|converted)")] * 3)
    expect(page.locator(".capacity-row")).to_contain_text("1 min of 1 hr 20 min")
    expect(page.get_by_text("Burning is new and has not been tried")).to_be_visible()
    assert disc_of(world)["kind"] == "blank", "nothing is written before the person has said so"

    expect(eject_box(page)).to_be_checked()
    eject_box(page).uncheck()  # (so that the disc can be looked at afterwards)
    start_button(page).click()
    dialog = page.get_by_role("dialog", name="Burn this music CD?")
    expect(dialog).to_contain_text("3 songs")
    expect(dialog).to_contain_text("cannot be undone")
    expect(dialog).to_contain_text("a CD-R is spoiled for good")
    expect(dialog).to_contain_text("listen to it once")
    dialog.get_by_role("button", name="Cancel").click()
    assert disc_of(world)["kind"] == "blank", "a refused question writes nothing"

    start_button(page).click()
    confirm(page, "Burn this music CD?", "Burn the disc")
    expect(page.locator("#burn-title")).to_have_text("Music CD of 3 songs")
    expect(page.locator("#burn-title")).to_be_focused()  # the keyboard follows the page to what is happening
    expect(page.locator(".callout", has_text="The music CD is ready: 3 songs")).to_be_visible(timeout=60_000)
    expect(page.locator(".rip-tracks li.done")).to_have_count(3)
    expect(page.locator(".toast", has_text="Burned 3 songs")).to_be_visible()
    disc = disc_of(world)
    assert disc["kind"] == "audio" and disc["type"] == "CD-R" and [t["audio"] for t in disc["tracks"]] == [True, True, True]
    assert not (world / "E" / "eject.log").exists()
    assert not list((live.ctx.data_dir / "cache" / "discs").rglob("*.pcm")), "the temporary copies of the songs are gone"

    page.get_by_role("tab", name="History").click()
    row_ = page.locator(".rip-row").first
    expect(row_).to_contain_text("Music CD of 3 songs")
    expect(row_).to_contain_text("CD-R · E: · 3 songs")
    expect(row_).to_contain_text("Done")
    row_.get_by_role("button", name="Details").click()
    expect(row_.locator(".rip-tracks li")).to_have_text([f"{n}{title}Written" for n, title in enumerate(ALBUM, start=1)])


def test_a_burn_can_pick_its_speed_and_eject_the_disc_when_it_is_done(page, live, world) -> None:
    add_library(page, live)
    put_on_list(page, *ALBUM)
    open_burn(page)
    expect(page.get_by_label("Speed")).to_have_value("")
    page.get_by_label("Speed").select_option(label="8x: slowest, gentlest")
    expect(eject_box(page)).to_be_checked()  # (what Settings starts from)
    expect(start_button(page)).to_be_enabled()
    start_button(page).click()
    confirm(page, "Burn this music CD?", "Burn the disc")
    expect(page.locator(".callout", has_text="The music CD is ready")).to_be_visible(timeout=60_000)
    assert (world / "E" / "eject.log").exists(), "the disc came out"
    page.get_by_role("button", name="Burn something else").click()
    expect(disc_head(page)).to_contain_text("No disc in the drive")
    expect(page.locator(".burn-songs li")).to_have_count(3)  # (the list is kept: another copy is one click away)


def test_a_burn_that_is_stopped_says_what_became_of_the_disc(page, live, world) -> None:
    set_control(world, burn_slow=0.01)
    add_library(page, live)
    put_on_list(page, *ALBUM)
    open_burn(page)
    expect(start_button(page)).to_be_enabled()
    start_button(page).click()
    confirm(page, "Burn this music CD?", "Burn the disc")
    expect(page.locator(".job-banner")).to_contain_text("Writing to the disc", timeout=60_000)
    page.locator(".job-banner").get_by_role("button", name="Cancel").click()
    callout = page.locator(".callout.warn", has_text="Stopped")
    expect(callout).to_be_visible(timeout=30_000)
    expect(callout).to_contain_text("Writing had begun, so the disc is probably unusable now. A CD-R cannot be written twice; put in a new disc.")
    page.get_by_role("tab", name="History").click()
    expect(page.locator(".rip-row").first).to_contain_text("Stopped · disc spoiled")
    page.get_by_role("tab", name=BURN).click()
    page.get_by_role("button", name="Burn something else").click()
    expect(disc_head(page)).to_contain_text("with something on it")  # the disc is looked at again: it is no longer blank


def test_a_burn_that_is_still_going_is_picked_up_again_after_the_page_is_reloaded(page, live, world) -> None:
    set_control(world, burn_slow=0.01)
    add_library(page, live)
    put_on_list(page, *ALBUM)
    open_burn(page)
    expect(start_button(page)).to_be_enabled()
    eject_box(page).uncheck()
    start_button(page).click()
    confirm(page, "Burn this music CD?", "Burn the disc")
    expect(page.locator(".job-banner")).to_contain_text("Writing to the disc", timeout=60_000)
    page.reload()
    page.wait_for_selector(".sidebar")
    page.evaluate("location.hash = '#/discs'")
    page.get_by_role("tab", name=BURN).click()
    expect(page.locator("#burn-title")).to_have_text("Music CD of 3 songs")
    expect(page.locator(".job-banner")).to_be_visible()
    set_control(world)  # no longer slow
    expect(page.locator(".callout", has_text="The music CD is ready")).to_be_visible(timeout=60_000)


def test_a_list_that_does_not_fit_says_so_and_cannot_be_burned(page, live, world) -> None:
    add_drive(world, "E", name="Stand-in recorder", writer=True, disc=blank_disc(type="CD-R", capacity=5000))
    disc_helper.forget_listing()
    add_library(page, live)
    put_on_list(page, *ALBUM, "Drift", "Glacier", "Polar Night")
    open_burn(page)
    expect(disc_head(page)).to_contain_text("Blank CD-R, 1 min free")
    alert = page.locator(".callout[role=alert]", has_text="the disc has")
    expect(alert).to_be_visible()
    expect(alert).to_contain_text("Take out about")
    expect(start_button(page)).to_be_disabled()
    for _ in range(4):  # a shorter list fits
        page.get_by_role("button", name=re.compile("^Take .* off the list$")).last.click()
    expect(page.locator(".callout[role=alert]", has_text="the disc has")).to_have_count(0)
    expect(start_button(page)).to_be_enabled()


def test_a_disc_that_has_been_written_cannot_take_music_and_says_why(page, live, world) -> None:
    add_drive(world, "E", name="Stand-in recorder", writer=True, disc={**audio_disc(seed=2, tracks=(60, 80)), "type": "CD-R", "rewritable": False, "capacity": 359846})
    disc_helper.forget_listing()
    add_library(page, live)
    put_on_list(page, *ALBUM)
    open_burn(page)
    expect(page.locator(".callout[role=alert]", has_text="is not blank")).to_be_visible()
    expect(start_button(page)).to_be_disabled()
    expect(page.get_by_role("button", name="Erase this disc…")).to_be_disabled()  # (a CD-R cannot be erased)


# ---------------------------------------------------------------------------------------- a data disc


def test_a_data_disc_keeps_the_files_and_every_one_is_read_back(page, live, world) -> None:
    add_library(page, live)
    put_on_list(page, *ALBUM)
    open_burn(page)
    kind(page, "Data disc")
    expect(page.get_by_label("Layout on the disc")).to_have_value("library")
    expect(page.locator(".burn-songs li .reason")).to_have_text(["Will be copied"] * 3)
    expect(page.locator(".burn-songs li .mono")).to_have_text([f"Mara Quinn/Static Hearts/{n:02d} - {title}.mp3" for n, title in enumerate(ALBUM, start=1)])
    expect(page.get_by_label(re.compile("^Read every file back"))).to_be_checked()
    eject_box(page).uncheck()
    page.get_by_label("Name of the disc").fill("Road Trip")
    expect(start_button(page, "Burn the data disc…")).to_be_enabled()
    start_button(page, "Burn the data disc…").click()
    dialog = page.get_by_role("dialog", name="Burn this data disc?")
    expect(dialog).to_contain_text("3 files")
    expect(dialog).to_contain_text("closes the disc")
    expect(dialog).to_contain_text("every file is read back and compared")
    dialog.get_by_role("button", name="Burn the disc").click()
    expect(page.locator("#burn-title")).to_have_text("Data disc Road Trip (3 files)")
    expect(page.locator(".callout", has_text="every file was read back from the disc and matches")).to_be_visible(timeout=90_000)
    expect(page.locator(".toast", has_text="Burned 3 files")).to_be_visible()
    disc = disc_of(world)
    assert disc["kind"] == "data" and disc["label"] == "Road Trip" and len(disc["files"]) == 3
    for n, title in enumerate(ALBUM, start=1):
        written = world / "E" / "files" / "Mara Quinn" / "Static Hearts" / f"{n:02d} - {title}.mp3"
        original = next(live.library.rglob(f"{n:02d} - {title}.mp3"))
        assert written.read_bytes() == original.read_bytes()
    page.get_by_role("tab", name="History").click()
    expect(page.locator(".rip-row").first).to_contain_text("Done · checked")


def test_the_files_can_go_in_one_numbered_folder_for_a_car_stereo(page, live, world) -> None:
    add_library(page, live)
    put_on_list(page, *ALBUM)
    open_burn(page)
    kind(page, "Data disc")
    page.get_by_label("Layout on the disc").select_option("flat")
    expect(page.locator(".burn-songs li .mono")).to_have_text([f"{n:02d} - {title}.mp3" for n, title in enumerate(ALBUM, start=1)])


def test_a_disc_that_reads_back_wrong_is_reported_and_not_called_good(page, live, world) -> None:
    set_control(world, corrupt_burn=True)
    add_library(page, live)
    put_on_list(page, *ALBUM)
    open_burn(page)
    kind(page, "Data disc")
    eject_box(page).uncheck()
    expect(start_button(page, "Burn the data disc…")).to_be_enabled()
    start_button(page, "Burn the data disc…").click()
    confirm(page, "Burn this data disc?", "Burn the disc")
    callout = page.locator(".callout.warn")
    expect(callout).to_be_visible(timeout=90_000)
    expect(callout).to_contain_text("does not match")
    expect(page.locator(".callout", has_text="matches")).to_have_count(0)
    page.get_by_role("tab", name="History").click()
    expect(page.locator(".rip-row").first).to_contain_text("Failed")


# ---------------------------------------------------------------------------------------- an ISO file, with no disc at all


def test_a_disc_image_is_saved_without_a_disc_or_a_drive(page, live, world, tmp_path) -> None:
    (world / "E" / "disc.json").unlink()
    (world / "E" / "drive.json").unlink()
    (world / "E").rmdir()
    disc_helper.forget_listing()
    images = tmp_path / "images"
    images.mkdir()
    add_library(page, live)
    put_on_list(page, *ALBUM)
    open_burn(page)
    kind(page, "Disc image")
    expect(page.get_by_role("heading", name="The disc")).to_have_count(0)  # (no drive is asked about)
    page.get_by_label("Folder for the image").fill(str(images))
    page.get_by_label("Name of the disc").fill("Road Trip")
    expect(page.get_by_text("It will be saved as")).to_contain_text(str(images / "Road Trip.iso"))
    start_button(page, "Save the disc image…").click()
    dialog = page.get_by_role("dialog", name="Save the disc image?")
    expect(dialog).to_contain_text("Nothing that is there already is replaced")
    dialog.get_by_role("button", name="Save the image").click()
    expect(page.locator(".callout", has_text="The disc image was saved as")).to_be_visible(timeout=60_000)
    expect(page.locator(".toast", has_text="Saved the disc image")).to_be_visible()
    image = images / "Road Trip.iso"
    assert image.is_file() and image.stat().st_size % 2048 == 0
    assert sorted(p.name for p in images.iterdir()) == ["Road Trip.iso"]
    page.get_by_role("tab", name="History").click()
    expect(page.locator(".rip-row").first).to_contain_text("Disc image Road Trip (3 files)")


def test_a_folder_that_is_not_there_is_explained(page, live, world, tmp_path) -> None:
    page.allowed_statuses = {404, 409, 422}
    add_library(page, live)
    put_on_list(page, *ALBUM)
    open_burn(page)
    kind(page, "Disc image")
    page.get_by_label("Folder for the image").fill(str(tmp_path / "nowhere"))
    expect(page.locator(".callout[role=alert]", has_text="That folder is not there")).to_be_visible()
    expect(start_button(page, "Save the disc image…")).to_be_disabled()


# ---------------------------------------------------------------------------------------- erasing


def test_a_rewritable_disc_is_erased_after_a_question_that_says_what_it_does(page, live, world) -> None:
    add_drive(world, "E", name="Stand-in recorder", writer=True, disc={**audio_disc(seed=4, tracks=(60,)), "type": "CD-RW", "rewritable": True, "capacity": 359846})
    disc_helper.forget_listing()
    open_burn(page)
    expect(disc_head(page)).to_contain_text("CD-RW with something on it. It can be erased")
    page.get_by_role("button", name="Erase this disc…").click()
    dialog = page.get_by_role("dialog", name="Erase this disc?")
    expect(dialog).to_contain_text("Everything on the CD-RW in drive E: is deleted")
    expect(dialog).to_contain_text("cannot be undone")
    dialog.get_by_role("button", name="Cancel").click()
    assert disc_of(world)["kind"] == "audio", "a refused question erases nothing"

    page.get_by_role("button", name="Erase this disc…").click()
    confirm(page, "Erase this disc?", "Erase the disc")
    expect(page.locator("#burn-title")).to_have_text("Erase CD-RW")
    expect(page.locator(".callout", has_text="The disc was erased and is blank again")).to_be_visible(timeout=60_000)
    expect(page.locator(".job-banner")).to_have_count(0)
    assert disc_of(world)["kind"] == "blank"
    page.get_by_role("button", name="Done").click()
    expect(disc_head(page)).to_contain_text("Blank CD-RW, 1 hr 20 min free")
    expect(page.get_by_role("button", name="Erase this disc…")).to_be_disabled()  # (nothing left to erase)
    page.get_by_role("tab", name="History").click()
    expect(page.locator(".rip-row").first).to_contain_text("Erase CD-RW")


def test_an_erase_cannot_be_stopped_so_it_offers_no_stop(page, live, world) -> None:
    add_drive(world, "E", name="Stand-in recorder", writer=True, disc={**audio_disc(seed=4, tracks=(60,)), "type": "CD-RW", "rewritable": True, "capacity": 359846})
    set_control(world, burn_slow=0.05)  # (an erase takes five seconds)
    disc_helper.forget_listing()
    open_burn(page)
    page.get_by_role("button", name="Erase this disc…").click()
    confirm(page, "Erase this disc?", "Erase the disc")
    expect(page.locator(".job-banner")).to_be_visible()
    expect(page.locator(".job-banner").get_by_role("button", name="Cancel")).to_have_count(0)
    expect(page.locator(".callout", has_text="The disc was erased")).to_be_visible(timeout=60_000)


# ---------------------------------------------------------------------------------------- no recorder


def test_a_computer_without_a_recorder_says_so_and_can_still_make_an_image(page, live, tmp_path, monkeypatch) -> None:
    readers = tmp_path / "readers"
    add_drive(readers, "F", name="Stand-in reader", disc=audio_disc(seed=3, tracks=(60, 80)))
    monkeypatch.setenv("MTK_DISC_HELPER", json.dumps([sys.executable, str(FAKE), str(readers)]))
    disc_helper.forget_listing()
    add_library(page, live)
    put_on_list(page, *ALBUM)
    open_burn(page)
    expect(page.get_by_role("heading", name="No disc writer found")).to_be_visible()
    expect(start_button(page)).to_have_count(0)
    kind(page, "Disc image")
    expect(page.get_by_role("heading", name="No disc writer found")).to_have_count(0)
    expect(start_button(page, "Save the disc image…")).to_be_enabled()


def test_a_computer_without_the_disc_helper_says_why_and_still_offers_an_image(page, live, monkeypatch) -> None:
    monkeypatch.delenv("MTK_DISC_HELPER", raising=False)
    disc_helper.forget_listing()
    add_library(page, live)
    put_on_list(page, *ALBUM)
    page.evaluate("location.hash = '#/discs'")
    expect(page.get_by_role("heading", name="Discs", level=1)).to_be_visible()
    page.get_by_role("tab", name=BURN).click()
    expect(page.get_by_role("heading", name="Discs are not available here")).to_be_visible()
    kind(page, "Disc image")
    expect(page.get_by_role("heading", name="Discs are not available here")).to_have_count(0)
    expect(start_button(page, "Save the disc image…")).to_be_enabled()


# ---------------------------------------------------------------------------------------- settings


def test_the_settings_card_sets_what_burning_starts_from(page, live, world) -> None:
    add_library(page, live)
    page.evaluate("location.hash = '#/settings'")
    card = page.locator(".card-panel", has_text="what burning starts from")
    expect(card.get_by_role("checkbox", name="Eject a burned disc when it is done")).to_be_checked()
    card.get_by_role("checkbox", name="Eject a burned disc when it is done").uncheck()
    expect(page.locator(".toast", has_text="Saved").first).to_be_visible()
    card.get_by_role("checkbox", name=re.compile("^After burning a data disc")).uncheck()
    wait_until(page, "fetch('/api/settings').then(r => r.json()).then(s => s.disc.burn_verify === false)")
    assert (live.ctx.config.disc.eject_after_burn, live.ctx.config.disc.burn_verify) == (False, False)

    put_on_list(page, *ALBUM)
    open_burn(page)
    expect(eject_box(page)).not_to_be_checked()
    kind(page, "Data disc")
    expect(page.get_by_role("checkbox", name=re.compile("^Read every file back"))).not_to_be_checked()


def test_diagnostics_say_which_drives_can_write(page, live, world) -> None:
    page.evaluate("location.hash = '#/settings'")
    page.get_by_role("button", name="Run diagnostics").click()
    row_ = page.locator(".diag", has_text="CD and DVD drives")
    expect(row_).to_contain_text("E: Stand-in recorder (disc not recognised, writes CD-R, CD-RW)")


# ---------------------------------------------------------------------------------------- the keyboard


def test_the_whole_burn_can_be_done_from_the_keyboard(page, live, world) -> None:
    add_library(page, live)
    put_on_list(page, *ALBUM)
    open_burn(page)
    expect(start_button(page)).to_be_enabled()

    page.get_by_role("radio", name=re.compile("^Music CD")).focus()
    page.keyboard.press("ArrowDown")
    expect(page.get_by_role("radio", name=re.compile("^Data disc"))).to_be_checked()
    page.keyboard.press("ArrowUp")
    expect(page.get_by_role("radio", name=re.compile("^Music CD"))).to_be_checked()

    page.get_by_role("button", name=f"Move {ALBUM[1]} up").focus()
    page.keyboard.press("Enter")
    expect_titles(page, [ALBUM[1], ALBUM[0], ALBUM[2]])
    expect(page.get_by_role("button", name=f"Move {ALBUM[1]} down")).to_be_focused()  # (it is first now: the button that can still be used has the keyboard)
    page.keyboard.press("Enter")
    expect_titles(page, ALBUM)

    button = start_button(page)
    expect(button).to_be_enabled()
    button.focus()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog", name="Burn this music CD?")
    expect(dialog).to_be_visible()
    expect(dialog.locator(":focus")).to_have_count(1)  # (the keyboard is in the question)
    for _ in range(3):
        if dialog.get_by_role("button", name="Burn the disc").evaluate("(el) => el === document.activeElement"):
            break
        page.keyboard.press("Tab")
    expect(dialog.get_by_role("button", name="Burn the disc")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#burn-title")).to_be_focused()  # the keyboard follows the page to what is happening
    expect(page.locator(".callout", has_text="The music CD is ready")).to_be_visible(timeout=60_000)
    page.get_by_role("button", name="Burn something else").focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("radiogroup", name="What to make")).to_be_visible()


# ---------------------------------------------------------------------------------------- accessibility


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_every_state_of_the_burn_tab_passes_an_automated_accessibility_check(page, live, tmp_path, world, monkeypatch, theme) -> None:
    """axe (see test_accessibility.py) over the Burn tab as a person meets it: no helper, no recorder, an empty list, a checked list, a list that
    does not fit, a data disc, a disc image, the questions before a burn and an erase, a burn under way, stopped and finished, an erase, and the
    history with its details."""
    accessibility = pytest.importorskip("tests.e2e.test_accessibility", reason="pip install -e '.[e2e]' for the accessibility checks")
    page.allowed_statuses = {404, 409, 422}
    page.emulate_media(reduced_motion="reduce")
    page.evaluate("(theme) => import('/static/js/state.js').then((s) => s.setTheme(theme))", theme)
    add_library(page, live)
    problems: list[str] = []

    def look(name: str, *, still: bool = True, open_dialog: bool = False) -> None:
        # (CLEAR closes every dialog: a question that is open to be looked at only has its toasts put away)
        page.evaluate("() => import('/static/js/state.js').then((s) => s.toasts.set({ items: [] }))" if open_dialog else accessibility.CLEAR)
        if still:
            accessibility.settled(page)
        accessibility.has_a_heading(page, name)
        problems.extend(f"[{name}] {line}" for line in accessibility.violations(page))

    put_on_list(page, *ALBUM)
    readers = tmp_path / "readers"
    add_drive(readers, "F", name="Stand-in reader", disc=audio_disc(seed=3, tracks=(60, 80)))
    recorder = {"name": "Stand-in recorder", "writer": True}

    # no helper at all
    helper = json.dumps([sys.executable, str(FAKE), str(world)])
    monkeypatch.delenv("MTK_DISC_HELPER")
    disc_helper.forget_listing()
    open_burn(page)
    expect(page.get_by_role("heading", name="Discs are not available here")).to_be_visible()
    look("the discs are not available")

    # a helper, but no recorder
    monkeypatch.setenv("MTK_DISC_HELPER", json.dumps([sys.executable, str(FAKE), str(readers)]))
    disc_helper.forget_listing()
    page.reload()
    page.wait_for_selector(".sidebar")
    open_burn(page)
    expect(page.get_by_role("heading", name="No disc writer found")).to_be_visible()
    look("no recorder")

    # a recorder with a blank disc, and nothing on the list
    monkeypatch.setenv("MTK_DISC_HELPER", helper)
    disc_helper.forget_listing()
    page.evaluate("import('/static/js/state.js').then((s) => s.clearBurnList())")
    page.get_by_role("button", name="Look again").click()
    expect(disc_head(page)).to_contain_text("Blank CD-R, 1 hr 20 min free")
    expect(page.get_by_role("heading", name="No songs on the list yet")).to_be_visible()
    look("an empty list")

    # a list that is checked
    put_on_list(page, *ALBUM)
    expect(page.locator(".capacity-row")).to_contain_text("of 1 hr 20 min")
    expect(start_button(page)).to_be_enabled()
    look("a checked list")

    # a list that does not fit
    add_drive(world, "E", **recorder, disc=blank_disc(type="CD-R", capacity=2400))  # (the three songs take 3000 sectors)
    disc_helper.forget_listing()
    page.get_by_role("button", name="Look again").click()
    expect(page.locator(".callout[role=alert]", has_text="the disc has")).to_be_visible()
    look("a list that does not fit")
    add_drive(world, "E", **recorder, disc=blank_disc(type="CD-R"))
    disc_helper.forget_listing()
    page.get_by_role("button", name="Look again").click()
    expect(start_button(page)).to_be_enabled()

    # a data disc, and a disc image
    kind(page, "Data disc")
    expect(page.locator(".burn-songs li .reason").first).to_have_text("Will be copied")
    expect(start_button(page, "Burn the data disc…")).to_be_enabled()
    look("a data disc")
    kind(page, "Disc image")
    images = tmp_path / "images"
    images.mkdir()
    page.get_by_label("Folder for the image").fill(str(images))
    expect(page.get_by_text("It will be saved as")).to_be_visible()
    expect(start_button(page, "Save the disc image…")).to_be_enabled()
    look("a disc image")
    kind(page, "Music CD")
    expect(start_button(page)).to_be_enabled()

    # the question before a burn
    start_button(page).click()
    expect(page.get_by_role("dialog", name="Burn this music CD?")).to_be_visible()
    look("the question before a burn", still=False, open_dialog=True)
    page.get_by_role("dialog").get_by_role("button", name="Cancel").click()

    # a burn under way, and one that was stopped
    set_control(world, burn_slow=0.01)
    start_button(page).click()
    confirm(page, "Burn this music CD?", "Burn the disc")
    expect(page.locator(".job-banner")).to_contain_text("Writing to the disc", timeout=60_000)
    look("a burn under way", still=False)
    page.locator(".job-banner").get_by_role("button", name="Cancel").click()
    expect(page.locator(".callout.warn", has_text="Stopped")).to_be_visible(timeout=30_000)
    look("a burn that was stopped")
    page.get_by_role("button", name="Burn something else").click()

    # a rewritable disc with something on it: the erase, and its question
    set_control(world)
    add_drive(world, "E", **recorder, disc={**audio_disc(seed=4, tracks=(60,)), "type": "CD-RW", "rewritable": True, "capacity": 359846})
    disc_helper.forget_listing()
    page.get_by_role("button", name="Look again").click()
    expect(page.get_by_role("button", name="Erase this disc…")).to_be_enabled()
    look("a disc that can be erased")
    page.get_by_role("button", name="Erase this disc…").click()
    expect(page.get_by_role("dialog", name="Erase this disc?")).to_be_visible()
    look("the question before an erase", still=False, open_dialog=True)
    page.get_by_role("dialog").get_by_role("button", name="Erase the disc").click()
    expect(page.locator(".callout", has_text="The disc was erased")).to_be_visible(timeout=60_000)
    look("an erase that finished")
    page.get_by_role("button", name="Done").click()

    # a burn that finished
    expect(start_button(page)).to_be_enabled()
    eject_box(page).uncheck()
    start_button(page).click()
    confirm(page, "Burn this music CD?", "Burn the disc")
    expect(page.locator(".callout", has_text="The music CD is ready")).to_be_visible(timeout=60_000)
    look("a burn that finished")

    # the history
    page.get_by_role("tab", name="History").click()
    expect(page.locator(".rip-row")).to_have_count(3)
    look("the history")
    page.locator(".rip-row").first.get_by_role("button", name="Details").click()
    expect(page.locator(".rip-row").first.locator(".rip-tracks li")).to_have_count(3)
    look("the history, with the details of a burn")
    page.evaluate(accessibility.CLEAR)

    assert not problems, f"{len(problems)} accessibility problems in the {theme} theme:\n" + "\n".join(problems)
