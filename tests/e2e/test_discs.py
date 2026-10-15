"""The Discs screen in a real browser: a stand-in drive (tests/fake_disc_helper.py) holds a disc of known audio, a stand-in MusicBrainz knows
what it is, and the real UI finds the disc, finds the album, shows where the songs will go, rips, stops, takes back."""

from __future__ import annotations

import contextlib
import json
import re
import sys
import time
from pathlib import Path

import pytest
import send2trash
import soundfile as sf
from playwright.sync_api import expect

from musictoolkit.discs import helper as disc_helper
from musictoolkit.discs.toc import Toc, Track
from tests.e2e.conftest import add_library, wait_until
from tests.fake_disc_helper import add_drive, audio, audio_disc, set_control
from tests.fake_musicbrainz import musicbrainz_script, release_xml
from tests.fake_streams import serve_script

expect.set_options(timeout=15_000)

FAKE = Path(__file__).resolve().parent.parent / "fake_disc_helper.py"
SEED = 6
SECTORS = (60, 80, 70)
STARTS = (0, 60, 140)
TOC = Toc((Track(1, 0), Track(2, 60), Track(3, 140)), 210)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Drive E: with an audio disc in it, drive F: with none."""
    root = tmp_path / "drives"
    add_drive(root, "E", disc=audio_disc(seed=SEED, tracks=SECTORS, label="Audio CD"))
    monkeypatch.setenv("MTK_DISC_HELPER", json.dumps([sys.executable, str(FAKE), str(root)]))
    disc_helper.forget_listing()
    yield root
    disc_helper.forget_listing()


@pytest.fixture
def recycle(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A stand-in for the Recycle Bin: the file is removed and noted."""
    binned: list[str] = []

    def fake(path: str) -> None:
        binned.append(path)
        Path(path).unlink()

    monkeypatch.setattr(send2trash, "send2trash", fake)
    return binned


def albums(*titles: str, exact: bool = True):
    """What the stand-in MusicBrainz knows about the disc: each title is an album with the same three songs."""
    return [
        release_xml(key=f"album-{i}", title=title, artist="Real Artist", disc_ids=(TOC.disc_id(),) if exact and i == 0 else (), tracks=(("One", 1000), ("Two", 2000), ("Three", 3000)))
        for i, title in enumerate(titles)
    ]


@contextlib.contextmanager
def musicbrainz(live, monkeypatch: pytest.MonkeyPatch, *releases: str):
    """MusicBrainz, reachable, with a contact to ask with, knowing these releases for the disc."""
    live.ctx.config.musicbrainz.contact = "tests@example.com"
    with serve_script(musicbrainz_script(known={TOC.disc_id(): list(releases)} if releases else {})) as server:
        monkeypatch.setenv("MTK_MUSICBRAINZ_URL", server.url)
        yield server


def open_discs(page) -> None:
    page.evaluate("location.hash = '#/discs'")
    expect(page.get_by_role("heading", name="Discs", level=1)).to_be_visible()


def album_dir(live) -> Path:
    return live.library / "Real Artist" / "The Real Name"


def decoded(path: Path) -> bytes:
    return sf.read(str(path), dtype="int16", always_2d=True)[0].tobytes()


def plan_paths(page) -> list[str]:
    return page.locator(".sync-table .where").all_inner_texts()


def start_button(page, songs: int = 3):
    return page.get_by_role("button", name=f"Rip {songs} songs")


# ---------------------------------------------------------------------------------------- finding the disc and the album


def test_a_disc_in_the_drive_is_found_the_album_looked_up_and_the_songs_ripped(page, live, world, monkeypatch) -> None:
    add_library(page, live)
    with musicbrainz(live, monkeypatch, *albums("The Real Name")):
        open_discs(page)
        expect(page.locator(".disc-head")).to_contain_text("Audio CD · 3 songs")
        expect(page.get_by_role("radio", name=re.compile(r"^The Real Name"))).to_be_checked()
        expect(page.get_by_label("Album", exact=True)).to_have_value("The Real Name")
        expect(page.get_by_label("Album artist")).to_have_value("Real Artist")
        expect(page.get_by_label("Title of song 2")).to_have_value("Two")
        expect(page.locator(".sync-table .where")).to_have_text(
            ["Real Artist/The Real Name/01 - One.flac", "Real Artist/The Real Name/02 - Two.flac", "Real Artist/The Real Name/03 - Three.flac"]
        )
        assert not album_dir(live).exists(), "nothing is written before the rip is started"

        start_button(page).click()
        expect(page.locator("#rip-title")).to_have_text("The Real Name · Real Artist")
        expect(page.locator(".callout", has_text="Ripped 3 songs")).to_be_visible(timeout=60_000)
        expect(page.locator(".rip-tracks li.done")).to_have_count(3)

    for number, name in ((1, "One"), (2, "Two"), (3, "Three")):
        assert decoded(album_dir(live) / f"0{number} - {name}.flac") == audio(SEED, STARTS[number - 1], SECTORS[number - 1]), f"{name} is not what was on the disc"
    expect(page.locator(".toast", has_text="Ripped 3 songs")).to_be_visible()
    page.get_by_role("button", name="Show in my library").click()
    wait_until(page, "location.hash.startsWith('#/search/')")
    page.get_by_role("link", name="The Real Name").first.wait_for()


