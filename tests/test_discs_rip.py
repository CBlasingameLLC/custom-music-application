"""Planning a rip (where every song would go, and whether it can start), the history, and what is done after a rip was interrupted."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from musictoolkit.db.connection import connect
from musictoolkit.discs import rip, store
from musictoolkit.discs.reader import ReadStats
from musictoolkit.discs.rip import RipRequest, TrackInput
from musictoolkit.discs.toc import Toc, Track

DEFAULT_SCHEME = "{album_artist}/{album}/{track:02d} - {title}.{ext}"


def toc3() -> Toc:
    return Toc((Track(1, 0), Track(2, 3000), Track(3, 7000)), 10500)  # 40 s, 53.3 s and 46.7 s


def request(root: Path, **changes) -> RipRequest:
    base = dict(
        drive="E:", root=str(root), scheme=DEFAULT_SCHEME, format="flac", quality="standard", accuracy="verified", album="Test Album", album_artist="Test Artist",
        tracks=[TrackInput(1, "First"), TrackInput(2, "Second"), TrackInput(3, "Third")], year=2001, disc_number=1, disc_total=1,
    )
    return RipRequest(**{**base, **changes})


@pytest.fixture
def music(tmp_path: Path) -> Path:
    folder = tmp_path / "music"
    folder.mkdir()
    return folder


def plan(music: Path, tmp_path: Path, **changes) -> rip.RipPlan:
    return rip.plan_rip(toc3(), request(music, **changes), [str(music)], tmp_path / "cache")


def names(p: rip.RipPlan) -> list[str | None]:
    return [t.path.relative_to(p.root).as_posix() if t.path else None for t in p.tracks]


# ---------------------------------------------------------------------------------------- where the songs go


def test_every_song_gets_its_place_under_the_chosen_library_folder(music: Path, tmp_path: Path) -> None:
    p = plan(music, tmp_path)
    assert names(p) == ["Test Artist/Test Album/01 - First.flac", "Test Artist/Test Album/02 - Second.flac", "Test Artist/Test Album/03 - Third.flac"]
    assert [t.action for t in p.tracks] == ["rip"] * 3 and p.problems == [] and p.folder == music / "Test Artist" / "Test Album"
    assert [(t.start, t.sectors) for t in p.tracks] == [(0, 3000), (3000, 4000), (7000, 3500)]
    assert not any(music.rglob("*")), "planning touches nothing"


def test_a_song_the_person_left_out_is_not_ripped(music: Path, tmp_path: Path) -> None:
    p = plan(music, tmp_path, tracks=[TrackInput(1, "First"), TrackInput(2, "Second", rip=False), TrackInput(3, "Third")])
    assert [t.action for t in p.tracks] == ["rip", "skip", "rip"] and p.tracks[1].path is None and [t.number for t in p.to_rip] == [1, 3]


def test_a_file_that_is_already_there_is_left_alone_not_replaced(music: Path, tmp_path: Path) -> None:
    taken = music / "Test Artist" / "Test Album" / "02 - Second.flac"
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"mine")
    p = plan(music, tmp_path)
    assert [t.action for t in p.tracks] == ["rip", "exists", "rip"] and "already there" in p.tracks[1].note
    assert taken.read_bytes() == b"mine"


def test_two_songs_that_would_get_one_name_are_not_both_ripped(music: Path, tmp_path: Path) -> None:
    p = plan(music, tmp_path, scheme="{album_artist}/{album}/{title}.{ext}", tracks=[TrackInput(1, "Intro"), TrackInput(2, "Intro"), TrackInput(3, "Outro")])
    assert [t.action for t in p.tracks] == ["rip", "clash", "rip"] and "same name" in p.tracks[1].note


def test_names_are_made_usable_on_windows(music: Path, tmp_path: Path) -> None:
    p = plan(music, tmp_path, scheme="{album_artist}/{album}/{title}.{ext}", album='AC/DC: "Back" in Black?', album_artist="AC/DC",
             tracks=[TrackInput(1, "CON"), TrackInput(2, "Hells Bells."), TrackInput(3, "What?")])
    assert names(p) == ["AC_DC/AC_DC_ _Back_ in Black_/_CON.flac", "AC_DC/AC_DC_ _Back_ in Black_/Hells Bells.flac", "AC_DC/AC_DC_ _Back_ in Black_/What_.flac"]


def test_a_song_with_no_title_is_called_by_its_number(music: Path, tmp_path: Path) -> None:
    p = plan(music, tmp_path, tracks=[TrackInput(1, ""), TrackInput(2, "  "), TrackInput(3, "Third")])
    assert names(p)[:2] == ["Test Artist/Test Album/01 - Track 01.flac", "Test Artist/Test Album/02 - Track 02.flac"]


def test_the_disc_number_can_be_part_of_the_name(music: Path, tmp_path: Path) -> None:
    p = plan(music, tmp_path, scheme="{album_artist}/{album}/{disc}-{track:02d} - {title}.{ext}", disc_number=2, disc_total=2)
    assert names(p)[0] == "Test Artist/Test Album/2-01 - First.flac"


def test_each_song_can_have_its_own_artist_and_an_album_by_various_artists(music: Path, tmp_path: Path) -> None:
    p = plan(music, tmp_path, scheme="{album_artist}/{album}/{track:02d} - {artist} - {title}.{ext}", album_artist="Various Artists",
             tracks=[TrackInput(1, "One", artist="Band A"), TrackInput(2, "Two", artist="Band B"), TrackInput(3, "Three")])
    assert names(p) == [
        "Various Artists/Test Album/01 - Band A - One.flac", "Various Artists/Test Album/02 - Band B - Two.flac",
        "Various Artists/Test Album/03 - Various Artists - Three.flac",
    ]


def test_a_name_too_long_for_windows_is_not_attempted(music: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rip, "path_too_long", lambda path: "Third" in path.name)
    p = plan(music, tmp_path)
    assert [t.action for t in p.tracks] == ["rip", "rip", "too_long"] and "too long for Windows" in p.tracks[2].note


# ---------------------------------------------------------------------------------------- whether it can start


def test_the_estimate_follows_the_format_and_the_length(music: Path, tmp_path: Path) -> None:
    seconds = (3000 + 4000 + 3500) / 75
    flac = plan(music, tmp_path)
    mp3 = plan(music, tmp_path, format="mp3", quality="320")
    assert flac.estimated_bytes == pytest.approx(seconds * 44100 * 4 * 0.6, rel=0.001)
    assert mp3.estimated_bytes == pytest.approx(seconds * 320 * 1000 / 8, rel=0.001)
    assert names(mp3)[0].endswith("01 - First.mp3")


def test_too_little_room_for_the_songs_stops_the_rip_before_it_starts(music: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rip, "_free", lambda folder: 1_000_000)
    p = plan(music, tmp_path)
    assert any("not enough room for these songs" in problem for problem in p.problems)
    assert any("temporary copy" in problem for problem in p.problems)


def test_nothing_chosen_or_everything_already_there_is_nothing_to_rip(music: Path, tmp_path: Path) -> None:
    p = plan(music, tmp_path, tracks=[TrackInput(n, f"T{n}", rip=False) for n in (1, 2, 3)])
    assert p.problems == ["There is nothing to rip: every song is left out or already there."]


def test_a_library_folder_that_is_not_there_is_a_problem(tmp_path: Path) -> None:
    gone = tmp_path / "unplugged"
    p = rip.plan_rip(toc3(), request(gone), [str(gone)], tmp_path / "cache")
    assert any("not available" in problem for problem in p.problems)


@pytest.mark.parametrize("changes, message", [
    (dict(tracks=[TrackInput(1, "A"), TrackInput(2, "B")]), "does not match the disc"),
    (dict(tracks=[TrackInput(1, "A"), TrackInput(2, "B"), TrackInput(4, "D")]), "does not match the disc"),
    (dict(scheme="{nonsense}.{ext}"), "Invalid template"),
    (dict(scheme="../{title}.{ext}"), "relative path"),
    (dict(format="wav"), "format must be"),
    (dict(format="mp3", quality="standard"), "quality for mp3"),
    (dict(accuracy="paranoid"), "accuracy must be"),
])
def test_a_request_that_makes_no_sense_is_refused_with_words_for_the_person(music: Path, tmp_path: Path, changes: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        plan(music, tmp_path, **changes)


def test_a_folder_outside_the_library_is_refused(music: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not in your library"):
        rip.plan_rip(toc3(), request(tmp_path / "elsewhere"), [str(music)], tmp_path / "cache")


def test_a_disc_with_no_music_is_refused(music: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no music"):
        rip.plan_rip(Toc((Track(1, 0, audio=False),), 5000), request(music), [str(music)], tmp_path / "cache")


def test_the_plan_for_the_screen_has_short_paths_and_counts(music: Path, tmp_path: Path) -> None:
    shown = plan(music, tmp_path, tracks=[TrackInput(1, "First"), TrackInput(2, "Second", rip=False), TrackInput(3, "Third")]).to_dict()
    assert shown["ok"] is True and shown["folder"] == "Test Artist/Test Album" and shown["counts"]["rip"] == 2 and shown["counts"]["skip"] == 1
    assert shown["tracks"][0]["path"] == "Test Artist/Test Album/01 - First.flac" and shown["tracks"][1]["path"] is None
    assert shown["tracks"][0]["seconds"] == 40.0


# ---------------------------------------------------------------------------------------- what is said about the songs


def test_what_happened_to_a_song_is_said_in_plain_words() -> None:
    assert rip._describe(ReadStats(sectors=100), 0) is None
    assert rip._describe(ReadStats(sectors=100, rereads=1), 0) == "1 part had to be read again."
    stats = ReadStats(sectors=5000, rereads=3, suspect=[75 * 12 + 16], unreadable=[75 * 61, 75 * 100])
    assert rip._describe(stats, 0) == (
        "3 parts had to be read again; 1 part could not be confirmed (around 0:12); 2 parts could not be read and are silent (around 1:01, 1:40)."
    )


# ---------------------------------------------------------------------------------------- the history, and an interrupted rip


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "library.db")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def make_rip(conn, tmp_path: Path, batch: str = "abc") -> int:
    return store.create_rip(
        conn, batch_id=batch, disc_id="X", release_id=None, album="Album", artist="Artist", drive="E:", fmt="flac", quality="standard", accuracy="verified",
        root=str(tmp_path), folder=str(tmp_path / "Artist"),
        tracks=[{"number": 1, "title": "One", "file_path": str(tmp_path / "one.flac")}, {"number": 2, "title": "Two", "file_path": str(tmp_path / "two.flac")}],
    )


def test_a_rip_is_in_the_history_with_every_song(conn, tmp_path: Path) -> None:
    rip_id = make_rip(conn, tmp_path)
    store.set_track(conn, rip_id, 1, status="done", size=10, mtime=1.5, rereads=1)
    store.finish_rip(conn, rip_id, "partial", "Stopped.")
    found = store.get_rip(conn, rip_id)
    assert (found["album"], found["status"], found["message"], found["undone_at"]) == ("Album", "partial", "Stopped.", None)
    assert [(t["number"], t["status"], t["size"], t["rereads"]) for t in found["tracks"]] == [(1, "done", 10, 1), (2, "waiting", None, 0)]
    listed = store.list_rips(conn)
    assert len(listed) == 1 and listed[0]["songs"] == 1 and listed[0]["tracks"] == 2
    with pytest.raises(ValueError):
        store.set_track(conn, rip_id, 1, nonsense=1)


def test_the_problems_of_a_rip_are_added_up_when_it_finishes(conn, tmp_path: Path) -> None:
    rip_id = make_rip(conn, tmp_path)
    store.set_track(conn, rip_id, 1, suspect=2, unreadable=1)
    store.set_track(conn, rip_id, 2, suspect=1)
    store.finish_rip(conn, rip_id, "done")
    assert store.get_rip(conn, rip_id)["problems"] == 4


def test_a_rip_that_was_running_when_the_app_stopped_is_marked_and_its_leftovers_removed(conn, tmp_path: Path) -> None:
    rip_id = make_rip(conn, tmp_path, "run1")
    done = tmp_path / "one.flac"
    done.write_bytes(b"finished song")
    store.set_track(conn, rip_id, 1, status="done", size=13, mtime=1.0)
    half = tmp_path / "two.flac.part"
    half.write_bytes(b"half a song")
    store.set_track(conn, rip_id, 2, status="ripping")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "run1-02.pcm").write_bytes(b"raw")
    (cache / "other-01.pcm").write_bytes(b"someone else's")
    finished = make_rip(conn, tmp_path, "fin1")
    store.finish_rip(conn, finished, "done")

    assert store.recover(conn, cache) == 1
    found = store.get_rip(conn, rip_id)
    assert found["status"] == "interrupted" and "closed" in found["message"] and found["finished_at"]
    assert [t["status"] for t in found["tracks"]] == ["done", "failed"]
    assert done.exists() and not half.exists() and not (cache / "run1-02.pcm").exists() and (cache / "other-01.pcm").exists()
    assert store.get_rip(conn, finished)["status"] == "done"
    assert store.recover(conn, cache) == 0, "once is enough"


@pytest.mark.parametrize("error", [KeyError("genre"), ValueError("not a path"), IndexError("no such field")], ids=["KeyError", "ValueError", "IndexError"])
def test_a_layout_that_cannot_name_one_song_leaves_that_song_out_and_plans_the_rest(
    music: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    real = rip.organizer.render_relative

    def render(row, scheme, *args, **kwargs):
        if row["title"] == "Second":
            raise error
        return real(row, scheme, *args, **kwargs)

    monkeypatch.setattr(rip.organizer, "render_relative", render)
    p = plan(music, tmp_path)
    assert [t.action for t in p.tracks] == ["rip", "problem", "rip"] and p.tracks[1].path is None
    assert "layout cannot name this song" in p.tracks[1].note and p.problems == []


def test_a_song_with_trouble_still_counts_as_a_song_in_the_history(conn, tmp_path: Path) -> None:
    rip_id = make_rip(conn, tmp_path)
    store.set_track(conn, rip_id, 1, status="problem", suspect=1)
    store.set_track(conn, rip_id, 2, status="done")
    assert store.list_rips(conn)[0]["songs"] == 2


def test_the_history_lists_the_newest_rip_first(conn, tmp_path: Path) -> None:
    first, second, third = (make_rip(conn, tmp_path, batch) for batch in ("a", "b", "c"))
    conn.execute("UPDATE disc_rips SET started_at = ? WHERE id = ?", ("2020-01-01T00:00:00+00:00", third))
    conn.commit()
    assert [r["id"] for r in store.list_rips(conn)] == [second, first, third]
    assert [r["id"] for r in store.list_rips(conn, limit=1)] == [second]


def test_a_rip_cannot_be_taken_back_twice_or_when_it_is_not_known(conn, tmp_path: Path) -> None:
    rip_id = make_rip(conn, tmp_path)
    store.finish_rip(conn, rip_id, "done")
    assert rip.undo_rip(conn, rip_id, [tmp_path], trash=lambda path: None)["trashed"] == 0
    with pytest.raises(ValueError, match="already been taken back"):
        rip.undo_rip(conn, rip_id, [tmp_path], trash=lambda path: None)
    with pytest.raises(ValueError, match="no such rip"):
        rip.undo_rip(conn, 999, [tmp_path], trash=lambda path: None)


def test_on_windows_a_song_is_found_in_the_library_whatever_the_case_of_its_path(conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Windows:  # the same code as it runs there, where one file can be spelled in several ways
        name = "nt"

        def __getattr__(self, attribute: str):
            return getattr(os, attribute)

    monkeypatch.setattr(rip, "os", Windows())
    rip_id = make_rip(conn, tmp_path)
    song = tmp_path / "one.flac"
    song.write_bytes(b"x")
    store.set_track(conn, rip_id, 1, status="done", size=1, mtime=song.stat().st_mtime)
    conn.execute("INSERT INTO tracks (file_path) VALUES (?)", (str(song).upper(),))
    conn.commit()
    result = rip.undo_rip(conn, rip_id, [tmp_path], trash=lambda path: Path(path).unlink())
    assert result["trashed"] == 1 and not song.exists()
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0, "the library forgot it too"


def test_a_song_is_moved_into_place_in_the_way_each_system_does_not_overwrite(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Path, Path]] = []

    class System:
        def __init__(self, name: str) -> None:
            self.name = name

        def rename(self, source, target) -> None:
            calls.append(("rename", source, target))

        def replace(self, source, target) -> None:
            calls.append(("replace", source, target))

        def __getattr__(self, attribute: str):
            return getattr(os, attribute)

    for name, expected in (("nt", "rename"), ("posix", "replace")):
        calls.clear()
        monkeypatch.setattr(rip, "os", System(name))
        rip._move_into_place(Path("a.part"), Path("a"))
        assert calls == [(expected, Path("a.part"), Path("a"))], f"on {name}"
