"""A library folder that is not there, or is there but empty while the library knows songs in it (a drive that is not mounted), must
never turn into "every song was deleted"; and a server looks for new songs by itself."""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from musictoolkit.cli import app as cli_app
from musictoolkit.db.connection import connect
from musictoolkit.ingest import scanner
from musictoolkit.ingest.scanner import LibraryUnavailable, folder_unavailable_for, looks_unmounted, scan_library
from musictoolkit.web import rescan_job
from musictoolkit.web.rescan_job import RescanSchedule
from tests.conftest import build_web, make_track, scan


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()


def _flags(conn) -> list[int]:
    return [row["is_missing"] for row in conn.execute("SELECT is_missing FROM tracks ORDER BY id")]


def _empty(folder: Path) -> None:
    for child in folder.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()


# ---------------------------------------------------------------------------------------------------- the scanner


def test_a_folder_with_nothing_in_it_looks_unmounted_and_one_with_anything_in_it_does_not(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert looks_unmounted(empty)
    assert looks_unmounted(tmp_path / "not there")
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    assert looks_unmounted(tmp_path / "file.txt")  # (not a folder)
    (empty / "lost+found").mkdir()
    assert not looks_unmounted(empty)
    assert not looks_unmounted(str(tmp_path))


def test_an_empty_folder_with_songs_known_in_it_is_not_scanned_and_nothing_is_marked_missing(conn, tmp_path: Path) -> None:
    library = tmp_path / "srv" / "music"
    make_track(library, "a.mp3", title="A")
    make_track(library / "Album", "b.mp3", title="B")
    assert scan_library(conn, library).added == 2
    _empty(library)
    with pytest.raises(LibraryUnavailable, match=r"is empty, but the library has songs in it\. A drive that is not mounted looks like that"):
        scan_library(conn, library)
    assert _flags(conn) == [0, 0]


def test_force_scans_it_anyway_and_every_song_is_marked_missing(conn, tmp_path: Path) -> None:
    library = tmp_path / "music"
    make_track(library, "a.mp3", title="A")
    make_track(library, "b.mp3", title="B")
    scan_library(conn, library)
    _empty(library)
    result = scan_library(conn, library, force=True)
    assert (result.missing, result.added) == (2, 0) and _flags(conn) == [1, 1]


def test_a_folder_that_is_not_there_is_refused_whether_or_not_it_has_songs(conn, tmp_path: Path) -> None:
    with pytest.raises(LibraryUnavailable, match=r"is not there \(is the drive connected\?\)$"):
        scan_library(conn, tmp_path / "typo")
    library = tmp_path / "music"
    make_track(library, "a.mp3", title="A")
    scan_library(conn, library)
    gone = tmp_path / "gone"
    library.rename(gone)
    with pytest.raises(LibraryUnavailable, match=r"is not there"):
        scan_library(conn, library)
    assert _flags(conn) == [0]
    assert scan_library(conn, library, force=True).missing == 1


def test_a_new_empty_folder_is_scanned_without_trouble(conn, tmp_path: Path) -> None:
    library = tmp_path / "music"
    library.mkdir()
    result = scan_library(conn, library)
    assert (result.added, result.missing) == (0, 0)


def test_songs_that_are_already_missing_do_not_make_an_empty_folder_suspect(conn, tmp_path: Path) -> None:
    library = tmp_path / "music"
    make_track(library, "a.mp3", title="A")
    scan_library(conn, library)
    (library / "keep.txt").write_text("x", encoding="utf-8")  # (something in the folder: the song can be marked missing)
    (library / "a.mp3").unlink()
    assert scan_library(conn, library).missing == 1
    (library / "keep.txt").unlink()
    assert scan_library(conn, library).missing == 0  # (nothing present is left to lose: the only song was already missing)
    assert _flags(conn) == [1]


def test_songs_in_other_folders_do_not_make_an_empty_folder_suspect(conn, tmp_path: Path) -> None:
    other = tmp_path / "other"
    make_track(other, "a.mp3", title="A")
    scan_library(conn, other)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert scan_library(conn, fresh).missing == 0
    assert _flags(conn) == [0]


def test_a_folder_that_only_starts_like_the_library_folder_is_not_inside_it(conn, tmp_path: Path) -> None:
    library, sibling = tmp_path / "music", tmp_path / "music2"
    make_track(sibling, "a.mp3", title="A")
    scan_library(conn, sibling)
    library.mkdir()
    assert scan_library(conn, library).missing == 0  # (music2 is not inside music)


def test_a_song_is_not_found_because_its_drive_is_gone_only_when_its_folder_looks_unmounted(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    song = music / "a.mp3"
    assert folder_unavailable_for(song, [str(music)])  # (an empty folder: where a drive that is not mounted would be)
    (music / "keep.txt").write_text("x", encoding="utf-8")
    assert not folder_unavailable_for(song, [str(music)])  # (the folder has things in it: this song is gone, the drive is not)
    assert folder_unavailable_for(tmp_path / "gone" / "a.mp3", [str(tmp_path / "gone")])  # (a folder that is not there at all)
    assert not folder_unavailable_for(song, [str(tmp_path / "somewhere else")])  # (the song is not in that folder)
    assert not folder_unavailable_for(song, [])
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not folder_unavailable_for(tmp_path / "empty2" / "a.mp3", [str(empty)])  # (empty2 only starts like empty)
    assert folder_unavailable_for(empty / "deep" / "er" / "a.mp3", [str(tmp_path / "other"), str(empty)])  # (any of the folders)


# ---------------------------------------------------------------------------------------------------- the scan job, the stream


def test_the_scan_job_skips_a_folder_that_looks_unmounted_and_says_so(web, music_dir: Path) -> None:
    before = web.client.get("/api/about").json()["tracks"]
    _empty(music_dir)
    result = scan(web)
    assert result["offline_roots"] == [str(music_dir)] and result["missing"] == 0
    assert web.client.get("/api/about").json()["tracks"] == before


def test_the_scan_job_still_marks_a_deleted_song_missing_when_the_folder_has_others(web, music_dir: Path) -> None:
    first = next(music_dir.rglob("*.mp3"))
    first.unlink()
    result = scan(web)
    assert result["offline_roots"] == [] and result["missing"] == 1


def test_playing_a_song_whose_drive_is_gone_says_so_and_does_not_forget_the_song(web, music_dir: Path) -> None:
    track = web.client.get("/api/tracks", params={"limit": 1}).json()["items"][0]["id"]
    assert web.client.get(f"/api/tracks/{track}/stream").status_code == 200
    gone = music_dir.with_name("unplugged")
    music_dir.rename(gone)
    refused = web.client.get(f"/api/tracks/{track}/stream")
    assert refused.status_code == 503 and refused.json()["detail"] == "The library's drive is not available right now. Is it connected?"
    music_dir.mkdir()  # (the mount point: there, and empty)
    assert web.client.get(f"/api/tracks/{track}/stream").status_code == 503
    assert web.client.get(f"/api/tracks/{track}/info").status_code == 503
    with web.ctx.db() as db:
        assert db.execute("SELECT COUNT(*) FROM tracks WHERE is_missing = 1").fetchone()[0] == 0
    music_dir.rmdir()
    gone.rename(music_dir)
    assert web.client.get(f"/api/tracks/{track}/stream").status_code == 200


def test_a_song_that_is_gone_from_a_folder_that_has_others_is_still_forgotten_as_before(web, music_dir: Path) -> None:
    track = web.client.get("/api/tracks", params={"limit": 1}).json()["items"][0]["id"]
    with web.ctx.db() as db:
        path = Path(db.execute("SELECT file_path FROM tracks WHERE id = ?", (track,)).fetchone()[0])
    path.unlink()
    missing = web.client.get(f"/api/tracks/{track}/stream")
    assert missing.status_code == 404 and missing.json()["detail"] == "the file is missing from disk"
    with web.ctx.db() as db:
        assert db.execute("SELECT is_missing FROM tracks WHERE id = ?", (track,)).fetchone()[0] == 1


def test_a_song_opened_from_outside_every_library_folder_that_is_gone_is_forgotten_as_before(web, tmp_path: Path) -> None:
    outside = make_track(tmp_path / "elsewhere", "o.mp3", title="Outside")
    with web.ctx.db() as db:
        scanner.add_files(db, [str(outside)])
        track = db.execute("SELECT id FROM tracks WHERE title = 'Outside'").fetchone()[0]
    outside.unlink()
    assert web.client.get(f"/api/tracks/{track}/stream").status_code == 404


# ---------------------------------------------------------------------------------------------------- the command line


def test_the_scan_command_refuses_a_folder_that_looks_unmounted_and_force_overrides(tmp_path: Path) -> None:
    library = tmp_path / "music"
    make_track(library, "a.mp3", title="A")
    config = tmp_path / "config.toml"
    config.write_text('[database]\npath = "./data/library.db"\n[logging]\ndir = "./logs"\n', encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(cli_app, ["--config", str(config), "scan", str(library)]).exit_code == 0
    _empty(library)
    refused = runner.invoke(cli_app, ["--config", str(config), "scan", str(library)])
    assert refused.exit_code == 1 and "Not scanned: " in refused.output and "A drive that is not mounted looks like that" in refused.output
    forced = runner.invoke(cli_app, ["--config", str(config), "scan", str(library), "--force"])
    assert forced.exit_code == 0 and "Missing: 1" in forced.output
    typo = runner.invoke(cli_app, ["--config", str(config), "scan", str(tmp_path / "typo")])
    assert typo.exit_code == 1 and "is not there" in typo.output


# ---------------------------------------------------------------------------------------------------- looking by itself


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], str]]:
    calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr("musictoolkit.web.routers.manage.submit_scan", lambda ctx, roots, title="": calls.append((list(roots), title)))
    return calls


