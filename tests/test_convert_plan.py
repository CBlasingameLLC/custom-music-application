"""What a conversion would do, song by song, before anything is written."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from musictoolkit.convert import formats
from musictoolkit.convert import plan as planning
from musictoolkit.convert.engine import Spec
from musictoolkit.convert.source import SourceError, SourceInfo, inspect_source
from tests import convert_audio as audio
from tests.convert_library import Library

SCHEME = "{album_artist}/{album}/{track:02d} - {title}.{ext}"


@pytest.fixture
def library(tmp_path: Path) -> Library:
    return Library(tmp_path / "library")


def make(library: Library, tmp_path: Path, spec: Spec, **kwargs: Any) -> planning.ConvertPlan:
    kwargs.setdefault("ask_windows", lambda paths: pytest.fail(f"Windows was asked about {paths}"))
    return planning.build_plan(library.rows, spec, str(tmp_path / "out"), kwargs.pop("scheme", SCHEME), [str(library.folder)], **kwargs)


def by_title(plan: planning.ConvertPlan) -> dict[str, planning.PlannedSong]:
    return {s.title: s for s in plan.songs}


# ---------------------------------------------------------------------------------------- what becomes of each song


def test_a_mixed_library_is_sorted_into_what_each_song_needs(library: Library, tmp_path: Path) -> None:
    library.add("lossless.flac", audio.music(44100, 30), title="Lossless")
    library.add("hires.flac", audio.music(96000, 10), 96000, subtype="PCM_24", title="Hires")
    library.add("fits.mp3", audio.music(44100, 10), fmt="MP3", subtype="MPEG_LAYER_III", title="Fits")  # 256 kbps constant
    library.add("six.flac", np.zeros((44100, 6), dtype=np.int16) + 5, title="Six")
    library.add("broken.mp3", None, title="Broken").update(file_path=str(library.folder / "broken.mp3"))
    (library.folder / "broken.mp3").write_bytes(b"\0" * 3000)
    library.add("gone.flac", None, title="Gone")
    plan = make(library, tmp_path, Spec("mp3", "256", max_rate=48000))
    got = by_title(plan)
    assert {t: s.action for t, s in got.items()} == {"Lossless": "convert", "Hires": "convert", "Fits": "copy", "Six": "cannot", "Broken": "cannot", "Gone": "cannot"}
    assert got["Hires"].notes == ["Resampled from 96 kHz to 48 kHz."] and got["Lossless"].notes == []
    assert got["Fits"].notes == ["Already in this format: copied as it is."]
    assert got["Six"].notes == ["This song has 6 channels; Convert handles mono and stereo."]
    assert got["Broken"].notes[0].startswith("The song cannot be read (") and got["Gone"].notes == ["The file is not there any more."]
    assert [plan.count(a) for a in ("convert", "copy", "cannot", "skip", "exists", "clash")] == [2, 1, 3, 0, 0, 0]
    assert plan.problems == [] and plan.ok and len(plan.writing) == 3


def test_songs_already_as_asked_for_can_be_left_out_or_converted_anyway(library: Library, tmp_path: Path) -> None:
    library.add("a.flac", audio.music(44100, 5), title="A")
    for existing, action in (("copy", "copy"), ("skip", "skip"), ("convert", "convert")):
        plan = make(library, tmp_path, Spec("flac", existing=existing))
        assert by_title(plan)["A"].action == action, existing
    only_skipped = make(library, tmp_path, Spec("flac", existing="skip"))
    assert only_skipped.problems == ["There is nothing to convert: every song is already as asked for, left out, or cannot be converted."] and not only_skipped.ok


def test_what_the_estimates_are_made_of(library: Library, tmp_path: Path) -> None:
    library.add("a.flac", audio.music(44100, 200), title="A")
    library.add("b.mp3", audio.music(44100, 10), fmt="MP3", subtype="MPEG_LAYER_III", title="B")
    spec = Spec("mp3", "320")
    plan = make(library, tmp_path, spec)
    got = by_title(plan)
    assert got["A"].bytes == 200 * 320 * 125 + 150 * 1024 and got["A"].seconds == pytest.approx(200, abs=0.1)  # (a cover picture is allowed 150 KB)
    assert got["B"].action == "copy" and got["B"].bytes == (library.folder / "b.mp3").stat().st_size
    assert make(library, tmp_path, spec, cover=False).songs[0].bytes == 200 * 320 * 125
    assert plan.bytes == got["A"].bytes + got["B"].bytes and plan.room_needed == int(plan.bytes * 1.05) + 64 * 1024 * 1024  # (5% and 64 MB to spare)


def test_the_estimate_of_a_song_whose_rate_changes_is_made_at_the_new_rate(library: Library, tmp_path: Path) -> None:
    library.add("hires.flac", audio.music(96000, 10), 96000, subtype="PCM_24", title="Hires")
    song = make(library, tmp_path, Spec("wav", max_rate=48000, depth="16"), cover=False).songs[0]
    assert song.action == "convert" and song.bytes == 10 * 48000 * 2 * 2 + 44  # (ten seconds of 48 kHz, 16-bit stereo, and a header)


def test_a_plan_is_good_only_with_something_to_write_and_no_problem() -> None:
    spec = Spec("mp3")
    writes = planning.PlannedSong(1, "/a.flac", "A", "X", action="convert")
    left_out = planning.PlannedSong(2, "/b.flac", "B", "X", action="skip")
    assert planning.ConvertPlan(spec, SCHEME, "/out", [writes], []).ok is True
    assert planning.ConvertPlan(spec, SCHEME, "/out", [left_out], []).ok is False  # (nothing to write)
    assert planning.ConvertPlan(spec, SCHEME, "/out", [], []).ok is False
    assert planning.ConvertPlan(spec, SCHEME, "/out", [writes], ["There is not enough room."]).ok is False  # (a problem)
    assert planning.ConvertPlan(spec, SCHEME, "/out", [left_out], []).room_needed == 0  # (nothing to write: no room to ask for, not even the spare)


# ---------------------------------------------------------------------------------------- the names


def test_a_song_goes_where_the_layout_puts_it_with_the_extension_of_the_new_format(library: Library, tmp_path: Path) -> None:
    library.add("one.flac", audio.music(44100, 5), title="Big in Japan", artist="Guano Apes", album_artist="Guano Apes", album="Don't Give Me Names", track_number=3)
    library.add("two.flac", audio.music(44100, 5), title="What? Why: Not", artist="Some: Band?", album_artist="", album="", track_number=0)
    for fmt in formats.FORMATS.values():
        plan = make(library, tmp_path, Spec(fmt.id))
        first, second = plan.songs
        assert first.relative == f"Guano Apes/Don't Give Me Names/03 - Big in Japan.{fmt.extension}", fmt.id
        assert second.relative == f"Some_ Band_/Unknown Album/What_ Why_ Not.{fmt.extension}"  # (made usable on Windows; a missing number takes nothing with it)
        assert first.target == os.path.normpath(str(tmp_path / "out" / first.relative))
    flat = make(library, tmp_path, Spec("opus"), scheme="{artist} - {title}.{ext}")
    assert [s.relative for s in flat.songs] == ["Guano Apes - Big in Japan.opus", "Some_ Band_ - What_ Why_ Not.opus"]


def test_a_layout_that_cannot_be_filled_in_is_refused(library: Library, tmp_path: Path) -> None:
    library.add("a.flac", audio.music(44100, 5))
    for bad in ("{nonsense}/{title}.{ext}", "../{title}.{ext}", "{title}", "/{title}.{ext}"):
        with pytest.raises(ValueError):
            make(library, tmp_path, Spec("mp3"), scheme=bad)


def test_a_choice_that_is_not_offered_is_refused(library: Library, tmp_path: Path) -> None:
    library.add("a.flac", audio.music(44100, 5))
    for spec in (Spec("aac"), Spec("mp3", "999"), Spec("flac", max_rate=96000), Spec("flac", depth="32"), Spec("flac", existing="overwrite")):
        with pytest.raises(ValueError):
            make(library, tmp_path, spec)


def test_a_file_that_is_already_there_is_left_alone_and_nothing_is_written_by_looking(library: Library, tmp_path: Path) -> None:
    library.add("a.flac", audio.music(44100, 5), title="A")
    library.add("b.flac", audio.music(44100, 5), title="B")
    out = tmp_path / "out" / "Aurora Vale" / "Northern Lights"
    out.mkdir(parents=True)
    (out / "01 - A.mp3").write_bytes(b"the person's own file")
    plan = make(library, tmp_path, Spec("mp3"))
    got = by_title(plan)
    assert (got["A"].action, got["A"].notes, got["B"].action) == ("exists", ["Already there: left as it is."], "convert")
    assert (out / "01 - A.mp3").read_bytes() == b"the person's own file" and not any(p.name.endswith(".part") for p in out.iterdir())
    assert plan.writing == [got["B"]] and got["A"].bytes == 0


def test_two_songs_with_the_same_name_in_one_batch_are_not_both_written(library: Library, tmp_path: Path) -> None:
    first = library.add("a.flac", audio.music(44100, 5), title="Same", track_number=1)
    library.add("b.flac", audio.music(44100, 5, seed=2), title="Same", track_number=1)
    library.add("c.flac", audio.music(44100, 5, seed=3), title="same", track_number=1)  # (the same name on Windows, whatever the letter case)
    plan = make(library, tmp_path, Spec("mp3"))
    assert [s.action for s in plan.songs] == ["convert", "clash", "clash" if os.path.normcase("A") == os.path.normcase("a") else "convert"]
    assert plan.songs[1].notes == ["Another song in this batch has the same name: this one is left out."] and plan.songs[0].track_id == first["id"]


def test_a_name_too_long_for_windows_is_said_so(library: Library, tmp_path: Path) -> None:
    library.add("a.flac", audio.music(44100, 5), title="T" * 100, album="A" * 100, album_artist="X" * 100)
    plan = make(library, tmp_path, Spec("mp3"))
    song = plan.songs[0]
    assert song.action == "cannot" and song.notes[0].startswith("The name is too long for Windows (") and not plan.ok


def test_the_longest_name_is_the_one_that_leaves_room_for_the_temporary_name(library: Library, tmp_path: Path) -> None:
    library.add("a.flac", audio.music(44100, 5), title="T", album="A", album_artist="X")
    base = len(str(tmp_path)) + 1
    for extra, fits in ((0, True), (1, False)):
        folder = tmp_path / ("x" * (248 - base + extra))  # (the song is then /T.mp3 after it: 254 characters, and 255)
        plan = planning.build_plan(library.rows, Spec("mp3"), str(folder), "{title}.{ext}", [str(library.folder)], ask_windows=lambda paths: {})
        song = plan.songs[0]
        assert len(song.target) == 254 + extra
        if fits:
            assert song.action == "convert" and plan.ok
        else:
            assert song.action == "cannot" and song.notes == ["The name is too long for Windows (255 characters; at most 254, which leaves room for the temporary name the song is written under)."]


def test_a_song_may_never_be_written_into_the_library(library: Library, tmp_path: Path) -> None:
    library.add("a.flac", audio.music(44100, 5))
    plan = planning.build_plan(library.rows, Spec("mp3"), str(tmp_path / "out"), "{title}.{ext}", [str(library.folder), str(tmp_path / "out" / "Inner")],
                               ask_windows=lambda paths: {})
    assert plan.songs[0].action == "convert"  # (a library folder inside the destination is not in the way unless a song would land in it)
    inner = Library(tmp_path / "out" / "Inner")
    inner.add("b.flac", audio.music(44100, 5), title="Inner")
    plan = planning.build_plan(library.rows, Spec("mp3"), str(tmp_path / "out"), "Inner/{title}.{ext}", [str(library.folder), str(tmp_path / "out" / "Inner")],
                               ask_windows=lambda paths: {})
    assert plan.songs[0].action == "cannot" and plan.songs[0].notes == ["This would write into your library."]


# ---------------------------------------------------------------------------------------- the folder


def test_the_folder_the_converted_songs_go_in(tmp_path: Path) -> None:
    roots = [str(tmp_path / "library")]
    (tmp_path / "library").mkdir()
    (tmp_path / "file").write_text("x")
    ok = tmp_path / "somewhere" / "deeper"
    assert planning.check_destination(str(ok), roots) == (ok, None)  # (made when the conversion starts)
    existing = tmp_path / "existing"
    existing.mkdir()
    assert planning.check_destination(str(existing) + os.sep, roots) == (existing, None)
    for raw, words in [
        ("", "Choose a folder"), ("   ", "Choose a folder"), ("music/converted", "whole path"), ("converted", "whole path"),
        (str(tmp_path / "library"), "outside your library folders"), (str(tmp_path / "library" / "Artist"), "outside your library folders"),
        (str(tmp_path / "file"), "a file, not a folder"), (str(tmp_path / "file" / "inside"), "in the way"), ("\\\\.\\PhysicalDrive0", "not a folder the converted songs can go in"),
        ("\\\\?\\C:\\x", "not a folder the converted songs can go in"),
    ]:
        folder, problem = planning.check_destination(raw, roots)
        assert folder is None and words in (problem or ""), raw


def test_a_folder_on_a_drive_that_is_not_there_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(planning, "_existing_ancestor", lambda path: None)  # (no part of the path is there: a drive letter that nothing is plugged in as)
    assert planning.check_destination(str(tmp_path / "converted"), []) == (None, "That folder's drive is not there.")


def test_an_empty_library_folder_in_the_settings_is_ignored_not_taken_for_the_folder_the_app_is_in(library: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    library.add("a.flac", audio.music(44100, 3), title="A")
    monkeypatch.chdir(tmp_path)  # (an empty name means "here" to the system, and the folder for the converted songs is in here)
    plan = planning.build_plan(library.rows, Spec("mp3"), str(tmp_path / "out"), SCHEME, ["", str(library.folder)], ask_windows=lambda paths: {})
    assert plan.ok and [(s.action, s.notes) for s in plan.songs] == [("convert", [])] and plan.destination == str(tmp_path / "out")


def test_a_folder_that_only_looks_like_a_library_folder_is_not_one(tmp_path: Path) -> None:
    (tmp_path / "Music").mkdir()
    assert planning.check_destination(str(tmp_path / "Music-converted"), [str(tmp_path / "Music")])[1] is None
    assert planning.check_destination(str(tmp_path), [str(tmp_path / "Music")])[1] is None  # (a folder that holds the library is another matter: nothing lands inside it)


def test_a_bad_folder_is_a_problem_and_the_names_are_still_shown(library: Library, tmp_path: Path) -> None:
    library.add("a.flac", audio.music(44100, 5), title="A")
    plan = planning.build_plan(library.rows, Spec("mp3"), str(library.folder / "mp3s"), SCHEME, [str(library.folder)], ask_windows=lambda paths: {})
    assert plan.problems and "outside your library folders" in plan.problems[0] and not plan.ok
    assert plan.songs[0].action == "convert" and plan.songs[0].relative == "Aurora Vale/Northern Lights/01 - A.mp3" and plan.songs[0].target == ""
    assert plan.destination == "" and plan.free_bytes is None


def test_the_room_for_the_songs(library: Library, tmp_path: Path) -> None:
    library.add("a.flac", audio.music(44100, 600), title="A")
    spec = Spec("mp3", "320")
    needed = make(library, tmp_path, spec).room_needed
    assert needed > 600 * 320 * 125
    short = make(library, tmp_path, spec, free_bytes=lambda folder: needed - 1)
    assert short.free_bytes == needed - 1 and len(short.problems) == 1 and short.problems[0].startswith("There is not enough room: about ") and not short.ok
    enough = make(library, tmp_path, spec, free_bytes=lambda folder: needed)
    assert enough.problems == [] and enough.ok
    unknown = make(library, tmp_path, spec, free_bytes=lambda folder: None)
    assert unknown.free_bytes is None and unknown.ok
    real = make(library, tmp_path, spec)  # (the drive the folder is on, asked for real)
    assert isinstance(real.free_bytes, int) and real.free_bytes > 0


def test_nothing_to_convert_is_a_problem(library: Library, tmp_path: Path) -> None:
    assert make(library, tmp_path, Spec("mp3")).problems == ["There is nothing to convert: every song is already as asked for, left out, or cannot be converted."]
    library.add("six.flac", np.zeros((44100, 6), dtype=np.int16) + 5)
    assert make(library, tmp_path, Spec("mp3")).ok is False


# ---------------------------------------------------------------------------------------- songs only Windows decodes


def test_windows_is_asked_once_about_every_song_only_it_can_decode(library: Library, tmp_path: Path) -> None:
    library.add("a.m4a", None, title="Aac")
    library.add("b.wma", None, title="Wma")
    library.add("c.flac", audio.music(44100, 5), title="Flac")
    library.add("d.m4a", None, title="Locked")
    for name in ("a.m4a", "b.wma", "d.m4a"):
        (library.folder / name).write_bytes(b"x" * 40_000)
    asked: list[list[str]] = []

    def windows(paths: list[str]) -> dict[str, dict[str, Any]]:
        asked.append(paths)
        return {
            paths[0]: {"ok": True, "codec": "AAC", "rate": 44100, "channels": 2, "seconds": 200.0},
            paths[1]: {"ok": True, "codec": "WMA", "rate": 48000, "channels": 2, "seconds": 100.0},
            paths[2]: {"ok": False, "error": "Windows cannot decode Locked: it is copy-protected."},
        }

    plan = make(library, tmp_path, Spec("mp3", "256"), ask_windows=windows)
    assert asked == [[str(library.folder / n) for n in ("a.m4a", "b.wma", "d.m4a")]]
    got = by_title(plan)
    assert got["Aac"].action == "convert" and got["Aac"].notes == ["Windows decodes this M4A song first.", "A lossy song converted to another lossy format loses a little more quality."]
    assert got["Aac"].seconds == 200.0 and got["Aac"].info.via == "windows" and got["Aac"].bytes == 200 * 256 * 125 + planning.COVER_ALLOWANCE
    assert got["Wma"].action == "convert" and got["Wma"].notes == ["Windows decodes this WMA song first.", "A lossy song converted to another lossy format loses a little more quality."]
    assert (got["Locked"].action, got["Locked"].notes) == ("cannot", ["Windows cannot decode Locked: it is copy-protected."])
    assert got["Flac"].action == "convert"


def test_windows_is_not_asked_about_a_song_the_audio_library_reads_whatever_it_is_called(library: Library, tmp_path: Path) -> None:
    sf.write(str(library.folder / "really-flac.m4a"), audio.music(44100, 5), 44100, format="FLAC", subtype="PCM_16")
    library.add("really-flac.m4a", None, title="Disguised")
    assert make(library, tmp_path, Spec("mp3")).songs[0].action == "convert"  # (make() fails the test if Windows is asked)


def test_a_windows_that_says_nothing_about_a_song_leaves_it_unconverted_in_words(library: Library, tmp_path: Path) -> None:
    library.add("a.m4a", None, title="Aac")
    (library.folder / "a.m4a").write_bytes(b"x" * 100)
    plan = make(library, tmp_path, Spec("mp3"), ask_windows=lambda paths: {})
    assert (plan.songs[0].action, plan.songs[0].notes) == ("cannot", ["Windows could not say what is in this song."])


def test_a_file_that_is_not_one_windows_decodes_and_not_readable_is_refused_in_words(library: Library, tmp_path: Path) -> None:
    library.add("a.xyz", None, title="Odd")
    (library.folder / "a.xyz").write_bytes(b"x" * 100)
    plan = make(library, tmp_path, Spec("mp3"))
    assert plan.songs[0].action == "cannot" and plan.songs[0].notes[0].startswith("The song cannot be read (")


# ---------------------------------------------------------------------------------------- progress, stopping, and what is shown


def test_progress_counts_the_songs_and_a_stop_ends_the_looking(library: Library, tmp_path: Path) -> None:
    for number in range(5):
        library.add(f"{number}.flac", audio.music(44100, 3, seed=number), title=f"S{number}")
    seen: list[tuple[int, int]] = []
    make(library, tmp_path, Spec("mp3"), progress=lambda done, total: seen.append((done, total)))
    assert seen == [(n, 5) for n in range(1, 6)]
    looked: list[Path] = []

    class Stop(Exception):
        pass

    def inspector(path: Path) -> SourceInfo:
        looked.append(path)
        return inspect_source(path)

    def check() -> None:
        if len(looked) == 2:
            raise Stop

    with pytest.raises(Stop):
        make(library, tmp_path, Spec("mp3"), inspector=inspector, check=check)
    assert len(looked) == 2


def test_the_summary_and_the_pages_of_songs(library: Library, tmp_path: Path) -> None:
    for number in range(7):
        library.add(f"{number}.flac", audio.music(44100, 3, seed=number), title=f"S{number}", track_number=number + 1)
    library.add("six.flac", np.zeros((44100, 6), dtype=np.int16) + 5, title="Six")
    plan = make(library, tmp_path, Spec("opus", "96"), free_bytes=lambda folder: 10**12)
    summary = plan.summary()
    assert summary["ok"] is True and summary["songs"] == 8 and summary["counts"] == {"convert": 7, "copy": 0, "skip": 0, "cannot": 1, "exists": 0, "clash": 0}
    assert (summary["format"], summary["quality"], summary["scheme"], summary["destination"]) == ("opus", "96", SCHEME, str(tmp_path / "out"))
    assert summary["bytes"] == plan.bytes > 0 and summary["seconds"] == 21 and summary["free_bytes"] == 10**12 and summary["problems"] == []
    page = plan.page("convert", offset=2, limit=3)
    assert page["total"] == 7 and [i["title"] for i in page["items"]] == ["S2", "S3", "S4"]
    assert page["items"][0]["to"] == "Aurora Vale/Northern Lights/03 - S2.opus" and page["items"][0]["from"].endswith("2.flac") and page["items"][0]["id"] == 3
    assert plan.page("cannot")["items"][0]["notes"] == ["This song has 6 channels; Convert handles mono and stereo."]
    assert plan.page("convert", offset=-5, limit=10_000)["items"][0]["title"] == "S0" and len(plan.page("convert", limit=10_000)["items"]) == 7
    many = planning.ConvertPlan(Spec("mp3"), SCHEME, "/out", [planning.PlannedSong(n, f"/m/{n}.flac", f"S{n}", "A", "convert") for n in range(600)], [])
    assert (many.page("convert", limit=10_000)["total"], len(many.page("convert", limit=10_000)["items"])) == (600, 500)  # (a page is never more than 500 songs)
    assert len(many.page("convert", limit=0)["items"]) == 1 and len(many.page("convert", limit=-3)["items"]) == 1 and len(many.page("convert", offset=550, limit=100)["items"]) == 50
    assert Spec("opus").quality is None and make(library, tmp_path, Spec("opus")).summary()["quality"] == "128"  # (the default of the format, said in the summary)
    with pytest.raises(ValueError, match="action must be one of"):
        plan.page("everything")


@pytest.mark.parametrize("size,said", [
    (0, "0 bytes"), (1, "1 bytes"), (1023, "1023 bytes"), (1024, "1.0 KB"), (1536, "1.5 KB"), (1024**2, "1.0 MB"), (int(2.5 * 1024**3), "2.5 GB"),
    (1024**4, "1.0 TB"), (3 * 1024**4, "3.0 TB"), (2048 * 1024**4, "2048.0 TB"),
])
def test_a_size_is_said_in_the_biggest_unit_it_fills(size: int, said: str) -> None:
    assert planning.size_text(size) == said


def test_a_song_without_a_title_tag_is_known_by_its_file_name(library: Library, tmp_path: Path) -> None:
    library.add("no title here.flac", audio.music(44100, 3), title="")
    library.add("another one.flac", audio.music(44100, 3, seed=2), title=None)
    assert [s.title for s in make(library, tmp_path, Spec("mp3")).songs] == ["no title here", "another one"]


def test_a_song_that_cannot_be_converted_does_not_take_the_name_of_one_that_can(library: Library, tmp_path: Path) -> None:
    library.add("six.flac", np.zeros((44100, 6), dtype=np.int16) + 5, title="Same", track_number=1)
    library.add("two.flac", audio.music(44100, 3), title="Same", track_number=1)
    plan = make(library, tmp_path, Spec("mp3"))
    assert [(s.action, s.relative, s.target) for s in plan.songs] == [("cannot", "", ""), ("convert", "Aurora Vale/Northern Lights/01 - Same.mp3", str(tmp_path / "out" / "Aurora Vale/Northern Lights/01 - Same.mp3"))]


@pytest.mark.parametrize("slip,said", [
    (ValueError("template result '/x' is not a path inside the library"), "template result '/x' is not a path inside the library"),
    (KeyError("genre"), "'genre'"), (IndexError("string index out of range"), "string index out of range"),
])
def test_a_layout_that_slips_on_one_song_says_so_of_that_song_and_goes_on(library: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slip: Exception, said: str) -> None:
    library.add("a.flac", audio.music(44100, 3), title="A")
    library.add("b.flac", audio.music(44100, 3, seed=2), title="B")
    real = planning.organizer.render_relative

    def render(row: Any, scheme: str, *args: Any) -> str:
        if row["title"] == "A":
            raise slip
        return real(row, scheme, *args)

    monkeypatch.setattr(planning.organizer, "render_relative", render)
    got = by_title(make(library, tmp_path, Spec("mp3")))
    assert (got["A"].action, got["A"].notes) == ("cannot", [f"The layout does not give this song a place ({said})."])
    assert (got["B"].action, got["B"].relative) == ("convert", "Aurora Vale/Northern Lights/02 - B.mp3")


def test_a_bad_folder_and_nothing_to_convert_are_one_problem_not_two(library: Library, tmp_path: Path) -> None:
    plan = planning.build_plan(library.rows, Spec("mp3"), str(library.folder / "mp3s"), SCHEME, [str(library.folder)], ask_windows=lambda paths: {})  # (no songs at all)
    assert len(plan.problems) == 1 and "outside your library folders" in plan.problems[0]


def test_a_failed_inspection_in_words_does_not_stop_the_other_songs(library: Library, tmp_path: Path) -> None:
    library.add("a.flac", audio.music(44100, 3), title="A")
    library.add("b.flac", audio.music(44100, 3), title="B")

    def inspector(path: Path) -> SourceInfo:
        if path.name == "a.flac":
            raise SourceError("The file is damaged in a way nobody expected.")
        return inspect_source(path)

    plan = make(library, tmp_path, Spec("mp3"), inspector=inspector)
    assert [(s.title, s.action) for s in plan.songs] == [("A", "cannot"), ("B", "convert")] and plan.songs[0].notes == ["The file is damaged in a way nobody expected."]
