"""The job that converts: every song to its place, with its tags and cover, and what happens when something goes wrong half way."""

from __future__ import annotations

import contextlib
import errno
import io
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import numpy as np
import pytest
from PIL import Image

from musictoolkit.convert import engine, job, store, windows
from musictoolkit.convert import plan as planning
from musictoolkit.convert.engine import ConvertError, Spec
from musictoolkit.convert.job import ConvertProblem
from musictoolkit.convert.source import SourceError
from musictoolkit.convert.tags import read_tags
from musictoolkit.db.connection import connect
from musictoolkit.discs import rip as ripper
from musictoolkit.discs.helper import DiscError, DiscUnavailable
from musictoolkit.discs.tagging import SongTags, write_tags
from musictoolkit.media import art
from musictoolkit.web.jobs import JobCancelled
from tests import convert_audio as audio
from tests.convert_library import Library, stand_in_windows
from tests.fake_disc_helper import audio as fake_audio

SCHEME = "{album_artist}/{album}/{track:02d} - {title}.{ext}"
RECORDING, RELEASE = "95042c0a-5b64-4f61-ba75-2a1be26466d3", "07090529-0fbf-4bd3-adc4-fe627343976d"


def jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (240, 240), (30, 90, 200)).save(out, "JPEG")
    return out.getvalue()


COVER = jpeg()


class Handle:
    """What a job function is given: it can be told to stop (when it says something, if `cancel_when` says so), and it keeps what it was told."""

    def __init__(self, cancel_when: Callable[[dict[str, Any]], bool] | None = None) -> None:
        self.cancelled = False
        self.updates: list[dict[str, Any]] = []
        self.cancel_when = cancel_when

    def check(self) -> None:
        if self.cancelled:
            raise JobCancelled()

    def update(self, **fields: Any) -> None:
        self.updates.append(fields)
        if self.cancel_when and self.cancel_when(fields):
            self.cancelled = True

    def log(self, line: str) -> None:
        pass


@pytest.fixture
def rig(tmp_path: Path) -> SimpleNamespace:
    path = tmp_path / "music.db"

    @contextlib.contextmanager
    def db():
        conn = connect(path)
        try:
            yield conn
        finally:
            conn.close()

    return SimpleNamespace(db=db, library=Library(tmp_path / "library"), out=tmp_path / "out", scratch=tmp_path / "scratch", tmp=tmp_path)


def song_file(rig: SimpleNamespace, name: str, seconds: float = 6, seed: int = 1, cover: bool = True, **tags: Any) -> tuple[dict[str, Any], SongTags]:
    """A FLAC song in the library with a full set of tags and a cover, and the row for it."""
    row = rig.library.add(name, audio.music(44100, seconds, seed=seed), **tags)
    wrote = SongTags(
        row["title"], row["artist"], row["album"], row["album_artist"], row["track_number"], 9, 1, 2, 2019, "2019-03-04", "Rock", RECORDING, RELEASE, None, [], "", "",
        "A Composer", "A comment", "la la\nla", {"track_gain": "-5.50 dB", "track_peak": "0.900000"},
    )
    write_tags(Path(row["file_path"]), "flac", wrote, COVER if cover else None)
    return row, wrote


def run(rig: SimpleNamespace, spec: Spec | None = None, *, handle: Handle | None = None, cover: bool = True, scheme: str = SCHEME, destination: Path | None = None,
        wrap: Callable[[planning.ConvertPlan], None] | None = None) -> tuple[dict[str, Any], Handle]:
    spec = spec or Spec("mp3", "256")
    handle = handle or Handle()
    where = str(destination or rig.out)

    def make_plan(progress: Callable[[int, int], None], check: Callable[[], None]) -> planning.ConvertPlan:
        made = planning.build_plan(rig.library.rows, spec, where, scheme, [str(rig.library.folder)], progress=progress, check=check, cover=cover)
        if wrap:
            wrap(made)
        return made

    result = job.run_conversion(db=rig.db, make_plan=make_plan, request=job.Request(spec, where, scheme, cover, previewed=len(rig.library.rows)), batch_id="batch1", scratch=rig.scratch, handle=handle)
    return result, handle


def header(rig: SimpleNamespace, conversion_id: int = 1) -> dict[str, Any]:
    with rig.db() as conn:
        return store.get_conversion(conn, conversion_id)