def test_a_title_can_be_corrected_and_a_song_left_out_before_ripping(page, live, world, monkeypatch) -> None:
    add_library(page, live)
    with musicbrainz(live, monkeypatch, *albums("The Real Name")):
        open_discs(page)
        expect(page.get_by_label("Title of song 2")).to_have_value("Two")
        page.get_by_label("Title of song 2").fill("Second Thoughts")
        page.get_by_label("Rip song 3").uncheck()
        expect(page.locator(".sync-table .where").nth(1)).to_have_text("Real Artist/The Real Name/02 - Second Thoughts.flac")
        expect(page.get_by_role("button", name="Rip 2 songs")).to_be_enabled()
        expect(page.locator(".stat", has_text="left out")).to_contain_text("1")

        page.get_by_role("button", name="Rip 2 songs").click()
        expect(page.locator(".callout", has_text="Ripped 2 songs")).to_be_visible(timeout=60_000)

    assert sorted(p.name for p in album_dir(live).iterdir()) == ["01 - One.flac", "02 - Second Thoughts.flac"]
    expect(page.locator(".rip-tracks li").nth(2)).to_contain_text("Left out")


def test_the_albums_musicbrainz_offers_can_be_chosen_between_or_none_of_them(page, live, world, monkeypatch) -> None:
    add_library(page, live)
    with musicbrainz(live, monkeypatch, *albums("The Real Name", "The Real Name (Deluxe)")):
        open_discs(page)
        expect(page.get_by_role("radio")).to_have_count(3)
        expect(page.locator(".candidate").first).to_contain_text("this very disc")
        expect(page.locator(".candidate").nth(1)).to_contain_text("similar disc")

        page.get_by_role("radio", name=re.compile(r"Deluxe")).check()
        expect(page.get_by_label("Album", exact=True)).to_have_value("The Real Name (Deluxe)")
        page.get_by_role("radio", name=re.compile(r"None of these")).check()
        expect(page.get_by_label("Album", exact=True)).to_have_value("")
        expect(page.get_by_label("Title of song 1")).to_have_value("Track 01")
        expect(page.locator(".sync-table .where").first).to_have_text("Unknown Artist/Unknown Album/01 - Track 01.flac")


def test_a_disc_nobody_knows_is_typed_in_by_hand_and_the_paths_follow(page, live, world, monkeypatch) -> None:
    add_library(page, live)
    with musicbrainz(live, monkeypatch):
        open_discs(page)
        expect(page.get_by_text("MusicBrainz does not know this disc")).to_be_visible()
        expect(page.get_by_role("link", name=re.compile("Add it"))).to_have_attribute("href", re.compile(r"musicbrainz\.org/cdtoc/attach"))
        page.get_by_label("Album artist").fill("My Band")
        page.get_by_label("Album", exact=True).fill("Live in the Garage")
        page.get_by_label("Title of song 1").fill("Warm Up")
        expect(page.locator(".sync-table .where").first).to_have_text("My Band/Live in the Garage/01 - Warm Up.flac")
        page.get_by_label("Year").fill("1999")
        page.get_by_role("button", name="Rip 3 songs").click()
        expect(page.locator(".callout", has_text="Ripped 3 songs")).to_be_visible(timeout=60_000)
    assert (live.library / "My Band" / "Live in the Garage" / "01 - Warm Up.flac").exists()


def test_without_a_contact_the_screen_says_how_to_find_albums_and_typing_still_works(page, live, world) -> None:
    add_library(page, live)
    assert not live.ctx.config.musicbrainz.contact
    asked: list[str] = []
    page.on("request", lambda r: asked.append(f"{r.method} {r.url.split('/api/')[-1]}") if "/api/discs" in r.url else None)
    open_discs(page)
    note = page.locator(".note", has_text="add your email address")
    expect(note).to_be_visible()
    expect(note.get_by_role("link", name="Settings")).to_have_attribute("href", "#/settings")
    page.get_by_label("Album", exact=True).fill("By Hand")
    page.get_by_label("Album artist").fill("Me")
    try:
        expect(page.locator(".sync-table .where").first).to_have_text("Me/By Hand/01 - Track 01.flac")
    except AssertionError as error:  # (a failure seen only on a slow machine: say what the page was doing)
        fields = page.evaluate("Array.from(document.querySelectorAll('.organize-form input'), (i) => i.value)")
        raise AssertionError(f"{error}\nthe first fields of the form: {fields}\nwhat the page asked the server for, in order: {asked}") from None


LISTING = re.compile(r".*/api/discs/drives(\?.*)?$")


