"""Convert songs in the browser: choose, preview, convert for real, see the history, stop one, take one back."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import mutagen
import numpy as np
import soundfile as sf
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError
from playwright.sync_api import expect

from musictoolkit.convert import engine, store
from musictoolkit.media import art
from tests.e2e.conftest import add_library, api

expect.set_options(timeout=20_000)

START = re.compile(r"^Convert \d+ songs?$")


def open_convert(page) -> None:
    """Through the tools page and its card, the way a person gets there."""
    page.evaluate("location.hash = '#/tools'")
    page.get_by_role("link", name=re.compile("^Convert songs")).click()
    expect(page.get_by_role("heading", name="Convert songs", level=1)).to_be_visible()
    expect(page.locator(".organize-form")).to_be_visible()


def choose_the_whole_library(page) -> None:
    page.get_by_label("Kind of music to add").select_option(label="Whole library")
    page.get_by_role("button", name="Add", exact=True).click()
    expect(page.locator(".source-chip", has_text="Whole library")).to_be_visible()


def preview(page) -> None:
    page.get_by_role("button", name="Preview", exact=True).click()
    expect(page.locator(".stat-row")).to_be_visible()


def song_files(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())


def source_song(live, title: str) -> Path:
    return next(live.library.rglob(f"*{title}*.mp3"))


def convert_the_library(page, out: Path, *, format_label: str = "Opus", quality: str | None = "96") -> None:
    """The whole way through the form for every song in the library: choose, preview, convert, and wait for the message that says it is done."""
    open_convert(page)
    choose_the_whole_library(page)
    page.get_by_label("Format", exact=True).select_option(label=format_label)
    if quality:
        page.get_by_label("Quality", exact=True).select_option(value=quality)
    page.locator("#convert-folder").fill(str(out))
    preview(page)
    page.get_by_role("button", name=START).click()
    expect(page.locator(".toast", has_text="Converted 9 songs")).to_be_visible(timeout=120_000)


def add_a_song_mp3_can_copy(live) -> Path:
    """A 44.1 kHz stereo 256 kbps MP3 in the live library (before it is scanned), in the genre Copies: what MP3 is asked for at its default quality,
    so converting it to MP3 only copies it (the other songs are at 22.05 kHz, which MP3 as asked for here does not take)."""
    path = live.library / "Extra" / "Copies" / "01 - Copy Me.mp3"
    path.parent.mkdir(parents=True)
    rate = 44100
    tone = (0.3 * np.sin(2 * np.pi * 440 * np.arange(rate * 4) / rate)).astype("float32")
    sf.write(str(path), np.column_stack([tone, tone]), rate, format="MP3", subtype="MPEG_LAYER_III", bitrate_mode="CONSTANT", compression_level=0.19)
    try:
        tags = EasyID3(str(path))
    except ID3NoHeaderError:
        tags = EasyID3()
    tags.update({"title": "Copy Me", "artist": "Aurora Vale", "albumartist": "Aurora Vale", "album": "Copies", "tracknumber": "1", "genre": "Copies"})
    tags.save(str(path))
    return path


def test_the_whole_way_from_the_tools_page_to_taking_the_conversion_back(page, live, tmp_path):
    add_library(page, live)
    out = tmp_path / "converted"
    open_convert(page)

    expect(page.get_by_role("button", name="Preview", exact=True)).to_be_disabled()  # nothing is chosen yet
    expect(page.get_by_text("Nothing chosen yet. Add the music to convert.")).to_be_visible()
    choose_the_whole_library(page)
    page.get_by_label("Format", exact=True).select_option(label="Opus")
    page.get_by_label("Quality", exact=True).select_option(value="96")
    page.locator("#convert-folder").fill(str(out))
    preview(page)

    expect(page.locator(".stat", has_text="will be converted")).to_contain_text("9")
    expect(page.locator(".convert-table .move-row")).to_have_count(9)
    glacier = page.locator(".convert-table .move-row", has_text="Glacier")
    expect(glacier).to_contain_text("Aurora Vale/Northern Lights/01 - Glacier.opus")
    expect(glacier).to_contain_text("Resampled from 22.05 kHz to 48 kHz.")  # what happens to a song is said under it, not left to be found out
    assert not out.exists(), "looking at what would happen wrote something"

    page.get_by_role("button", name=START).click()
    expect(page.locator(".toast", has_text="Converted 9 songs")).to_be_visible(timeout=120_000)
    assert len(song_files(out)) == 9
    made = out / "Aurora Vale" / "Northern Lights" / "01 - Glacier.opus"
    info = sf.info(str(made))
    assert (info.format, info.subtype) == ("OGG", "OPUS")  # a real Opus file, not a renamed MP3
    tags = mutagen.File(made)
    assert tags["title"] == ["Glacier"] and tags["artist"] == ["Aurora Vale"] and tags["album"] == ["Northern Lights"] and tags["tracknumber"] == ["1"]
    assert art.embedded_art(made) == art.embedded_art(source_song(live, "Glacier")) is not None  # the cover came along
    assert art.embedded_art(out / "Mara Quinn" / "Static Hearts" / "01 - Wires.opus") is None  # and a song with none got none
    assert len(list(live.library.rglob("*.mp3"))) == 9 and not list(live.library.rglob("*.opus"))  # the library holds what it held
    assert api(page, "/tracks?limit=100")["total"] == 9

    entry = page.locator(".batch-row")
    expect(entry).to_contain_text("9 songs to Opus (about 96 kbps)")
    expect(entry.locator(".chip")).to_have_text("Done")
    entry.get_by_role("button", name="Details").click()
    detail = page.locator(".batch-detail")
    expect(detail).to_contain_text(str(out))
    expect(detail.locator(".convert-items .move-row")).to_have_count(9)
    expect(detail.locator(".convert-items .chip", has_text=re.compile(r"^Made$"))).to_have_count(9)

    page.evaluate("location.hash = '#/tools'")  # the tools page says when the last conversion was
    expect(page.locator(".tool-card", has_text="Convert songs")).to_contain_text("Last conversion just now")

    open_convert(page)
    page.locator(".batch-row").get_by_role("button", name="Undo").click()
    dialog = page.get_by_role("dialog", name="Take back 9 converted songs?")
    expect(dialog).to_contain_text("Your library is not touched.")
    dialog.get_by_role("button", name="Delete the converted files").click()
    expect(page.locator(".toast", has_text="Took back 9 converted songs")).to_be_visible()
    expect(page.locator(".batch-row .chip")).to_have_text("Taken back")
    expect(page.locator(".batch-row").get_by_role("button", name="Undo")).to_have_count(0)
    assert song_files(out) == [] and out.is_dir()  # the songs and the folders they made are gone; the folder the person chose is theirs
    assert len(list(live.library.rglob("*.mp3"))) == 9


def test_the_form_offers_what_the_format_has(page, live):
    open_convert(page)
    quality = page.get_by_label("Quality", exact=True)
    expect(quality.locator("option")).to_have_text(["320 kbps", "256 kbps", "192 kbps", "128 kbps"])
    expect(page.get_by_label("Bit depth")).to_have_count(0)
    expect(page.locator(".format-blurb")).to_contain_text("Plays everywhere")

    page.get_by_label("Format", exact=True).select_option(label="FLAC (lossless)")
    expect(page.get_by_label("Quality", exact=True)).to_have_count(0)  # lossless has no quality to choose
    expect(page.get_by_label("Bit depth")).to_be_visible()
    expect(page.locator(".format-blurb")).to_contain_text("Exactly what the song is")

    page.get_by_label("Format", exact=True).select_option(label="Ogg Vorbis")
    expect(page.get_by_label("Bit depth")).to_have_count(0)
    expect(page.get_by_label("Quality", exact=True).locator("option")).to_have_count(6)
    expect(page.get_by_label("Quality", exact=True)).to_have_value("160")  # Ogg's own default: FLAC had no quality to carry over
    page.get_by_label("Quality", exact=True).select_option(value="96")
    page.get_by_label("Format", exact=True).select_option(label="Opus")
    expect(page.get_by_label("Quality", exact=True)).to_have_value("96")  # a quality both formats have is kept
    page.get_by_label("Format", exact=True).select_option(label="MP3")
    expect(page.get_by_label("Quality", exact=True)).to_have_value("256")  # one MP3 does not have gives MP3's default


def test_nothing_can_be_previewed_until_something_is_chosen_and_the_layout_makes_sense(page, live):
    open_convert(page)
    button = page.get_by_role("button", name="Preview", exact=True)
    expect(button).to_be_disabled()
    expect(page.get_by_text("Choose what to convert first.")).to_be_visible()
    choose_the_whole_library(page)
    expect(button).to_be_enabled()
    page.get_by_role("button", name="Remove Whole library").click()
    expect(button).to_be_disabled()

    choose_the_whole_library(page)
    layout = page.get_by_label("Folder and file name, built from these fields")
    layout.fill("{nonsense}/{title}.{ext}")
    expect(page.locator(".example-box.bad")).to_be_visible()  # said before anything is previewed
    expect(button).to_be_disabled()
    layout.fill("{artist} - {title}.{ext}")
    expect(page.locator(".example-box code").first).to_be_visible()
    expect(button).to_be_enabled()
    page.get_by_role("button", name="{album}", exact=True).click()  # the field chips add to the layout
    expect(layout).to_have_value("{artist} - {title}.{ext}{album}")


def test_a_change_after_the_preview_asks_for_a_new_preview_and_never_starts_what_was_not_shown(page, live, tmp_path):
    add_library(page, live)
    out = tmp_path / "converted"
    open_convert(page)
    choose_the_whole_library(page)
    page.locator("#convert-folder").fill(str(out))
    preview(page)
    start = page.get_by_role("button", name=START)
    expect(start).to_be_visible()

    page.get_by_label("Quality", exact=True).select_option(value="128")
    expect(page.get_by_text("You changed something after previewing")).to_be_visible()
    expect(start).to_have_count(0)
    expect(page.locator(".stat-row")).to_have_count(0)
    preview(page)
    expect(start).to_be_visible()
    expect(page.get_by_text("You changed something after previewing")).to_have_count(0)
    assert not out.exists()


def test_a_folder_inside_the_library_is_refused_in_words_and_nothing_can_be_started(page, live):
    add_library(page, live)
    inside = live.library / "converted"
    open_convert(page)
    choose_the_whole_library(page)
    page.locator("#convert-folder").fill(str(inside))
    preview(page)
    problem = page.locator(".callout.warn[role=alert]")
    expect(problem).to_contain_text("Choose a folder outside your library folders")
    expect(page.get_by_role("button", name=START)).to_have_count(0)
    assert not inside.exists()


def test_what_was_chosen_is_there_again_next_time_but_not_what_to_convert(page, live, tmp_path):
    add_library(page, live)
    out = tmp_path / "flacs"
    open_convert(page)
    choose_the_whole_library(page)
    page.get_by_label("Format", exact=True).select_option(label="FLAC (lossless)")
    page.get_by_label("Bit depth").select_option(value="keep")
    page.get_by_label("Sample rate").select_option(label="At most 44.1 kHz (CD)")
    page.get_by_label("Songs already in this format").select_option(label="Leave them out")
    page.locator("#convert-folder").fill(f"  {out}  ")  # (spaces typed or pasted around it are not part of the folder, nor of what is remembered)
    page.get_by_label("Folder and file name, built from these fields").fill("{artist} - {title}.{ext}")
    page.get_by_label("Carry each song’s cover picture into its copy").uncheck()
    preview(page)
    page.get_by_role("button", name=START).click()
    expect(page.locator(".toast", has_text="Converted 9 songs")).to_be_visible(timeout=120_000)
    assert song_files(out) == sorted(f"{artist} - {title}.flac" for artist, title in [
        ("Aurora Vale", "Glacier"), ("Aurora Vale", "Polar Night"), ("Aurora Vale", "Drift"), ("Aurora Vale", "Salt and Stone"),
        ("The Fernwoods", "Gravel Dust"), ("The Fernwoods", "Porch Light"), ("Mara Quinn", "Wires"), ("Mara Quinn", "Neon Rain"), ("Mara Quinn", "Signal Fade"),
    ])
    assert all(art.embedded_art(out / name) is None for name in song_files(out))  # the cover was left out, as asked

    page.evaluate("location.hash = '#/songs'")
    open_convert(page)
    expect(page.get_by_label("Format", exact=True)).to_have_value("flac")
    expect(page.get_by_label("Bit depth")).to_have_value("keep")
    expect(page.get_by_label("Sample rate")).to_have_value("44100")
    expect(page.get_by_label("Songs already in this format")).to_have_value("skip")
    expect(page.locator("#convert-folder")).to_have_value(str(out))
    expect(page.get_by_label("Folder and file name, built from these fields")).to_have_value("{artist} - {title}.{ext}")
    expect(page.get_by_label("Carry each song’s cover picture into its copy")).not_to_be_checked()
    expect(page.get_by_text("Nothing chosen yet. Add the music to convert.")).to_be_visible()  # a conversion of everything is not started by habit


def test_a_conversion_is_stopped_from_the_banner_and_what_was_finished_stays_and_can_be_taken_back(page, live, tmp_path, monkeypatch):
    add_library(page, live)
    out = tmp_path / "converted"
    real = engine.convert_file
    calls: list[str] = []

    def slow(source, target, info, spec, *, check, on_progress):
        calls.append(str(source))
        if len(calls) >= 3:  # the third song and every one after it wait, to be stopped
            deadline = time.time() + 60
            while time.time() < deadline:
                check()
                time.sleep(0.05)
        return real(source, target, info, spec, check=check, on_progress=on_progress)

    monkeypatch.setattr(engine, "convert_file", slow)
    open_convert(page)
    choose_the_whole_library(page)
    page.get_by_label("Format", exact=True).select_option(label="Opus")  # (MP3 songs would only be copied to MP3: nothing for the slowed encoder to do)
    page.locator("#convert-folder").fill(str(out))
    preview(page)
    page.get_by_role("button", name=START).click()
    banner = page.locator(".job-banner")
    expect(banner).to_contain_text("Converting 9 songs to Opus")
    expect(banner).to_contain_text("Converting song 3 of 9")
    expect(page.get_by_role("button", name="Preview", exact=True)).to_be_disabled()  # nothing else is started meanwhile
    banner.get_by_role("button", name="Cancel").click()

    expect(page.locator(".toast", has_text="Stopped. The songs already converted stay in the folder")).to_be_visible()
    entry = page.locator(".batch-row")
    expect(entry).to_contain_text("9 songs to Opus")
    expect(entry.locator(".chip")).to_have_text("Stopped")
    assert len(song_files(out)) == 2, song_files(out)
    assert not list(out.rglob("*.part")), "a half-written file was left behind"

    entry.get_by_role("button", name="Details").click()
    expect(page.locator(".batch-detail .convert-items .chip", has_text=re.compile(r"^Made$"))).to_have_count(2)
    page.get_by_label("Which songs to show").select_option(label="Only the songs that were not made")
    expect(page.locator(".batch-detail .convert-items .move-row")).to_have_count(7)  # the rest were never started, and say so
    expect(page.locator(".batch-detail .convert-items .move-row").first).to_contain_text("Stopped")

    entry.get_by_role("button", name="Undo").click()
    page.get_by_role("dialog").get_by_role("button", name="Delete the converted files").click()
    expect(page.locator(".toast", has_text="Took back 2 converted songs")).to_be_visible()
    assert song_files(out) == []


def test_the_songs_that_were_not_made_are_listed_first_with_the_reason(page, live, tmp_path):
    out = tmp_path / "converted"
    with live.ctx.db() as conn:
        conversion_id, ids = store.create_conversion(
            conn, batch_id="seeded", title="3 songs to MP3 (256 kbps)", fmt="mp3", quality="256", destination=str(out),
            scheme="{artist} - {title}.{ext}", options={"max_rate": 0, "depth": "16", "existing": "copy", "cover": True}, left_out=1,
            items=[{"track_id": None, "title": t, "source": f"/music/{t}.flac", "target": str(out / f"{t}.mp3"), "action": "convert"} for t in ("Made one", "Broken one", "Waiting one")],
        )
        store.set_item(conn, ids[0], status="done", size=1000, mtime=1.0)
        store.set_item(conn, ids[1], status="failed", message="The file could not be written (No space left on device).")
        store.set_item(conn, ids[2], status="skipped", message="Stopped before this song was finished.")
        store.finish_conversion(conn, conversion_id, "partial", "1 song could not be converted.")
    open_convert(page)

    entry = page.locator(".batch-row")
    expect(entry).to_contain_text("3 songs to MP3 (256 kbps)")
    expect(entry.locator(".chip")).to_have_text("Some failed")
    entry.get_by_role("button", name="Details").click()
    detail = page.locator(".batch-detail")
    expect(detail).to_contain_text("1 song could not be converted.")
    rows = detail.locator(".convert-items .move-row")
    expect(rows).to_have_count(2)  # a conversion with something wrong opens on what is wrong
    expect(rows.nth(0)).to_contain_text("Broken one")
    expect(rows.nth(0)).to_contain_text("The file could not be written (No space left on device).")
    expect(rows.nth(0).locator(".chip")).to_have_text("Failed")
    expect(rows.nth(1)).to_contain_text("Waiting one")
    expect(rows.nth(1).locator(".chip")).to_have_text("Not made")
    page.get_by_label("Which songs to show").select_option(label="All 3 songs")
    expect(rows).to_have_count(3)
    expect(rows.nth(0).locator(".chip")).to_have_text("Made")


def test_a_conversion_started_by_a_closed_app_is_reported_as_interrupted_not_running(page, live, tmp_path):
    out = tmp_path / "converted"
    with live.ctx.db() as conn:
        _, ids = store.create_conversion(
            conn, batch_id="cut", title="2 songs to MP3 (256 kbps)", fmt="mp3", quality="256", destination=str(out), scheme="{title}.{ext}",
            options={}, left_out=0, items=[{"track_id": None, "title": t, "source": f"/m/{t}.flac", "target": str(out / f"{t}.mp3"), "action": "convert"} for t in ("A", "B")],
        )
        store.set_item(conn, ids[0], status="done", size=10, mtime=1.0)
        store.recover(conn)  # what the app does when it starts again
    open_convert(page)
    entry = page.locator(".batch-row")
    expect(entry.locator(".chip")).to_have_text("Interrupted")
    entry.get_by_role("button", name="Details").click()
    expect(page.locator(".batch-detail")).to_contain_text("The app was closed before the conversion was finished.")
    expect(page.locator(".batch-detail .convert-items .move-row")).to_have_count(1)  # opens on the one that was not finished
    expect(page.locator(".batch-detail .convert-items .move-row")).to_contain_text("The app was closed before this song was finished.")


def test_only_one_conversion_runs_at_a_time(page, live, tmp_path, monkeypatch):
    add_library(page, live)
    release = threading.Event()
    real = engine.convert_file

    def held(source, target, info, spec, *, check, on_progress):
        while not release.is_set():
            check()
            time.sleep(0.05)
        return real(source, target, info, spec, check=check, on_progress=on_progress)

    monkeypatch.setattr(engine, "convert_file", held)
    open_convert(page)
    choose_the_whole_library(page)
    page.get_by_label("Format", exact=True).select_option(label="Opus")
    page.locator("#convert-folder").fill(str(tmp_path / "converted"))
    preview(page)
    page.get_by_role("button", name=START).click()
    expect(page.locator(".job-banner")).to_be_visible()
    for control in (page.get_by_role("button", name="Preview", exact=True), page.get_by_role("button", name="Browse…")):
        expect(control).to_be_disabled()
    expect(page.locator("#convert-folder")).to_be_disabled()
    for control in page.locator(".organize-form select, .organize-form input, .organize-form button").all():
        expect(control).to_be_disabled()  # (nothing in the form can be changed under a conversion that is running)
    release.set()
    expect(page.locator(".toast", has_text="Converted 9 songs")).to_be_visible(timeout=120_000)
    expect(page.get_by_role("button", name="Preview", exact=True)).to_be_enabled()


def test_the_form_is_held_by_the_work_of_converting_even_while_it_waits_for_its_turn_and_by_no_other_work(page, live, tmp_path):
    add_library(page, live)
    release = threading.Event()

    def hold(handle) -> None:
        while not release.is_set():
            handle.check()
            time.sleep(0.05)

    try:
        live.ctx.jobs.submit("hold-other", "Holding other work", hold, lane="hold-other")  # (a kind of work that is not Convert's)
        open_convert(page)
        choose_the_whole_library(page)
        page.locator("#convert-folder").fill(str(tmp_path / "converted"))
        expect(page.get_by_role("button", name="Background tasks")).to_contain_text("Holding other work")  # (the page has been told of it)
        button = page.get_by_role("button", name="Preview", exact=True)
        expect(button).to_be_enabled()
        expect(page.locator("#convert-folder")).to_be_enabled()

        live.ctx.jobs.submit("hold-analysis", "Holding the analysis", hold, lane="analyze")  # (the lane the preview is made in: it has to wait)
        button.click()
        expect(page.locator(".job-banner")).to_contain_text("Looking at the songs to convert")
        for control in page.locator(".organize-form select, .organize-form input, .organize-form button").all():
            expect(control).to_be_disabled()  # (waiting for a turn holds the form as running does)
        release.set()
        expect(page.locator(".stat-row")).to_be_visible()
        expect(button).to_be_enabled()
    finally:
        release.set()


def test_the_same_choice_is_not_added_twice_and_two_ratings_are_two_choices(page, live):
    open_convert(page)
    add = page.get_by_role("button", name="Add", exact=True)
    page.get_by_label("Kind of music to add").select_option(label="Songs rated…")
    page.get_by_label("Minimum rating").select_option("4")
    add.click()
    expect(page.locator(".source-chip", has_text="Rated 4+ stars")).to_be_visible()
    expect(add).to_be_disabled()  # that very choice has been made
    page.get_by_label("Minimum rating").select_option("2")
    expect(add).to_be_enabled()
    add.click()
    expect(page.locator(".source-chip")).to_have_count(2)


def test_preview_waits_for_a_folder_and_a_layout(page, live, tmp_path):
    open_convert(page)
    choose_the_whole_library(page)
    button = page.get_by_role("button", name="Preview", exact=True)
    folder, layout = page.locator("#convert-folder"), page.get_by_label("Folder and file name, built from these fields")
    expect(button).to_be_enabled()
    folder.fill("")
    expect(button).to_be_disabled()
    folder.fill(str(tmp_path / "somewhere"))
    expect(button).to_be_enabled()
    layout.fill("")
    expect(button).to_be_disabled()
    layout.fill("{title}.{ext}")
    expect(button).to_be_enabled()


def test_a_layout_preset_fills_in_the_layout(page, live):
    open_convert(page)
    presets = page.get_by_label("Layout preset")
    chosen = presets.locator("option").nth(4).get_attribute("value")  # (the first option is "Custom")
    assert chosen == "{album_artist}/{album}/{title}.{ext}"
    presets.select_option(index=4)
    expect(page.get_by_label("Folder and file name, built from these fields")).to_have_value(chosen)


def test_the_folder_can_be_picked_in_the_dialog_a_browser_has_instead_of_the_desktop_picker(page, live, tmp_path):
    open_convert(page)
    page.get_by_role("button", name="Browse…").click()
    dialog = page.get_by_role("dialog", name="Choose the folder for the converted songs")
    dialog.get_by_label("Folder path").fill(str(tmp_path / "picked"))
    dialog.get_by_role("button", name="Choose", exact=True).click()
    expect(page.locator("#convert-folder")).to_have_value(str(tmp_path / "picked"))


def test_a_remembered_lossy_quality_comes_back(page, live, tmp_path):
    add_library(page, live)
    convert_the_library(page, tmp_path / "opus", format_label="Opus", quality="64")
    page.evaluate("location.hash = '#/songs'")
    open_convert(page)
    expect(page.get_by_label("Format", exact=True)).to_have_value("opus")
    expect(page.get_by_label("Quality", exact=True)).to_have_value("64")


def test_closing_the_question_before_taking_back_deletes_nothing(page, live, tmp_path):
    add_library(page, live)
    out = tmp_path / "converted"
    convert_the_library(page, out)
    made = song_files(out)
    for opener in (page.locator(".toast").get_by_role("button", name="Undo"), page.locator(".batch-row").get_by_role("button", name="Undo")):
        opener.click()  # (from the message that says it is done, and from the history)
        dialog = page.get_by_role("dialog", name="Take back 9 converted songs?")
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Cancel").click()
        expect(dialog).to_have_count(0)
    page.wait_for_timeout(1500)  # (a request that was wrongly sent would have been answered by now)
    assert song_files(out) == made
    assert not any(job["kind"] == "convert-undo" for job in api(page, "/jobs")["jobs"])
    expect(page.locator(".batch-row .chip")).to_have_text("Done")


def test_a_converted_file_that_was_changed_since_is_left_alone_when_taking_back(page, live, tmp_path):
    add_library(page, live)
    out = tmp_path / "converted"
    convert_the_library(page, out)
    mine = out / "Aurora Vale" / "Northern Lights" / "01 - Glacier.opus"
    mine.write_bytes(mine.read_bytes() + b"the person's own edit")
    page.locator(".batch-row").get_by_role("button", name="Undo").click()
    page.get_by_role("dialog").get_by_role("button", name="Delete the converted files").click()
    taken = page.locator(".toast", has_text="Took back 8 converted songs")
    expect(taken).to_contain_text("1 file had been changed since and was left alone")
    assert song_files(out) == ["Aurora Vale/Northern Lights/01 - Glacier.opus"]


def test_when_the_songs_changed_after_the_preview_the_result_says_so(page, live, tmp_path):
    add_library(page, live)
    out = tmp_path / "converted"
    open_convert(page)
    choose_the_whole_library(page)
    page.get_by_label("Format", exact=True).select_option(label="Opus")
    page.locator("#convert-folder").fill(str(out))
    preview(page)
    next(live.library.rglob("*Gravel Dust*.mp3")).unlink()  # a song's file goes while the person is looking at the preview
    page.get_by_role("button", name=START).click()
    done = page.locator(".toast", has_text="Converted 8 songs")
    expect(done).to_contain_text("The preview showed 9 songs: files changed in between.")
    assert len(song_files(out)) == 8


def test_a_song_that_fails_is_said_and_listed_and_the_others_are_made(page, live, tmp_path, monkeypatch):
    real = engine.convert_file

    def failing(source, target, info, spec, **kwargs):
        if "Wires" in str(source):
            raise engine.ConvertError("The song could not be converted (boom).")
        return real(source, target, info, spec, **kwargs)

    monkeypatch.setattr(engine, "convert_file", failing)
    add_library(page, live)
    out = tmp_path / "converted"
    open_convert(page)
    choose_the_whole_library(page)
    page.get_by_label("Format", exact=True).select_option(label="Opus")
    page.locator("#convert-folder").fill(str(out))
    preview(page)
    page.get_by_role("button", name=START).click()
    done = page.locator(".toast", has_text="Converted 8 songs")
    expect(done).to_contain_text("1 song could not be converted: the history says why.")
    entry = page.locator(".batch-row")
    expect(entry.locator(".chip")).to_have_text("Some failed")
    entry.get_by_role("button", name="Details").click()
    rows = page.locator(".batch-detail .convert-items .move-row")
    expect(rows).to_have_count(1)  # (it opens on what went wrong)
    expect(rows.first).to_contain_text("Wires")
    expect(rows.first).to_contain_text("The song could not be converted (boom).")
    expect(rows.first.locator(".chip")).to_have_text("Failed")
    assert len(song_files(out)) == 8


def test_a_conversion_that_made_nothing_has_nothing_to_take_back(page, live, tmp_path):
    out = tmp_path / "converted"
    with live.ctx.db() as conn:
        conversion_id, ids = store.create_conversion(
            conn, batch_id="nothing", title="1 song to MP3 (256 kbps)", fmt="mp3", quality="256", destination=str(out), scheme="{title}.{ext}", options={}, left_out=0,
            items=[{"track_id": None, "title": "A", "source": "/m/A.flac", "target": str(out / "A.mp3"), "action": "convert"}],
        )
        store.set_item(conn, ids[0], status="failed", message="The song could not be converted (boom).")
        store.finish_conversion(conn, conversion_id, "failed", "1 song could not be converted.")
    open_convert(page)
    entry = page.locator(".batch-row")
    expect(entry.locator(".chip")).to_have_text("Failed")
    expect(entry.get_by_role("button", name="Undo")).to_have_count(0)
    entry.get_by_role("button", name="Details").click()
    expect(page.locator(".batch-detail")).to_contain_text("The song could not be converted (boom).")


def test_more_of_the_songs_of_a_long_conversion_are_shown_on_request(page, live, tmp_path):
    out = tmp_path / "converted"
    with live.ctx.db() as conn:
        conversion_id, ids = store.create_conversion(
            conn, batch_id="long", title="120 songs to MP3 (256 kbps)", fmt="mp3", quality="256", destination=str(out), scheme="{title}.{ext}", options={}, left_out=0,
            items=[{"track_id": None, "title": f"Song {n:03d}", "source": f"/m/{n}.flac", "target": str(out / f"{n}.mp3"), "action": "convert"} for n in range(120)],
        )
        for item_id in ids:
            store.set_item(conn, item_id, status="skipped", message="Stopped.")
        store.finish_conversion(conn, conversion_id, "cancelled", "Stopped.")
    open_convert(page)
    page.locator(".batch-row").get_by_role("button", name="Details").click()
    rows = page.locator(".batch-detail .convert-items .move-row")
    expect(rows).to_have_count(100)
    page.get_by_role("button", name=re.compile(r"^Show more \(20 left\)")).click()
    expect(rows).to_have_count(120)
    expect(page.get_by_role("button", name=re.compile(r"^Show more"))).to_have_count(0)


def test_a_song_already_in_the_format_is_copied_and_the_page_says_how_many_of_each(page, live, tmp_path):
    copy = add_a_song_mp3_can_copy(live)
    add_library(page, live)
    out = tmp_path / "converted"
    open_convert(page)
    choose_the_whole_library(page)
    page.locator("#convert-folder").fill(str(out))
    preview(page)  # (MP3 at 256 kbps, the default)
    expect(page.locator(".stat", has_text="will be converted")).to_contain_text("9")
    expect(page.locator(".stat", has_text="copied as it is")).to_contain_text("1")
    expect(page.get_by_role("tab")).to_have_count(2)  # (no tab for what does not happen)
    expect(page.get_by_role("tab", name=re.compile("^Will be converted"))).to_have_attribute("aria-selected", "true")
    page.get_by_role("tab", name=re.compile("^Copied as they are")).click()
    row = page.locator(".convert-table .move-row")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("Copy Me")
    expect(row).to_contain_text("Already in this format: copied as it is.")
    page.get_by_role("button", name="Convert 10 songs").click()  # (the nine that change and the one that is copied)
    expect(page.locator(".toast", has_text="Converted 9 songs and copied 1 song as it was")).to_be_visible(timeout=120_000)
    assert (out / "Aurora Vale" / "Copies" / "01 - Copy Me.mp3").read_bytes() == copy.read_bytes()  # a copy is the very file


def test_a_preview_of_nothing_but_copies_opens_on_them_and_leaving_them_out_leaves_nothing_to_do(page, live, tmp_path):
    add_a_song_mp3_can_copy(live)
    add_library(page, live)
    out = tmp_path / "converted"
    open_convert(page)
    page.get_by_label("Kind of music to add").select_option(label="A genre")
    page.get_by_label("Genre", exact=True).select_option(label="Copies · 1 song")
    page.get_by_role("button", name="Add", exact=True).click()
    page.locator("#convert-folder").fill(str(out))
    preview(page)
    expect(page.get_by_role("tab", name=re.compile("^Copied as they are"))).to_have_attribute("aria-selected", "true")  # (it opens on what there is)
    expect(page.get_by_role("button", name="Convert 1 song")).to_be_visible()

    page.get_by_label("Songs already in this format").select_option(label="Leave them out")
    preview(page)
    expect(page.locator(".callout.warn[role=alert]")).to_contain_text("There is nothing to convert")
    expect(page.locator(".stat", has_text="left out")).to_contain_text("1")
    expect(page.get_by_role("button", name=START)).to_have_count(0)
    assert not out.exists()