def parts_in(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*") if p.name.endswith(".part")] if folder.exists() else []


# ---------------------------------------------------------------------------------------- the songs are written


@pytest.mark.parametrize("spec,title", [
    (Spec("mp3", "256"), "1 song to MP3 (256 kbps)"), (Spec("ogg", "160"), "1 song to Ogg Vorbis (about 160 kbps)"), (Spec("opus", "96"), "1 song to Opus (about 96 kbps)"),
    (Spec("flac"), "1 song to FLAC"), (Spec("wav"), "1 song to WAV"),
])
def test_a_conversion_is_titled_by_its_format_and_how_good_it_is(rig: SimpleNamespace, spec: Spec, title: str) -> None:
    song_file(rig, "a.flac", title="A")
    run(rig, spec)
    assert header(rig)["title"] == title


def test_songs_go_to_their_places_with_their_tags_and_cover(rig: SimpleNamespace) -> None:
    sources = [song_file(rig, f"{n}.flac", seed=n, title=title, track_number=n) for n, title in enumerate(("One", "Two", "Three"), start=1)]
    result, _ = run(rig)
    for (row, wrote), name in zip(sources, ("01 - One", "02 - Two", "03 - Three")):
        target = rig.out / "Aurora Vale" / "Northern Lights" / f"{name}.mp3"
        assert target.is_file()
        got = read_tags(target)
        for field in ("title", "artist", "album", "album_artist", "track_number", "track_total", "disc_number", "disc_total", "year", "date", "genre", "recording_id",
                      "release_id", "composer", "comment", "lyrics", "replaygain"):
            assert getattr(got, field) == getattr(wrote, field), (name, field)
        assert art.embedded_art(target) == COVER
        back, rate = audio.read(target)
        assert rate == 44100 and len(back) / rate == pytest.approx(6.0, abs=0.06)
    assert parts_in(rig.out) == [] and not list(rig.scratch.glob("*"))
    assert (result["status"], result["converted"], result["copied"], result["failed"], result["left_out"], result["songs"]) == ("done", 3, 0, 0, 0, 3)
    assert result["destination"] == str(rig.out) and result["previewed"] == 3 and result["message"] is None and result["conversion_id"] == 1
    saved = header(rig)
    assert (saved["status"], saved["title"], saved["format"], saved["quality"], saved["scheme"]) == ("done", "3 songs to MP3 (256 kbps)", "mp3", "256", SCHEME)
    assert saved["options"] == {"max_rate": 0, "depth": "16", "existing": "copy", "cover": True} and saved["finished_at"] and saved["undone_at"] is None
    assert [i["status"] for i in saved["items"]] == ["done"] * 3 and saved["items_total"] == 3
    assert saved["bytes"] == sum(i["size"] for i in saved["items"]) == sum(p.stat().st_size for p in rig.out.rglob("*.mp3"))
    assert [i["action"] for i in saved["items"]] == ["convert"] * 3 and saved["items"][0]["target"] == str(rig.out / "Aurora Vale/Northern Lights/01 - One.mp3")


def test_progress_runs_from_nothing_to_the_whole_in_music_not_in_songs(rig: SimpleNamespace) -> None:
    song_file(rig, "long.flac", seconds=30, title="Long", track_number=1)
    song_file(rig, "short.flac", seconds=10, seed=2, title="Short", track_number=2)
    _, handle = run(rig)
    sized = [u for u in handle.updates if u.get("total") == 40_000]  # (the conversion's own: forty seconds of music, in milliseconds)
    done = [u["done"] for u in sized]
    assert done[0] == 0 and done == sorted(done) and done[-1] == 40_000
    messages = [u["message"] for u in handle.updates if "Converting" in u.get("message", "")]
    assert "Converting song 1 of 2: Long" in messages and "Converting song 2 of 2: Short" in messages
    assert any(m.startswith("Converting song 1 of 2: Long (") and m.endswith("%)") for m in messages) and any(m.endswith("(100%)") for m in messages)
    second = [u["done"] for u in sized if u.get("message", "").startswith("Converting song 2 of 2: Short")]
    assert min(second) >= 30_000  # (the long song is three quarters of the music, and was done first)


def test_the_cover_is_left_out_when_the_person_said_so_and_the_tags_stay(rig: SimpleNamespace) -> None:
    song_file(rig, "a.flac", title="A")
    run(rig, cover=False)
    target = rig.out / "Aurora Vale/Northern Lights/01 - A.mp3"
    assert art.embedded_art(target) is None and read_tags(target).title == "A" and header(rig)["options"]["cover"] is False


def test_a_song_that_has_no_tags_of_its_own_takes_them_from_the_library(rig: SimpleNamespace) -> None:
    rig.library.add("plain.flac", audio.music(44100, 4), title="From The Library", artist="Library Artist", track_number=5)
    run(rig)
    got = read_tags(rig.out / "Aurora Vale/Northern Lights/05 - From The Library.mp3")
    assert (got.title, got.artist, got.lyrics, got.recording_id) == ("From The Library", "Library Artist", "", None)


def test_a_song_already_in_the_format_is_copied_exactly_with_what_it_has(rig: SimpleNamespace) -> None:
    row = rig.library.add("fits.mp3", audio.music(44100, 6), fmt="MP3", subtype="MPEG_LAYER_III", title="Fits", track_number=1)
    write_tags(Path(row["file_path"]), "mp3", SongTags("Fits Tagged", "Someone", "Album", "", 1, 0), COVER)
    result, _ = run(rig, Spec("mp3", "320"))
    target = rig.out / "Aurora Vale/Northern Lights/01 - Fits.mp3"
    assert target.read_bytes() == Path(row["file_path"]).read_bytes()  # (its own tags, untouched: "Fits Tagged", not the library's "Fits")
    assert (result["converted"], result["copied"]) == (0, 1) and header(rig)["items"][0]["action"] == "copy"


def test_songs_left_out_are_counted_and_nothing_is_said_to_have_failed(rig: SimpleNamespace) -> None:
    song_file(rig, "a.flac", title="A", track_number=1)
    rig.library.add("six.flac", np.zeros((44100, 6), dtype=np.int16) + 9, title="Six", track_number=2)
    song_file(rig, "there.flac", seed=3, title="There", track_number=3)
    (rig.out / "Aurora Vale/Northern Lights").mkdir(parents=True)
    keep = rig.out / "Aurora Vale/Northern Lights/03 - There.mp3"
    keep.write_bytes(b"the person's own")
    result, _ = run(rig)
    assert (result["status"], result["converted"], result["failed"], result["left_out"], result["songs"]) == ("done", 1, 0, 2, 1) and result["message"] is None
    assert keep.read_bytes() == b"the person's own" and sorted(p.name for p in keep.parent.iterdir()) == ["01 - A.mp3", "03 - There.mp3"]


def test_the_plan_is_made_again_so_a_song_added_since_the_preview_is_converted(rig: SimpleNamespace) -> None:
    song_file(rig, "a.flac", title="A", track_number=1)
    spec = Spec("mp3", "256")
    shown = planning.build_plan(rig.library.rows, spec, str(rig.out), SCHEME, [str(rig.library.folder)])
    song_file(rig, "b.flac", seed=2, title="B", track_number=2)  # (after the preview)
    result, _ = run(rig, spec)
    assert len(shown.writing) == 1 and result["converted"] == 2 and result["previewed"] == 2  # (the request says what the screen showed: the caller passes it)


def test_a_file_that_turns_up_while_the_songs_are_being_looked_at_is_not_replaced_and_its_song_is_not_even_converted(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    song_file(rig, "a.flac", title="A", track_number=1)
    song_file(rig, "b.flac", seed=2, title="B", track_number=2)
    taken = rig.out / "Aurora Vale/Northern Lights/02 - B.mp3"
    converted: list[str] = []
    real = engine.convert_file

    def spy(source, target, info, spec, **kwargs):
        converted.append(Path(source).name)
        return real(source, target, info, spec, **kwargs)

    monkeypatch.setattr(engine, "convert_file", spy)

    def wrap(made: planning.ConvertPlan) -> None:  # (the plan has been made; then the person's own file appears)
        taken.parent.mkdir(parents=True, exist_ok=True)
        taken.write_bytes(b"arrived late")

    result, _ = run(rig, wrap=wrap)
    assert taken.read_bytes() == b"arrived late" and (result["converted"], result["status"]) == (1, "done")
    skipped = [i for i in header(rig)["items"] if i["status"] == "skipped"]
    assert [(i["title"], i["message"]) for i in skipped] == [("B", "Already there: left as it is.")] and parts_in(rig.out) == []
    assert converted == ["a.flac"]  # (there was no point in making a copy of B that would be thrown away)


def test_a_batch_in_which_every_song_turned_out_to_be_there_already_is_done_and_not_failed(rig: SimpleNamespace) -> None:
    song_file(rig, "a.flac", title="A", track_number=1)
    taken = rig.out / "Aurora Vale/Northern Lights/01 - A.mp3"

    def wrap(made: planning.ConvertPlan) -> None:
        taken.parent.mkdir(parents=True, exist_ok=True)
        taken.write_bytes(b"mine")

    result, _ = run(rig, wrap=wrap)
    assert (result["status"], result["converted"], result["failed"], result["message"]) == ("done", 0, 0, None) and taken.read_bytes() == b"mine"
    assert header(rig)["status"] == "done"


def test_a_file_that_turns_up_while_its_own_song_is_being_converted_is_not_replaced(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    song_file(rig, "a.flac", title="A", track_number=1)
    song_file(rig, "b.flac", seed=2, title="B", track_number=2)
    taken = rig.out / "Aurora Vale/Northern Lights/02 - B.mp3"
    real = engine.convert_file

    def meanwhile(source, target, info, spec, **kwargs):
        if Path(source).name == "b.flac":  # (the person's own file takes the name while the song is being written under its temporary one)
            taken.write_bytes(b"arrived meanwhile")
        return real(source, target, info, spec, **kwargs)

    monkeypatch.setattr(engine, "convert_file", meanwhile)
    result, _ = run(rig)
    assert taken.read_bytes() == b"arrived meanwhile" and (result["converted"], result["status"]) == (1, "done")
    skipped = [i for i in header(rig)["items"] if i["status"] == "skipped"]
    assert [(i["title"], i["message"]) for i in skipped] == [("B", "Already there: left as it is.")] and parts_in(rig.out) == []


def test_a_leftover_under_the_temporary_name_is_replaced_and_never_written_through(rig: SimpleNamespace) -> None:
    song_file(rig, "a.flac", title="A", track_number=1)
    precious = rig.tmp / "precious.txt"
    precious.write_bytes(b"the person's own file")
    album = rig.out / "Aurora Vale" / "Northern Lights"
    album.mkdir(parents=True)
    leftover = album / ("01 - A.mp3" + store.PART_SUFFIX)
    try:
        leftover.symlink_to(precious)  # (what a name under the temporary one could be, if anything put a link there)
    except (OSError, NotImplementedError):
        pytest.skip("this system does not let a test make a link")
    result, _ = run(rig)
    assert (result["converted"], result["status"]) == (1, "done") and (album / "01 - A.mp3").is_file() and not (album / "01 - A.mp3").is_symlink()
    assert precious.read_bytes() == b"the person's own file" and parts_in(rig.out) == []


def test_a_leftover_that_cannot_be_taken_away_fails_its_own_song_and_the_others_go_on(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    song_file(rig, "a.flac", title="A", album="Solo", album_artist="Only One", track_number=1)
    song_file(rig, "b.flac", seed=2, title="B", track_number=1)
    solo = rig.out / "Only One" / "Solo"
    solo.mkdir(parents=True)
    held = solo / ("01 - A.mp3" + store.PART_SUFFIX)
    held.write_bytes(b"half of an earlier try")
    real = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == held:
            raise PermissionError(errno.EACCES, "The process cannot access the file because it is being used by another process")
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    result, _ = run(rig)
    assert (result["status"], result["converted"], result["failed"]) == ("partial", 1, 1)
    assert [(i["title"], i["status"]) for i in header(rig)["items"]] == [("A", "failed"), ("B", "done")]
    assert held.read_bytes() == b"half of an earlier try" and (rig.out / "Aurora Vale/Northern Lights/01 - B.mp3").is_file()


# ---------------------------------------------------------------------------------------- a conversion that cannot begin


def test_a_plan_with_a_problem_is_refused_in_words_and_writes_nothing(rig: SimpleNamespace) -> None:
    song_file(rig, "a.flac", title="A")
    with pytest.raises(ConvertProblem, match="outside your library folders"):
        run(rig, destination=rig.library.folder / "converted")
    assert not (rig.library.folder / "converted").exists()
    with rig.db() as conn:
        assert store.list_conversions(conn) == []
    empty = rig.tmp / "nothing"
    rig.library.rows.clear()
    with pytest.raises(ConvertProblem, match="There is nothing to convert"):
        run(rig, destination=empty)
    assert not empty.exists()


def test_a_folder_that_cannot_be_made_is_said_so(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    song_file(rig, "a.flac", title="A")
    real = Path.mkdir

    def mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == rig.out:
            raise PermissionError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(ConvertProblem, match=r"^The folder could not be made \(Permission denied\)\.$"):
        run(rig)
    with rig.db() as conn:
        assert store.list_conversions(conn) == []


def test_a_song_is_written_beside_its_place_under_a_temporary_name_and_only_then_takes_its_own(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    song_file(rig, "a.flac", title="A", track_number=1)
    rig.library.add("b.mp3", audio.music(44100, 4), fmt="MP3", subtype="MPEG_LAYER_III", title="B", track_number=2)  # (256 kbps: only copied)
    album = rig.out / "Aurora Vale" / "Northern Lights"
    seen: list[tuple[str, str, bool]] = []
    real_convert, real_copy = engine.convert_file, engine.copy_file

    def convert(source, target, info, spec, **kwargs):
        seen.append(("convert", Path(target).relative_to(album).as_posix(), (album / "01 - A.mp3").exists()))
        return real_convert(source, target, info, spec, **kwargs)

    def copy(source, target, **kwargs):
        seen.append(("copy", Path(target).relative_to(album).as_posix(), (album / "02 - B.mp3").exists()))
        return real_copy(source, target, **kwargs)

    monkeypatch.setattr(engine, "convert_file", convert)
    monkeypatch.setattr(engine, "copy_file", copy)
    result, _ = run(rig)
    assert (result["converted"], result["copied"]) == (1, 1)
    assert seen == [("convert", "01 - A.mp3.part", False), ("copy", "02 - B.mp3.part", False)]  # (nothing under its own name until it is whole)
    assert sorted(p.name for p in album.iterdir()) == ["01 - A.mp3", "02 - B.mp3"]


# ---------------------------------------------------------------------------------------- something goes wrong half way


def failing(real: Callable[..., Any], when: Callable[[str], bool], error: Exception) -> Callable[..., Any]:
    def convert(source, target, info, spec, **kwargs):
        if when(Path(str(source)).name):
            raise error
        return real(source, target, info, spec, **kwargs)
    return convert


def test_a_song_that_fails_is_said_so_and_the_others_go_on(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    for n, title in enumerate(("A", "B", "C"), start=1):
        song_file(rig, f"{title.lower()}.flac", seed=n, title=title, track_number=n)
    monkeypatch.setattr(engine, "convert_file", failing(engine.convert_file, lambda name: name == "b.flac", ConvertError("The song could not be converted (boom).")))
    result, _ = run(rig)
    assert (result["status"], result["converted"], result["failed"]) == ("partial", 2, 1) and result["message"] == "1 song could not be converted."
    items = header(rig)["items"]
    assert [(i["title"], i["status"], i["message"]) for i in items] == [("A", "done", None), ("B", "failed", "The song could not be converted (boom)."), ("C", "done", None)]
    assert sorted(p.name for p in (rig.out / "Aurora Vale/Northern Lights").iterdir()) == ["01 - A.mp3", "03 - C.mp3"]


def test_when_no_song_can_be_converted_it_is_an_error_in_words(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    song_file(rig, "a.flac", title="A")
    monkeypatch.setattr(engine, "convert_file", failing(engine.convert_file, lambda name: True, ConvertError("The song could not be converted (boom).")))
    with pytest.raises(ConvertProblem, match=r"^1 song could not be converted\.$"):
        run(rig)
    saved = header(rig)
    assert (saved["status"], saved["failed"], saved["converted"]) == ("failed", 1, 0) and parts_in(rig.out) == []


def test_running_out_of_room_ends_the_batch_and_leaves_the_songs_that_were_done(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    for n, title in enumerate(("A", "B", "C", "D"), start=1):
        song_file(rig, f"{title.lower()}.flac", seed=n, title=title, track_number=n)
    monkeypatch.setattr(engine, "convert_file", failing(engine.convert_file, lambda name: name == "b.flac", ConvertError(engine.NO_ROOM)))
    result, _ = run(rig)
    assert (result["status"], result["converted"], result["failed"], result["message"]) == ("partial", 1, 1, engine.NO_ROOM)
    assert [(i["title"], i["status"]) for i in header(rig)["items"]] == [("A", "done"), ("B", "failed"), ("C", "skipped"), ("D", "skipped")]
    assert header(rig)["items"][2]["message"] == engine.NO_ROOM and [p.name for p in (rig.out / "Aurora Vale/Northern Lights").iterdir()] == ["01 - A.mp3"]


def test_a_drive_too_full_to_make_a_folder_ends_the_batch_like_one_too_full_to_write_a_song(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    for n, title in enumerate(("A", "B", "C"), start=1):
        song_file(rig, f"{title.lower()}.flac", seed=n, title=title, track_number=n)
    real, made = job._make_folders, []

    def make(folder: Path) -> list[Path]:
        made.append(folder)
        if len(made) == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(folder)

    monkeypatch.setattr(job, "_make_folders", make)
    result, _ = run(rig)
    assert (result["status"], result["converted"], result["failed"], result["message"]) == ("partial", 1, 1, engine.NO_ROOM)
    assert [(i["title"], i["status"], i["message"]) for i in header(rig)["items"]] == [("A", "done", None), ("B", "failed", engine.NO_ROOM), ("C", "skipped", engine.NO_ROOM)]


def test_a_folder_that_cannot_be_made_for_another_reason_is_a_failed_song_and_the_batch_goes_on(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    for n, title in enumerate(("A", "B", "C"), start=1):
        song_file(rig, f"{title.lower()}.flac", seed=n, title=title, track_number=n)
    real, made = job._make_folders, []

    def make(folder: Path) -> list[Path]:
        made.append(folder)
        if len(made) == 2:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(folder)

    monkeypatch.setattr(job, "_make_folders", make)
    result, _ = run(rig)
    assert (result["status"], result["converted"], result["failed"]) == ("partial", 2, 1)
    assert [(i["title"], i["status"], i["message"]) for i in header(rig)["items"]] == [("A", "done", None), ("B", "failed", "The file could not be written (Permission denied)."), ("C", "done", None)]


def test_no_room_for_the_first_song_is_an_error(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    song_file(rig, "a.flac", title="A")
    monkeypatch.setattr(engine, "convert_file", failing(engine.convert_file, lambda name: True, ConvertError(engine.NO_ROOM)))
    with pytest.raises(ConvertProblem, match="^There is not enough room on the drive for the converted songs.$"):
        run(rig)
    assert header(rig)["status"] == "failed"


@pytest.fixture
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> tuple[float, ...]:
    """The pauses between the tries at a file that Windows has in use (the ripper's: 0.2, 0.5, 1 and 2 seconds), as no time at all."""
    monkeypatch.setattr(ripper, "PATIENCE", (0.0, 0.0, 0.0, 0.0))
    return ripper.PATIENCE


def test_a_slip_in_a_tag_is_a_failed_song_not_a_stopped_batch(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, no_waiting: tuple[float, ...]) -> None:
    song_file(rig, "a.flac", title="A", track_number=1)
    song_file(rig, "b.flac", seed=2, title="B", track_number=2)
    real, tried = job.write_tags, []

    def write(path, fmt, tags, cover):
        tried.append(tags.title)
        if tags.title == "A":
            raise job.TagError("The file is read-only or in use by another program.", busy=True)
        return real(path, fmt, tags, cover)

    monkeypatch.setattr(job, "write_tags", write)
    result, _ = run(rig)
    assert (result["status"], result["converted"], result["failed"]) == ("partial", 1, 1)
    assert header(rig)["items"][0]["message"] == "The file is read-only or in use by another program." and parts_in(rig.out) == []
    assert tried == ["A"] * (len(no_waiting) + 1) + ["B"]  # (a file that stays in use is tried again as many times as the ripper tries, and then it is a failed song)


def test_a_file_that_windows_has_in_use_for_a_moment_is_tagged_and_moved_when_it_lets_go(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, no_waiting: tuple[float, ...]) -> None:
    song_file(rig, "a.flac", title="A", track_number=1)
    real_tags, real_move, refusals = job.write_tags, job.move_into_place, {"tags": 2, "move": 3}

    def write(path, fmt, tags, cover):
        if refusals["tags"]:
            refusals["tags"] -= 1
            raise job.TagError("The file is read-only or in use by another program.", busy=True)
        return real_tags(path, fmt, tags, cover)

    def move(source, target):
        if refusals["move"]:
            refusals["move"] -= 1
            raise PermissionError(errno.EACCES, "The process cannot access the file because it is being used by another process")
        return real_move(source, target)

    monkeypatch.setattr(job, "write_tags", write)
    monkeypatch.setattr(job, "move_into_place", move)
    result, _ = run(rig)
    assert (result["status"], result["converted"], result["failed"]) == ("done", 1, 0) and refusals == {"tags": 0, "move": 0}
    assert read_tags(rig.out / "Aurora Vale/Northern Lights/01 - A.mp3").title == "A" and parts_in(rig.out) == []


def test_a_file_that_turns_up_under_the_name_while_it_is_being_put_there_is_not_replaced(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    song_file(rig, "a.flac", title="A", track_number=1)
    taken = rig.out / "Aurora Vale/Northern Lights/01 - A.mp3"

    def move(source, target):  # (what Windows does when the name was taken a moment ago: it refuses)
        taken.write_bytes(b"arrived at the last moment")
        raise FileExistsError(errno.EEXIST, "Cannot create a file when that file already exists")

    monkeypatch.setattr(job, "move_into_place", move)
    result, _ = run(rig)
    assert taken.read_bytes() == b"arrived at the last moment" and (result["status"], result["converted"], result["failed"]) == ("done", 0, 0)
    assert [(i["status"], i["message"]) for i in header(rig)["items"]] == [("skipped", "Already there: left as it is.")] and parts_in(rig.out) == []


def test_a_half_written_file_that_a_program_held_on_to_is_taken_away_at_the_end(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, no_waiting: tuple[float, ...]) -> None:
    song_file(rig, "a.flac", title="A", track_number=1)
    song_file(rig, "b.flac", seed=2, title="B", track_number=2)
    original_convert = engine.convert_file
    failing_convert = failing(original_convert, lambda name: name == "a.flac", ConvertError("The song could not be converted (boom)."))
    real, held = Path.unlink, []

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name.endswith(store.PART_SUFFIX) and self.exists() and len(held) < 2:  # (a virus scanner has it: the first two tries to take it away are refused)
            held.append(self)
            raise PermissionError(errno.EACCES, "The process cannot access the file because it is being used by another process")
        return real(self, missing_ok=missing_ok)

    def leaving_a_half_file(source, target, info, spec, **kwargs):
        if Path(str(source)).name == "a.flac":
            Path(target).write_bytes(b"half")
        return failing_convert(source, target, info, spec, **kwargs)

    monkeypatch.setattr(engine, "convert_file", leaving_a_half_file)
    monkeypatch.setattr(Path, "unlink", unlink)
    result, _ = run(rig)
    assert held and (result["status"], result["converted"], result["failed"]) == ("partial", 1, 1)
    assert parts_in(rig.out) == []  # (the end of the batch tried once more, and the program had let go)


def only_one_song_fails(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Two songs, in different albums: the one in "Only One / Solo" fails, the other is converted."""
    song_file(rig, "a.flac", title="A", album="Solo", album_artist="Only One", track_number=1)
    song_file(rig, "b.flac", seed=2, title="B", track_number=2)
    monkeypatch.setattr(engine, "convert_file", failing(engine.convert_file, lambda name: name == "a.flac", ConvertError("The song could not be converted (boom).")))
    result, _ = run(rig)
    assert (result["status"], result["converted"], result["failed"]) == ("partial", 1, 1)
    assert [p.name for p in (rig.out / "Aurora Vale/Northern Lights").iterdir()] == ["02 - B.mp3"]
    return result


def test_a_song_that_fails_leaves_none_of_the_folders_it_made(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    only_one_song_fails(rig, monkeypatch)
    assert not (rig.out / "Only One").exists()  # neither "Solo" nor the artist's folder above it is left empty


def test_a_folder_that_was_there_before_stays_when_the_song_for_it_fails(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    mine = rig.out / "Only One" / "Solo"
    mine.mkdir(parents=True)
    only_one_song_fails(rig, monkeypatch)
    assert mine.is_dir() and list(mine.iterdir()) == []  # it was the person's, and empty or not it is not the app's to remove


def test_only_the_folders_a_failed_song_made_go_and_never_one_with_something_in_it(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    (rig.out / "Only One").mkdir(parents=True)
    (rig.out / "Only One" / "notes.txt").write_text("the person's own")
    only_one_song_fails(rig, monkeypatch)
    assert not (rig.out / "Only One" / "Solo").exists()  # the one this song made is gone ...
    assert (rig.out / "Only One" / "notes.txt").read_text() == "the person's own"  # ... the one with a file of the person's in it is not


# ---------------------------------------------------------------------------------------- stopping


def test_a_stop_leaves_the_songs_that_were_done_and_nothing_half_written(rig: SimpleNamespace) -> None:
    for n, title in enumerate(("A", "B", "C"), start=1):
        song_file(rig, f"{title.lower()}.flac", seed=n, title=title, track_number=n)
    handle = Handle(cancel_when=lambda fields: fields.get("message", "").startswith("Converting song 2 of 3"))
    with pytest.raises(JobCancelled):
        run(rig, handle=handle)
    assert [p.name for p in (rig.out / "Aurora Vale/Northern Lights").iterdir()] == ["01 - A.mp3"] and parts_in(rig.out) == [] and not list(rig.scratch.glob("*"))
    saved = header(rig)
    assert (saved["status"], saved["converted"], saved["failed"]) == ("cancelled", 1, 0) and saved["message"] == "Stopped. The songs that were finished stay where they are."
    assert [(i["title"], i["status"]) for i in saved["items"]] == [("A", "done"), ("B", "skipped"), ("C", "skipped")]
    assert saved["items"][1]["message"] == "Stopped before this song was finished." and saved["items"][2]["message"] == "Stopped."


def test_a_stop_that_comes_between_two_songs_begins_nothing_of_the_next(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    for n, title in enumerate(("A", "B"), start=1):
        song_file(rig, f"{title.lower()}.flac", seed=n, title=title, track_number=n)
    handle = Handle()
    real = store.set_item

    def set_item(conn: sqlite3.Connection, item_id: int, **fields: Any) -> None:
        real(conn, item_id, **fields)
        if fields.get("status") == "done":
            handle.cancelled = True  # (the first song has just been finished: the stop comes between it and the next)

    monkeypatch.setattr(store, "set_item", set_item)
    with pytest.raises(JobCancelled):
        run(rig, handle=handle)
    saved = header(rig)
    assert [(i["title"], i["status"]) for i in saved["items"]] == [("A", "done"), ("B", "skipped")] and saved["items"][1]["message"] == "Stopped."
    assert not any("song 2" in str(update.get("message")) for update in handle.updates)  # (the next song was not so much as announced)
    assert [p.name for p in (rig.out / "Aurora Vale/Northern Lights").iterdir()] == ["01 - A.mp3"]


def test_a_stop_leaves_no_empty_folder_of_the_song_that_was_stopped(rig: SimpleNamespace) -> None:
    song_file(rig, "a.flac", title="A", track_number=1)
    song_file(rig, "b.flac", seed=2, title="B", album="Solo", album_artist="Only One", track_number=1)
    handle = Handle(cancel_when=lambda fields: fields.get("message", "").startswith("Converting song 2 of 2"))
    with pytest.raises(JobCancelled):
        run(rig, handle=handle)
    assert [p.name for p in (rig.out / "Aurora Vale/Northern Lights").iterdir()] == ["01 - A.mp3"]
    assert not (rig.out / "Only One").exists()


def test_a_stop_before_the_first_song_writes_nothing(rig: SimpleNamespace) -> None:
    song_file(rig, "a.flac", title="A")
    handle = Handle()
    handle.cancelled = True
    with pytest.raises(JobCancelled):
        run(rig, handle=handle)
    assert not rig.out.exists() or list(rig.out.rglob("*.mp3")) == []


def test_a_stop_while_the_songs_are_being_looked_at_writes_nothing_and_leaves_no_history(rig: SimpleNamespace) -> None:
    song_file(rig, "a.flac", title="A")
    handle = Handle(cancel_when=lambda fields: "Looking at the songs again:" in fields.get("message", ""))
    with pytest.raises(JobCancelled):
        run(rig, handle=handle)
    with rig.db() as conn:
        assert store.list_conversions(conn) == []
    assert not rig.out.exists()


# ---------------------------------------------------------------------------------------- songs only Windows decodes


def test_a_song_only_windows_decodes_is_converted_from_what_windows_made_of_it(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    stand_in_windows(rig.tmp, monkeypatch)
    rig.library.windows_song("aac.m4a", 14, 400, title="Aac", track_number=1)
    song_file(rig, "plain.flac", title="Plain", seed=2, track_number=2)
    rig.library.windows_song("wma.wma", 15, 380, kind=b"FAKEWMA1", title="Wma", track_number=3)
    result, handle = run(rig, Spec("flac"))
    assert (result["status"], result["converted"], result["copied"], result["failed"]) == ("done", 2, 1, 0)  # (the plain FLAC is already as asked for: copied)
    for name, seed, sectors in (("01 - Aac", 14, 400), ("03 - Wma", 15, 380)):
        samples = audio.read(rig.out / "Aurora Vale/Northern Lights" / f"{name}.flac", "int16")[0]
        assert np.array_equal(samples.reshape(-1).tobytes(), fake_audio(seed, 0, sectors))  # (the very samples Windows made of it)
    assert not list(rig.scratch.glob("*"))
    percents = [int(m.group(1)) for u in handle.updates if (m := __import__("re").search(r"Converting song 1 of 3: Aac \((\d+)%\)", u.get("message", "")))]
    assert percents == sorted(percents) and percents[-1] == 100 and any(p <= 50 for p in percents) and any(p > 50 for p in percents)  # (decoding, then converting)


def test_one_decoded_copy_at_a_time_and_none_left_behind(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    stand_in_windows(rig.tmp, monkeypatch)
    for n in range(1, 4):
        rig.library.windows_song(f"{n}.m4a", 10 + n, 300, title=f"S{n}", track_number=n)
    seen: list[list[str]] = []
    real = windows.decode

    def spy(helper, source, target, on_event=None):
        seen.append(sorted(p.name for p in rig.scratch.glob("*")))
        return real(helper, source, target, on_event)

    monkeypatch.setattr(windows, "decode", spy)
    result, _ = run(rig, Spec("wav"))
    assert result["converted"] == 3 and seen == [[], [], []] and not list(rig.scratch.glob("*"))  # (each copy is gone before the next song is decoded)


def test_a_song_windows_cannot_decode_after_all_is_a_failed_song_and_the_rest_go_on(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    stand_in_windows(rig.tmp, monkeypatch)
    rig.library.windows_song("a.m4a", 14, 300, title="A", track_number=1)
    rig.library.windows_song("b.m4a", 15, 300, title="B", track_number=2)
    real = windows.decode

    def decode(helper, source, target, on_event=None):
        if source.endswith("a.m4a"):
            raise DiscError("failed", "Windows could not decode A after all.")
        return real(helper, source, target, on_event)

    monkeypatch.setattr(windows, "decode", decode)
    result, _ = run(rig, Spec("flac"))
    assert (result["status"], result["converted"], result["failed"]) == ("partial", 1, 1)
    assert header(rig)["items"][0]["message"] == "Windows could not decode A after all." and not list(rig.scratch.glob("*"))


def test_a_decoded_file_that_cannot_be_read_is_a_failed_song_and_the_others_go_on(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    stand_in_windows(rig.tmp, monkeypatch)
    rig.library.windows_song("a.m4a", 14, 300, title="A", track_number=1)
    song_file(rig, "b.flac", seed=2, title="B", track_number=2)
    real = job.inspect_source

    def inspect(path: Any) -> Any:
        if Path(str(path)).parent == rig.scratch:  # (what Windows made of the song)
            raise SourceError("The song cannot be read (boom).")
        return real(path)

    monkeypatch.setattr(job, "inspect_source", inspect)
    result, _ = run(rig)
    assert (result["status"], result["converted"], result["failed"]) == ("partial", 1, 1)
    assert [(i["title"], i["status"], i["message"]) for i in header(rig)["items"]] == [("A", "failed", "The song cannot be read (boom)."), ("B", "done", None)]
    assert not list(rig.scratch.glob("*")) and parts_in(rig.out) == []


def test_a_windows_that_goes_away_after_the_preview_fails_its_songs_in_words_and_the_others_go_on(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    stand_in_windows(rig.tmp, monkeypatch)
    rig.library.windows_song("a.m4a", 14, 300, title="A", track_number=1)
    song_file(rig, "b.flac", seed=2, title="B", track_number=2)

    class Gone:
        def __enter__(self) -> Self:
            raise DiscUnavailable("The disc helper is only in the Windows app.")

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(job, "DiscHelper", Gone)  # (the plan asked Windows through the real stand-in; the job's own helper is the one that is gone)
    result, _ = run(rig)
    assert (result["status"], result["converted"], result["failed"]) == ("partial", 1, 1)
    assert [(i["title"], i["status"], i["message"]) for i in header(rig)["items"]] == [("A", "failed", "The disc helper is only in the Windows app."), ("B", "done", None)]


def test_a_decoded_copy_that_could_not_be_removed_at_once_is_removed_at_the_end(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    stand_in_windows(rig.tmp, monkeypatch)
    rig.library.windows_song("a.m4a", 14, 300, title="A", track_number=1)
    real, refused = Path.unlink, []

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.parent == rig.scratch and not refused:  # (the first time: something still has the file open)
            refused.append(self)
            raise PermissionError(errno.EACCES, "The process cannot access the file because it is being used by another process")
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    result, _ = run(rig)
    assert refused and result["converted"] == 1 and not list(rig.scratch.glob("*"))


def test_a_stop_while_windows_decodes_a_song_stops_the_conversion_and_leaves_nothing(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    stand_in_windows(rig.tmp, monkeypatch, decode_slow=0.3)
    rig.library.windows_song("a.m4a", 14, 400, title="A", track_number=1)
    rig.library.windows_song("b.m4a", 15, 400, title="B", track_number=2)
    handle = Handle(cancel_when=lambda fields: "Converting song 1 of 2: A (" in fields.get("message", ""))  # (once it has begun to decode)
    with pytest.raises(JobCancelled):
        run(rig, Spec("flac"), handle=handle)
    assert not rig.out.exists() or list(rig.out.rglob("*.flac")) == []
    assert parts_in(rig.out) == [] and not list(rig.scratch.glob("*"))
    assert header(rig)["status"] == "cancelled" and [i["status"] for i in header(rig)["items"]] == ["skipped", "skipped"]


def test_a_windows_that_is_not_there_leaves_its_songs_out_in_words(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MTK_DISC_HELPER", "[]")
    song_file(rig, "plain.flac", title="Plain")
    rig.library.windows_song("a.m4a", 14, 300, title="A", track_number=2)
    result, _ = run(rig)
    assert (result["converted"], result["left_out"]) == (1, 1)


# ---------------------------------------------------------------------------------------- taking a conversion back


def test_a_conversion_taken_back_removes_what_it_made_and_leaves_what_was_changed(rig: SimpleNamespace) -> None:
    for n, title in enumerate(("A", "B", "C"), start=1):
        song_file(rig, f"{title.lower()}.flac", seed=n, title=title, track_number=n)
    run(rig)
    album = rig.out / "Aurora Vale/Northern Lights"
    (album / "02 - B.mp3").write_bytes(b"the person edited this")  # (a different size from what was written)
    (album / "03 - C.mp3").unlink()
    (album / "notes.txt").write_text("the person's own file in the folder")
    result = job.undo_conversion(db=rig.db, conversion_id=1, handle=Handle())
    assert result == {"conversion_id": 1, "removed": 1, "changed": 1, "gone": 1, "failed": 0}
    assert sorted(p.name for p in album.iterdir()) == ["02 - B.mp3", "notes.txt"] and (album / "02 - B.mp3").read_bytes() == b"the person edited this"
    assert header(rig)["undone_at"]
    with pytest.raises(ConvertProblem, match="taken back already"):
        job.undo_conversion(db=rig.db, conversion_id=1, handle=Handle())


def test_taking_a_conversion_back_removes_the_folders_it_emptied_but_not_the_one_it_was_given(rig: SimpleNamespace) -> None:
    song_file(rig, "a.flac", title="A", album="First", track_number=1)
    song_file(rig, "b.flac", seed=2, title="B", album="Second", album_artist="Other", track_number=1)
    run(rig)
    (rig.out / "Aurora Vale" / "First").mkdir(exist_ok=True)
    result = job.undo_conversion(db=rig.db, conversion_id=1, handle=Handle())
    assert result["removed"] == 2 and rig.out.is_dir() and list(rig.out.iterdir()) == [rig.out / "Aurora Vale"] and list((rig.out / "Aurora Vale").iterdir()) == [rig.out / "Aurora Vale" / "First"] or not (rig.out / "Other").exists()
    assert not (rig.out / "Other").exists() and not (rig.out / "Aurora Vale" / "Second").exists()


def test_a_file_whose_time_is_a_little_different_is_still_the_one_that_was_written(rig: SimpleNamespace) -> None:
    song_file(rig, "a.flac", title="A")
    run(rig)
    target = rig.out / "Aurora Vale/Northern Lights/01 - A.mp3"
    made = target.stat()
    os.utime(target, (made.st_atime, made.st_mtime + 1.9))  # (a FAT drive keeps the time to the nearest two seconds)
    assert job.undo_conversion(db=rig.db, conversion_id=1, handle=Handle())["removed"] == 1


def test_a_file_whose_time_is_exactly_as_far_off_as_is_allowed_is_still_the_one_that_was_written_and_a_little_further_is_not(rig: SimpleNamespace) -> None:
    for n, title in enumerate(("A", "B"), start=1):
        song_file(rig, f"{title.lower()}.flac", seed=n, title=title, track_number=n)
    run(rig)
    album = rig.out / "Aurora Vale/Northern Lights"
    recorded = 1_700_000_000.0  # (a time that can be said exactly, so that the distance between two of them can be too)
    with rig.db() as conn:
        for item in header(rig)["items"]:
            store.set_item(conn, item["id"], status="done", size=(album / Path(item["target"]).name).stat().st_size, mtime=recorded)
    os.utime(album / "01 - A.mp3", (recorded + job.MTIME_SLACK, recorded + job.MTIME_SLACK))
    os.utime(album / "02 - B.mp3", (recorded - job.MTIME_SLACK - 0.25, recorded - job.MTIME_SLACK - 0.25))
    result = job.undo_conversion(db=rig.db, conversion_id=1, handle=Handle())
    assert (result["removed"], result["changed"]) == (1, 1) and [p.name for p in album.iterdir()] == ["02 - B.mp3"]


def test_a_file_that_cannot_be_looked_at_or_deleted_is_counted_as_it_and_left_where_it_is(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    for n, title in enumerate(("A", "B", "C"), start=1):
        song_file(rig, f"{title.lower()}.flac", seed=n, title=title, track_number=n)
    run(rig)
    album = rig.out / "Aurora Vale/Northern Lights"
    real_stat, real_unlink = os.stat, Path.unlink

    def stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if str(path) == str(album / "01 - A.mp3"):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_stat(path, *args, **kwargs)

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == album / "02 - B.mp3":
            raise PermissionError(errno.EACCES, "The process cannot access the file because it is being used by another process")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(job.os, "stat", stat)
    monkeypatch.setattr(Path, "unlink", unlink)
    result = job.undo_conversion(db=rig.db, conversion_id=1, handle=Handle())
    assert result == {"conversion_id": 1, "removed": 1, "changed": 0, "gone": 0, "failed": 2}
    assert sorted(p.name for p in album.iterdir()) == ["01 - A.mp3", "02 - B.mp3"]


def test_a_file_that_was_touched_a_while_after_is_the_persons_now(rig: SimpleNamespace) -> None:
    song_file(rig, "a.flac", title="A")
    run(rig)
    target = rig.out / "Aurora Vale/Northern Lights/01 - A.mp3"
    made = target.stat()
    os.utime(target, (made.st_atime, made.st_mtime + 10))
    assert job.undo_conversion(db=rig.db, conversion_id=1, handle=Handle())["changed"] == 1 and target.exists()


def test_a_conversion_that_is_not_there_cannot_be_taken_back(rig: SimpleNamespace) -> None:
    with pytest.raises(ConvertProblem, match="not in the history"):
        job.undo_conversion(db=rig.db, conversion_id=99, handle=Handle())


def test_taking_back_can_be_stopped_and_says_how_far_it_got(rig: SimpleNamespace) -> None:
    for n, title in enumerate(("A", "B", "C"), start=1):
        song_file(rig, f"{title.lower()}.flac", seed=n, title=title, track_number=n)
    run(rig)
    handle = Handle(cancel_when=lambda fields: fields.get("message") == "Taking back song 2 of 3")
    with pytest.raises(JobCancelled):
        job.undo_conversion(db=rig.db, conversion_id=1, handle=handle)
    # (the stop is noticed before the next song: the one it was on is done)
    assert sorted(p.name for p in (rig.out / "Aurora Vale/Northern Lights").iterdir()) == ["03 - C.mp3"] and header(rig)["undone_at"] is None
    assert job.undo_conversion(db=rig.db, conversion_id=1, handle=Handle())["removed"] == 1 and not (rig.out / "Aurora Vale").exists()  # (and it can be finished later)


# ---------------------------------------------------------------------------------------- the history, and a conversion the app was closed in the middle of


def make_history(conn: sqlite3.Connection, batch: str, items: list[dict[str, Any]], started: str | None = None) -> int:
    conversion_id, ids = store.create_conversion(conn, batch_id=batch, title="x", fmt="mp3", quality="256", destination="/d", scheme=SCHEME, options={"a": 1}, left_out=2, items=items)
    if started:
        conn.execute("UPDATE conversions SET started_at = ? WHERE id = ?", (started, conversion_id))
        conn.commit()
    for item_id, item in zip(ids, items):
        if item.get("status"):
            store.set_item(conn, item_id, status=item["status"])
    return conversion_id


def test_the_history_lists_the_newest_first_and_pages_through_the_songs(rig: SimpleNamespace) -> None:
    items = [{"track_id": n, "title": f"S{n}", "source": f"/s/{n}.flac", "target": f"/d/{n}.mp3", "action": "convert"} for n in range(1, 8)]
    with rig.db() as conn:
        first = make_history(conn, "old", items[:2], started="2020-01-01T00:00:00+00:00")
        second = make_history(conn, "new", items, started="2021-01-01T00:00:00+00:00")
        assert [c["batch_id"] for c in store.list_conversions(conn)] == ["new", "old"] and [c["id"] for c in store.list_conversions(conn, limit=1)] == [second]
        page = store.get_conversion(conn, second, offset=2, limit=3)
        assert (page["items_total"], [i["title"] for i in page["items"]], page["options"], page["left_out"], page["songs"]) == (7, ["S3", "S4", "S5"], {"a": 1}, 2, 7)
        assert [i["title"] for i in store.get_conversion(conn, second, offset=-4, limit=10_000)["items"]] == [f"S{n}" for n in range(1, 8)]
        assert store.get_conversion(conn, 999) is None and first
        with pytest.raises(ValueError, match="unknown columns"):
            store.set_item(conn, 1, nonsense=1)


def test_a_page_of_songs_is_never_more_than_five_hundred_or_less_than_one(rig: SimpleNamespace) -> None:
    items = [{"track_id": n, "title": f"S{n:03d}", "source": f"/s/{n}.flac", "target": f"/d/{n}.mp3", "action": "convert"} for n in range(600)]
    with rig.db() as conn:
        conversion_id = make_history(conn, "many", items)
        assert [len(store.get_conversion(conn, conversion_id, limit=n)["items"]) for n in (10_000, 500, 100, 0, -1)] == [500, 500, 100, 1, 1]  # (to SQL a limit of -1 is "no limit")
        last = store.get_conversion(conn, conversion_id, offset=550, limit=100)
        assert last["items_total"] == 600 and [i["title"] for i in last["items"]][:2] == ["S550", "S551"] and len(last["items"]) == 50


def test_conversions_that_began_in_the_same_second_are_listed_the_later_first(rig: SimpleNamespace) -> None:
    item = [{"track_id": 1, "title": "S", "source": "/s/1.flac", "target": "/d/1.mp3", "action": "convert"}]
    with rig.db() as conn:
        earlier = make_history(conn, "a", item, started="2021-01-01T00:00:00+00:00")
        later = make_history(conn, "b", item, started="2021-01-01T00:00:00+00:00")
        assert [c["id"] for c in store.list_conversions(conn)] == [later, earlier]


def test_a_conversion_counts_the_bytes_of_the_songs_it_made_and_no_others(rig: SimpleNamespace) -> None:
    items = [{"track_id": n, "title": f"S{n}", "source": f"/s/{n}.flac", "target": f"/d/{n}.mp3", "action": "convert"} for n in range(3)]
    with rig.db() as conn:
        conversion_id, ids = store.create_conversion(conn, batch_id="b", title="x", fmt="mp3", quality="256", destination="/d", scheme=SCHEME, options={}, left_out=0, items=items)
        store.set_item(conn, ids[0], status="done", size=1000, mtime=1.0)
        store.set_item(conn, ids[1], status="failed", size=500)
        store.set_item(conn, ids[2], status="skipped", size=300)
        store.finish_conversion(conn, conversion_id, "partial")
        assert store.get_conversion(conn, conversion_id)["bytes"] == 1000


def test_a_conversion_the_app_was_closed_in_the_middle_of_is_marked_and_its_half_files_removed(rig: SimpleNamespace) -> None:
    rig.out.mkdir()
    half, other = rig.out / "b.mp3.part", rig.out / "c.mp3.part"
    for path in (half, other, rig.out / "a.mp3"):
        path.write_bytes(b"x")
    rig.scratch.mkdir()
    (rig.scratch / "batch7-00002.wav").write_bytes(b"x")
    (rig.scratch / "someone-elses.wav").write_bytes(b"x")
    items = [{"track_id": n, "title": t, "source": f"/s/{t}", "target": str(rig.out / f"{t}.mp3"), "action": "convert"} for n, t in enumerate("abc", start=1)]
    with rig.db() as conn:
        conversion_id = make_history(conn, "batch7", [dict(items[0], status="done"), dict(items[1], status="working"), items[2]])
        finished = make_history(conn, "batch8", [dict(items[0], status="done")])
        store.finish_conversion(conn, finished, "done")
        assert store.recover(conn, rig.scratch) == 1
        saved = store.get_conversion(conn, conversion_id)
        assert (saved["status"], saved["message"], saved["converted"], saved["failed"]) == ("interrupted", "The app was closed before the conversion was finished.", 1, 2)
        assert [i["status"] for i in saved["items"]] == ["done", "failed", "failed"] and saved["items"][1]["message"] == store.LEFT_UNDONE
        assert store.get_conversion(conn, finished)["status"] == "done" and store.recover(conn, rig.scratch) == 0
    assert not half.exists() and not other.exists() and (rig.out / "a.mp3").exists()
    assert [p.name for p in rig.scratch.iterdir()] == ["someone-elses.wav"]


def test_a_batch_of_nothing_still_finishes_with_counts_of_nothing(rig: SimpleNamespace) -> None:
    with rig.db() as conn:
        conversion_id, ids = store.create_conversion(conn, batch_id="empty", title="none", fmt="flac", quality="standard", destination="/d", scheme=SCHEME, options={}, left_out=0, items=[])
        store.finish_conversion(conn, conversion_id, "done")
        saved = store.get_conversion(conn, conversion_id)
    assert ids == [] and (saved["converted"], saved["copied"], saved["failed"], saved["bytes"], saved["items"]) == (0, 0, 0, 0, [])