def test_what_was_typed_stays_when_a_look_at_the_drives_fails_once(page, live, world) -> None:
    add_library(page, live)
    open_discs(page)
    expect(page.get_by_label("Title of song 1")).to_be_visible()
    page.get_by_label("Album", exact=True).fill("By Hand")
    failed = []

    def glitch(route) -> None:
        if not failed:
            failed.append(True)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"available": True, "reason": None, "error": "The disc helper did not answer.", "drives": [], "busy": False}))
        else:
            route.continue_()

    page.route(LISTING, glitch)
    page.evaluate("window.dispatchEvent(new Event('focus'))")  # a look at the drives, now
    expect(page.locator(".callout[role=alert]", has_text="did not answer")).to_be_visible()
    expect(page.get_by_label("Album", exact=True)).to_have_value("By Hand")  # nothing that was on the screen is thrown away because one look failed
    expect(page.locator(".callout[role=alert]", has_text="did not answer")).to_have_count(0, timeout=15_000)  # the next look is fine, and the trouble goes
    expect(page.get_by_label("Album", exact=True)).to_have_value("By Hand")


def test_what_was_typed_stays_when_the_drive_loses_the_disc_for_a_moment(page, live, world) -> None:
    add_library(page, live)
    open_discs(page)
    expect(page.get_by_label("Title of song 1")).to_be_visible()
    page.get_by_label("Album", exact=True).fill("By Hand")
    flapped = []

    def flap(route) -> None:
        response = route.fetch()
        data = response.json()
        if not flapped:
            flapped.append(True)
            for drive in data["drives"]:
                drive["media"] = "unknown"  # the drive cannot say what is in it, once
        route.fulfill(response=response, json=data)

    page.route(LISTING, flap)
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page.get_by_role("heading", name="This drive cannot read the disc")).to_be_visible()
    expect(page.get_by_label("Album", exact=True)).to_have_value("By Hand", timeout=15_000)  # and when the same disc is back, so is what was typed


def test_a_drive_that_loses_the_disc_for_a_moment_does_not_make_the_app_ask_musicbrainz_again(page, live, world, monkeypatch) -> None:
    add_library(page, live)
    live.ctx.config.musicbrainz.contact = "tests@example.com"
    answer = musicbrainz_script(known={TOC.disc_id(): albums("The Real Name")})

    def slow(h) -> None:  # MusicBrainz takes its time: the drive loses the disc while the app is waiting for it
        time.sleep(3)
        answer(h)

    flapped = []

    def flap(route) -> None:
        response = route.fetch()
        data = response.json()
        if flapped:
            for drive in data["drives"]:
                drive["media"] = "unknown"
            flapped.pop()
        route.fulfill(response=response, json=data)

    with serve_script(slow) as server:
        monkeypatch.setenv("MTK_MUSICBRAINZ_URL", server.url)
        page.route(LISTING, flap)
        open_discs(page)
        expect(page.locator(".status-line", has_text="Looking the disc up on MusicBrainz")).to_be_visible()
        flapped.append(True)
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(page.get_by_role("heading", name="This drive cannot read the disc")).to_be_visible()
        expect(page.get_by_role("radio", name=re.compile(r"^The Real Name"))).to_be_visible(timeout=30_000)  # the answer of the one question that was asked
        assert len(server.requests) == 1, f"MusicBrainz was asked {len(server.requests)} times: {[r['path'] for r in server.requests]}"


def test_coming_back_to_the_page_does_not_ask_musicbrainz_again_about_a_disc_it_knows(page, live, world, monkeypatch) -> None:
    add_library(page, live)
    with musicbrainz(live, monkeypatch, *albums("The Real Name")) as server:
        open_discs(page)
        expect(page.get_by_role("radio", name=re.compile(r"^The Real Name"))).to_be_visible()
        page.evaluate("location.hash = '#/settings'")
        expect(page.get_by_role("heading", name="Appearance", exact=True)).to_be_visible()
        page.evaluate("location.hash = '#/discs'")
        expect(page.get_by_role("radio", name=re.compile(r"^The Real Name"))).to_be_visible()
        assert len(server.requests) == 1, [r["path"] for r in server.requests]


def test_a_disc_taken_out_and_put_back_starts_over(page, live, world) -> None:
    page.allowed_statuses = {404, 409}  # (looking at the disc again, just as it was taken out, is refused; 404: the pictures of songs without one)
    add_library(page, live)
    open_discs(page)
    expect(page.get_by_label("Title of song 1")).to_be_visible()
    page.get_by_label("Album", exact=True).fill("By Hand")
    ejected = (world / "E" / "disc.json").read_text()
    (world / "E" / "disc.json").unlink()
    disc_helper.forget_listing()
    page.get_by_role("button", name="Look again").or_(page.get_by_role("button", name="Read the disc again")).first.click()
    expect(page.get_by_role("heading", name="Put an audio CD in the drive")).to_be_visible()
    (world / "E" / "disc.json").write_text(ejected)
    disc_helper.forget_listing()
    page.get_by_role("button", name="Read the disc again").click()
    expect(page.get_by_label("Title of song 1")).to_be_visible()
    expect(page.get_by_label("Album", exact=True)).to_have_value("")  # the tray was empty in between: a new start