def _ctx(*roots: str) -> SimpleNamespace:
    return SimpleNamespace(config=SimpleNamespace(library=SimpleNamespace(roots=list(roots))))


def test_it_asks_for_a_scan_of_every_folder_every_so_many_hours_the_first_that_long_after_the_start(asked) -> None:
    clock = _Clock()
    schedule = RescanSchedule(_ctx("/a", "/b"), hours=2, clock=clock)
    assert schedule.enabled and schedule.tick() is False
    clock.now += 2 * 3600 - 1
    assert schedule.tick() is False and asked == []
    clock.now += 1
    assert schedule.tick() is True and asked == [(["/a", "/b"], "Refreshing library")]
    assert schedule.tick() is False  # (the clock starts again from the scan it asked for)
    clock.now += 2 * 3600
    assert schedule.tick() is True and len(asked) == 2


def test_hours_of_zero_or_less_never_ask_and_start_no_thread(asked) -> None:
    for hours in (0, -1):
        clock = _Clock()
        schedule = RescanSchedule(_ctx("/a"), hours=hours, clock=clock)
        clock.now += 10**9
        assert not schedule.enabled and schedule.tick() is False and asked == []
        schedule.start()
        assert schedule._thread is None
        schedule.stop()


def test_with_no_library_folders_there_is_nothing_to_ask_for(asked) -> None:
    clock = _Clock()
    schedule = RescanSchedule(_ctx(), hours=1, clock=clock)
    clock.now += 7200
    assert schedule.tick() is False and asked == []