def test_every_song_can_be_left_out_at_once_and_put_back(page, live, world) -> None:
    add_library(page, live)
    open_discs(page)
    expect(page.get_by_label("Title of song 1")).to_be_visible()
    expect(page.get_by_role("button", name="Rip 3 songs")).to_be_enabled()
    page.get_by_label("Rip every song").uncheck()
    expect(page.get_by_label("Rip song 2")).not_to_be_checked()
    expect(page.locator(".callout", has_text="nothing to rip")).to_be_visible()
    expect(page.get_by_role("button", name="Rip", exact=True)).to_be_disabled()
    page.get_by_label("Rip song 2").check()
    expect(page.get_by_role("button", name="Rip 1 song")).to_be_enabled()
    page.get_by_label("Rip every song").check()
    expect(page.get_by_role("button", name="Rip 3 songs")).to_be_enabled()


def test_a_busy_musicbrainz_is_explained_and_typing_still_works(page, live, world, monkeypatch) -> None:
    add_library(page, live)
    live.ctx.config.musicbrainz.contact = "tests@example.com"
    with serve_script(musicbrainz_script(status=503)) as server:
        monkeypatch.setenv("MTK_MUSICBRAINZ_URL", server.url)
        open_discs(page)
        expect(page.locator(".callout[role=alert]", has_text="MusicBrainz is busy")).to_be_visible(timeout=60_000)
        page.get_by_label("Album", exact=True).fill("By Hand")
        expect(page.get_by_role("button", name="Rip 3 songs")).to_be_enabled()
        page.get_by_role("button", name="Ask MusicBrainz again").click()
        expect(page.locator(".callout[role=alert]", has_text="MusicBrainz is busy")).to_be_visible(timeout=60_000)


def test_the_songs_of_a_compilation_can_have_their_own_artists(page, live, world, monkeypatch) -> None:
    add_library(page, live)
    with musicbrainz(live, monkeypatch, *albums("The Real Name")):
        open_discs(page)
        expect(page.get_by_label("Title of song 1")).to_have_value("One")
        expect(page.get_by_label("Artist of song 1")).to_have_count(0)
        page.get_by_label("The songs have different artists").check()
        page.get_by_label("Artist of song 2").fill("Somebody Else")
        page.get_by_role("button", name="Rip 3 songs").click()
        expect(page.locator(".callout", has_text="Ripped 3 songs")).to_be_visible(timeout=60_000)
    from mutagen.flac import FLAC

    assert FLAC(album_dir(live) / "02 - Two.flac")["artist"] == ["Somebody Else"]
    assert FLAC(album_dir(live) / "02 - Two.flac")["albumartist"] == ["Real Artist"]


# ---------------------------------------------------------------------------------------- what the drive can be in


def test_discs_has_its_place_in_the_sidebar(page, live, world) -> None:
    add_library(page, live)
    link = page.locator(".sidebar").get_by_role("link", name="Discs")
    link.click()
    expect(page.get_by_role("heading", name="Discs", level=1)).to_be_visible()
    expect(link).to_have_attribute("aria-current", "page")


def test_no_drive_says_so_and_looking_again_finds_one_that_is_plugged_in(page, live, tmp_path, monkeypatch) -> None:
    root = tmp_path / "empty-world"
    root.mkdir()
    monkeypatch.setenv("MTK_DISC_HELPER", json.dumps([sys.executable, str(FAKE), str(root)]))
    disc_helper.forget_listing()
    add_library(page, live)
    open_discs(page)
    expect(page.get_by_role("heading", name="No CD or DVD drive found")).to_be_visible()
    add_drive(root, "E")  # the person plugs a drive in
    page.get_by_role("button", name="Look again").click()
    expect(page.get_by_role("heading", name="Put an audio CD in the drive")).to_be_visible()


def test_a_disc_put_in_while_the_page_is_open_is_noticed(page, live, world, monkeypatch) -> None:
    (world / "E" / "disc.json").unlink()  # the tray is empty
    disc_helper.forget_listing()
    add_library(page, live)
    open_discs(page)
    expect(page.get_by_role("heading", name="Put an audio CD in the drive")).to_be_visible()

    add_drive(world, "E", disc=audio_disc(seed=SEED, tracks=SECTORS, label="Audio CD"))  # the disc goes in
    expect(page.locator(".disc-head")).to_contain_text("Audio CD · 3 songs", timeout=20_000)
    expect(page.get_by_label("Title of song 1")).to_have_value("Track 01")


def test_a_data_disc_and_a_disc_that_cannot_be_read_are_not_taken_for_music(page, live, world) -> None:
    add_drive(world, "E", disc={"kind": "data", "label": "BACKUP", "seed": 0, "tracks": [{"number": 1, "audio": False, "sectors": 500}]})
    disc_helper.forget_listing()
    add_library(page, live)
    open_discs(page)
    expect(page.get_by_role("heading", name="This is a data disc, not a music CD")).to_be_visible()
    expect(page.get_by_text("“BACKUP” holds files, not songs")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile(r"^Rip "))).to_have_count(0)


def test_a_computer_without_the_disc_helper_says_why(page, live, monkeypatch) -> None:
    monkeypatch.delenv("MTK_DISC_HELPER", raising=False)
    disc_helper.forget_listing()
    open_discs(page)
    expect(page.get_by_role("heading", name="Discs are not available here")).to_be_visible()
    expect(page.locator(".empty-body")).not_to_be_empty()


def test_several_drives_can_be_chosen_between(page, live, world) -> None:
    add_drive(world, "D", name="First drive, empty")
    add_drive(world, "F", name="Second drive")
    disc_helper.forget_listing()
    add_library(page, live)
    open_discs(page)
    expect(page.get_by_label("Drive")).to_have_value("E:")  # the one with music in it, not the first one
    page.get_by_label("Drive").select_option("F:")
    expect(page.get_by_role("heading", name="Put an audio CD in the drive")).to_be_visible()
    page.get_by_label("Drive").select_option("E:")
    expect(page.locator(".disc-head")).to_contain_text("3 songs")


# ---------------------------------------------------------------------------------------- when it goes wrong, stopping, taking back


def test_a_rip_can_be_stopped_and_what_was_done_is_kept_and_can_be_taken_back(page, live, world, recycle, monkeypatch) -> None:
    set_control(world, slow=0.03)
    add_library(page, live)
    with musicbrainz(live, monkeypatch, *albums("The Real Name")):
        open_discs(page)
        start_button(page).click()
        expect(page.locator(".rip-tracks li.ripping")).to_be_visible(timeout=30_000)
        expect(page.locator(".rip-tracks li.done").first).to_be_visible(timeout=60_000)  # the first song is finished: something is kept
        page.locator(".job-banner").get_by_role("button", name="Cancel").click()
        expect(page.locator(".callout", has_text="Stopped")).to_be_visible(timeout=30_000)

    kept = sorted(p.name for p in album_dir(live).glob("*.flac"))
    assert kept and not list(album_dir(live).glob("*.part")), f"what was finished is kept, and no half-written song is left: {sorted(str(p) for p in live.library.rglob('*') if 'Real Artist' in str(p) or p.suffix in ('.flac', '.part'))}"
    expect(page.locator(".rip-tracks li.done")).to_have_count(len(kept))
    expect(page.locator(".job-banner")).to_have_count(0)  # (the page has heard that the rip is over: what follows is the taking back, and nothing else, refreshing the rip)

    page.get_by_role("button", name="Take this rip back").click()
    page.get_by_role("dialog").get_by_role("button", name=re.compile(r"^Move \d+ songs? to the Recycle Bin")).click()
    expect(page.locator(".toast", has_text="to the Recycle Bin")).to_be_visible(timeout=30_000)
    expect(page.locator(".callout", has_text="This rip has been taken back")).to_be_visible(timeout=15_000)
    expect(page.get_by_role("button", name="Take this rip back")).to_have_count(0)
    wait_until(page, "fetch('/api/discs/rips').then(r => r.json()).then(d => !!d.rips[0].undone_at)", 30)
    assert sorted(Path(p).name for p in recycle) == kept
    assert not list(live.library.rglob("*.flac"))


def test_a_rip_that_is_still_going_is_picked_up_again_after_the_page_is_reloaded(page, live, world, monkeypatch) -> None:
    set_control(world, slow=0.03)
    add_library(page, live)
    with musicbrainz(live, monkeypatch, *albums("The Real Name")):
        open_discs(page)
        start_button(page).click()
        expect(page.locator(".rip-tracks li.ripping")).to_be_visible(timeout=30_000)
        page.reload()
        page.wait_for_selector(".sidebar")
        expect(page.locator("#rip-title")).to_have_text("The Real Name · Real Artist")
        expect(page.locator(".job-banner")).to_be_visible()
        set_control(world)  # no longer slow
        expect(page.locator(".callout", has_text="Ripped 3 songs")).to_be_visible(timeout=60_000)


def test_a_disc_with_trouble_stays_in_the_drive_and_the_songs_concerned_are_marked(page, live, world, monkeypatch) -> None:
    set_control(world, flaky=[{"from": 70, "to": 70, "times": 100, "varies": True}])  # a part of song 2 that never reads the same way twice
    add_library(page, live)
    with musicbrainz(live, monkeypatch, *albums("The Real Name")):
        open_discs(page)
        expect(page.get_by_label("Eject the disc when it is done")).to_be_checked()
        start_button(page).click()
        expect(page.locator(".callout", has_text="Ripped 3 songs")).to_be_visible(timeout=60_000)
        callout = page.locator(".callout", has_text="Ripped 3 songs")
        expect(callout).to_contain_text("could not be read perfectly")
        second = page.locator(".rip-tracks li").nth(1)
        expect(second).to_contain_text("Done, with trouble")
        expect(second).to_contain_text("could not be confirmed")
    assert not (world / "E" / "eject.log").exists(), "a disc that had trouble is left in the drive"
    page.get_by_role("tab", name="History").click()
    expect(page.locator(".rip-row").first).to_contain_text("1 doubtful part")


def test_a_clean_rip_ejects_the_disc_when_asked(page, live, world, monkeypatch) -> None:
    add_library(page, live)
    with musicbrainz(live, monkeypatch, *albums("The Real Name")):
        open_discs(page)
        expect(page.get_by_label("Eject the disc when it is done")).to_be_checked()
        start_button(page).click()
        expect(page.locator(".callout", has_text="Ripped 3 songs")).to_be_visible(timeout=60_000)
    assert (world / "E" / "eject.log").exists()
    page.get_by_role("button", name="Rip another disc").click()
    expect(page.get_by_role("heading", name="Put an audio CD in the drive")).to_be_visible()