def test_a_fraction_of_an_hour_is_allowed(asked) -> None:
    clock = _Clock()
    schedule = RescanSchedule(_ctx("/a"), hours=0.5, clock=clock)
    clock.now += 1800
    assert schedule.tick() is True


def test_the_thread_looks_again_and_again_and_stops_when_asked(asked, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rescan_job, "CHECK_EVERY", 0.01)
    clock = _Clock()
    schedule = RescanSchedule(_ctx("/a"), hours=1, clock=clock)
    schedule.start()
    thread = schedule._thread
    assert thread is not None and thread.is_alive() and thread.daemon and thread.name == "library-rescan"
    schedule.start()
    assert schedule._thread is thread  # (a second start is no second thread)
    clock.now += 3600
    deadline = time.monotonic() + 10
    while not asked and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(asked) == 1
    schedule.stop()
    assert not thread.is_alive() and schedule._thread is None


def test_a_scan_that_cannot_be_asked_for_is_logged_and_the_thread_goes_on(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(rescan_job, "CHECK_EVERY", 0.01)
    seen = threading.Event()

    def boom(ctx, roots, title=""):
        seen.set()
        raise RuntimeError("no")

    monkeypatch.setattr("musictoolkit.web.routers.manage.submit_scan", boom)
    clock = _Clock()
    schedule = RescanSchedule(_ctx("/a"), hours=1, clock=clock)
    with caplog.at_level("WARNING", logger="musictoolkit"):
        schedule.start()
        clock.now += 3600
        assert seen.wait(10)
        deadline = time.monotonic() + 10
        while "The scheduled look for new songs failed" not in caplog.text and time.monotonic() < deadline:
            time.sleep(0.01)
        assert schedule._thread is not None and schedule._thread.is_alive()
        schedule.stop()
    assert "The scheduled look for new songs failed" in caplog.text


def test_the_app_starts_the_schedule_with_itself_and_stops_it_with_itself(tmp_path: Path) -> None:
    web = build_web(tmp_path, None, rescan_hours=6)
    assert web.ctx.rescan.enabled and web.ctx.rescan._thread is None
    with TestClient(web.app):
        assert web.ctx.rescan._thread is not None and web.ctx.rescan._thread.is_alive()
    assert web.ctx.rescan._thread is None
    idle = build_web(tmp_path / "idle", None)
    with TestClient(idle.app):
        assert not idle.ctx.rescan.enabled and idle.ctx.rescan._thread is None