def test_a_rip_that_cannot_start_says_why_and_leaves_the_choices_alone(page, live, world, monkeypatch) -> None:
    page.allowed_statuses = {404, 409, 422}
    add_library(page, live)
    album_dir(live).mkdir(parents=True)
    for name in ("01 - One.flac", "02 - Two.flac", "03 - Three.flac"):
        (album_dir(live) / name).write_bytes(b"mine")
    with musicbrainz(live, monkeypatch, *albums("The Real Name")):
        open_discs(page)
        expect(page.locator(".callout", has_text="nothing to rip")).to_be_visible()
        expect(page.get_by_role("button", name="Rip", exact=True)).to_be_disabled()
        expect(page.locator(".stat", has_text="already there")).to_contain_text("3")
        page.get_by_label("Album", exact=True).fill("Another Name")
        expect(page.get_by_role("button", name="Rip 3 songs")).to_be_enabled()
    assert all((album_dir(live) / name).read_bytes() == b"mine" for name in ("01 - One.flac", "02 - Two.flac", "03 - Three.flac"))


def test_what_was_typed_is_not_replaced_when_the_album_arrives_late(page, live, world, monkeypatch) -> None:
    add_library(page, live)
    live.ctx.config.musicbrainz.contact = "tests@example.com"
    answer = musicbrainz_script(known={TOC.disc_id(): albums("The Real Name")})

    def slow(h) -> None:  # MusicBrainz takes its time
        time.sleep(2)
        answer(h)

    with serve_script(slow) as server:
        monkeypatch.setenv("MTK_MUSICBRAINZ_URL", server.url)
        open_discs(page)
        expect(page.locator(".status-line", has_text="Looking the disc up on MusicBrainz")).to_be_visible()
        expect(page.get_by_role("button", name=re.compile(r"^Rip"))).to_be_disabled()  # not until it is known what the album is
        page.get_by_label("Album", exact=True).fill("My Own Name")
        expect(page.get_by_role("radio", name=re.compile(r"^The Real Name"))).to_be_visible(timeout=20_000)
        expect(page.get_by_label("Album", exact=True)).to_have_value("My Own Name")  # the albums are offered, what was typed stays
        page.get_by_role("radio", name=re.compile(r"^The Real Name")).check()
        expect(page.get_by_label("Album", exact=True)).to_have_value("The Real Name")  # until the person chooses one


def test_a_detail_that_cannot_be_used_is_explained_in_words(page, live, world) -> None:
    page.allowed_statuses = {404, 409, 422}
    add_library(page, live)
    open_discs(page)
    expect(page.get_by_label("Title of song 1")).to_be_visible()
    page.get_by_label("Year").fill("99999")
    expect(page.locator(".callout[role=alert]", has_text="cannot be used")).to_be_visible()
    expect(page.get_by_role("button", name="Rip", exact=True)).to_be_disabled()
    page.get_by_label("Year").fill("1999")
    expect(page.get_by_role("button", name="Rip 3 songs")).to_be_enabled()


# ---------------------------------------------------------------------------------------- how to rip


def test_mp3_with_a_quality_can_be_chosen_and_the_paths_follow(page, live, world, monkeypatch) -> None:
    add_library(page, live)
    with musicbrainz(live, monkeypatch, *albums("The Real Name")):
        open_discs(page)
        page.get_by_label("Format").select_option("mp3")
        expect(page.get_by_label("MP3 quality")).to_have_value("256")
        page.get_by_label("MP3 quality").select_option("128")
        expect(page.locator(".sync-table .where").first).to_have_text("Real Artist/The Real Name/01 - One.mp3")
        page.get_by_role("button", name="Rip 3 songs").click()
        expect(page.locator(".callout", has_text="Ripped 3 songs")).to_be_visible(timeout=60_000)
    from mutagen.mp3 import MP3

    assert abs(MP3(album_dir(live) / "01 - One.mp3").info.bitrate / 1000 - 128) <= 1


def test_the_layout_can_be_changed_and_a_broken_one_is_explained(page, live, world, monkeypatch) -> None:
    page.allowed_statuses = {404, 409, 422}
    add_library(page, live)
    with musicbrainz(live, monkeypatch, *albums("The Real Name")):
        open_discs(page)
        page.get_by_label("Layout pattern").fill("{album}/{title}.{ext}")
        expect(page.locator(".sync-table .where").first).to_have_text("The Real Name/One.flac")
        page.get_by_label("Layout pattern").fill("{nonsense}/{title}.{ext}")
        expect(page.locator(".callout[role=alert]").first).to_be_visible()
        expect(page.get_by_role("button", name="Rip", exact=True)).to_be_disabled()
        page.get_by_label("Layout pattern").fill("")
        expect(page.get_by_text("Type a layout")).to_be_visible()


def test_the_settings_card_sets_what_the_screen_starts_from(page, live, world) -> None:
    add_library(page, live)
    page.evaluate("location.hash = '#/settings'")
    card = page.locator(".card-panel", has_text="How audio CDs are ripped")
    card.get_by_label("Format").select_option("mp3")
    card.get_by_label("MP3 quality").select_option("320")
    card.get_by_label("Accuracy").select_option("fast")
    expect(page.locator(".toast", has_text="Saved").first).to_be_visible()
    assert (live.ctx.config.disc.rip_format, live.ctx.config.disc.rip_mp3_quality, live.ctx.config.disc.rip_accuracy) == ("mp3", "320", "fast")

    open_discs(page)
    expect(page.get_by_label("Format")).to_have_value("mp3")
    expect(page.get_by_label("MP3 quality")).to_have_value("320")
    expect(page.get_by_label("Accuracy")).to_have_value("fast")


def test_diagnostics_list_the_drives(page, live, world) -> None:
    page.evaluate("location.hash = '#/settings'")
    page.get_by_role("button", name="Run diagnostics").click()
    row = page.locator(".diag", has_text="CD and DVD drives")
    expect(row).to_contain_text("E: Stand-in CD/DVD drive (audio CD)")


# ---------------------------------------------------------------------------------------- the history


def test_the_history_lists_each_rip_with_what_happened_to_every_song(page, live, world, recycle, monkeypatch) -> None:
    add_library(page, live)
    with musicbrainz(live, monkeypatch, *albums("The Real Name")):
        open_discs(page)
        expect(page.get_by_role("tab", name="History")).to_be_visible()
        page.get_by_role("tab", name="History").click()
        expect(page.get_by_role("heading", name="Nothing ripped or burned yet")).to_be_visible()
        page.get_by_role("tab", name="Rip a CD").click()
        start_button(page).click()
        expect(page.locator(".callout", has_text="Ripped 3 songs")).to_be_visible(timeout=60_000)

    page.get_by_role("tab", name="History").click()
    row = page.locator(".rip-row").first
    expect(row).to_contain_text("The Real Name")
    expect(row).to_contain_text("FLAC · 3 of 3 songs")
    expect(row).to_contain_text("Done")
    row.get_by_role("button", name="Details").click()
    expect(row.locator(".rip-tracks li")).to_have_count(3)
    expect(row.locator(".rip-tracks li").first).to_contain_text("Done")

    row.get_by_role("button", name="Take back").click()
    page.get_by_role("dialog").get_by_role("button", name="Move 3 songs to the Recycle Bin").click()
    expect(page.locator(".rip-row").first).to_contain_text("Taken back", timeout=30_000)
    assert len(recycle) == 3 and not list(live.library.rglob("*.flac"))
    expect(page.locator(".rip-row").first.get_by_role("button", name="Take back")).to_have_count(0)


# ---------------------------------------------------------------------------------------- the keyboard


def test_the_whole_rip_can_be_done_from_the_keyboard(page, live, world, monkeypatch) -> None:
    add_library(page, live)
    with musicbrainz(live, monkeypatch, *albums("The Real Name", "The Real Name (Deluxe)")):
        open_discs(page)
        first = page.get_by_role("radio", name=re.compile(r"^The Real Name\b(?!.*Deluxe)"))
        expect(first).to_be_checked()
        first.focus()
        page.keyboard.press("ArrowDown")
        expect(page.get_by_label("Album", exact=True)).to_have_value("The Real Name (Deluxe)")
        page.keyboard.press("ArrowUp")
        expect(page.get_by_label("Album", exact=True)).to_have_value("The Real Name")

        page.get_by_label("Rip song 3").focus()
        page.keyboard.press("Space")
        expect(page.get_by_label("Rip song 3")).not_to_be_checked()
        page.keyboard.press("Space")
        expect(page.get_by_label("Rip song 3")).to_be_checked()

        button = start_button(page)
        expect(button).to_be_enabled()
        button.focus()
        page.keyboard.press("Enter")
        expect(page.locator("#rip-title")).to_be_focused()  # the keyboard follows the page to what is happening
        expect(page.locator(".callout", has_text="Ripped 3 songs")).to_be_visible(timeout=60_000)
    page.get_by_role("button", name="Rip another disc").focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", name="Put an audio CD in the drive")).to_be_visible()  # (the clean rip ejected the disc)
    expect(page.locator("main")).to_be_focused()  # the keyboard is not left on a button that has gone


# ---------------------------------------------------------------------------------------- accessibility


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_every_state_of_the_discs_screen_passes_an_automated_accessibility_check(page, live, tmp_path, world, recycle, monkeypatch, theme) -> None:
    """axe (see test_accessibility.py) over the Discs screen as a person meets it: no drive, no disc, a data disc, no way to look the album
    up, the albums to choose between, a song that is already there, a rip under way, one that was stopped, one with doubtful parts, the
    history with its details, and the confirmation before a rip is taken back."""
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

    other = tmp_path / "other-world"
    other.mkdir()
    ejected = (world / "E" / "disc.json").read_text()

    # no helper at all
    monkeypatch.delenv("MTK_DISC_HELPER")
    disc_helper.forget_listing()
    open_discs(page)
    expect(page.get_by_role("heading", name="Discs are not available here")).to_be_visible()
    look("the discs are not available")

    # a helper, but no drive
    monkeypatch.setenv("MTK_DISC_HELPER", json.dumps([sys.executable, str(FAKE), str(other)]))
    disc_helper.forget_listing()
    page.reload()
    page.wait_for_selector(".sidebar")
    expect(page.get_by_role("heading", name="No CD or DVD drive found")).to_be_visible()
    look("no drive")

    # a drive with nothing in it, and an empty history
    monkeypatch.setenv("MTK_DISC_HELPER", json.dumps([sys.executable, str(FAKE), str(world)]))
    disc_helper.forget_listing()
    (world / "E" / "disc.json").unlink()
    page.get_by_role("button", name="Look again").click()
    expect(page.get_by_role("heading", name="Put an audio CD in the drive")).to_be_visible()
    look("an empty drive")
    page.get_by_role("tab", name="History").click()
    expect(page.get_by_role("heading", name="Nothing ripped or burned yet")).to_be_visible()
    look("an empty history")
    page.get_by_role("tab", name="Rip a CD").click()

    # a data disc
    (world / "E" / "disc.json").write_text(json.dumps({"kind": "data", "label": "BACKUP", "seed": 0, "tracks": [{"number": 1, "audio": False, "sectors": 500}]}))
    disc_helper.forget_listing()
    page.get_by_role("button", name="Read the disc again").click()
    expect(page.get_by_role("heading", name="This is a data disc, not a music CD")).to_be_visible()
    look("a data disc")

    # music, and no contact to ask MusicBrainz with
    (world / "E" / "disc.json").write_text(ejected)
    disc_helper.forget_listing()
    page.get_by_role("button", name="Read the disc again").click()
    expect(page.get_by_text("add your email address")).to_be_visible()
    look("music, with no way to look the album up")

    # music, and albums to choose between
    with musicbrainz(live, monkeypatch, *albums("The Real Name", "The Real Name (Deluxe)")):
        page.get_by_role("button", name="Read the disc again").click()
        page.evaluate("location.hash = '#/settings'")  # away and back: the page asks again, now that there is a contact
        page.evaluate("location.hash = '#/discs'")
        expect(page.get_by_role("radio")).to_have_count(3)
        expect(page.get_by_role("button", name="Rip 3 songs")).to_be_enabled()
        look("music, with albums to choose between")

        page.get_by_label("The songs have different artists").check()
        expect(page.get_by_label("Artist of song 1")).to_be_visible()
        look("a compilation")
        page.get_by_label("The songs have different artists").uncheck()
        page.get_by_label("Format").select_option("mp3")
        look("MP3 chosen")
        page.get_by_label("Format").select_option("flac")

        album_dir(live).mkdir(parents=True)
        (album_dir(live) / "02 - Two.flac").write_bytes(b"mine")
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(page.locator(".stat", has_text="already there")).to_be_visible()
        look("a song that is already there")
        (album_dir(live) / "02 - Two.flac").unlink()
        page.evaluate("window.dispatchEvent(new Event('focus'))")  # back from File Explorer: the songs are looked at again
        expect(page.get_by_role("button", name="Rip 3 songs")).to_be_enabled()

        # a rip under way
        set_control(world, slow=0.25)
        page.get_by_role("button", name="Rip 3 songs").click()
        expect(page.locator(".rip-tracks li.ripping")).to_be_visible(timeout=30_000)
        look("a rip under way", still=False)
        page.locator(".job-banner").get_by_role("button", name="Cancel").click()
        expect(page.locator(".callout", has_text="Stopped")).to_be_visible(timeout=60_000)
        look("a rip that was stopped")
        page.get_by_role("button", name="Rip another disc").click()

        # a rip with a doubtful part, and then the history
        set_control(world, flaky=[{"from": 70, "to": 70, "times": 100, "varies": True}])
        expect(page.get_by_role("button", name="Rip 3 songs")).to_be_enabled(timeout=30_000)
        page.get_by_label("Album", exact=True).fill("The Real Name Again")
        expect(page.get_by_role("button", name="Rip 3 songs")).to_be_enabled()
        page.get_by_role("button", name="Rip 3 songs").click()
        expect(page.locator(".callout", has_text="Ripped 3 songs")).to_be_visible(timeout=90_000)
        look("a rip with a doubtful part")

    page.get_by_role("tab", name="History").click()
    expect(page.locator(".rip-row")).to_have_count(2)
    look("the history")
    page.locator(".rip-row").first.get_by_role("button", name="Details").click()
    expect(page.locator(".rip-row").first.locator(".rip-tracks li")).to_have_count(3)
    look("the history, with the details of a rip")
    page.locator(".rip-row").first.get_by_role("button", name="Take back").click()
    expect(page.get_by_role("dialog", name="Take back this rip?")).to_be_visible()
    look("the question before a rip is taken back", still=False, open_dialog=True)
    page.evaluate(accessibility.CLEAR)

    assert not problems, f"{len(problems)} accessibility problems in the {theme} theme:\n" + "\n".join(problems)
