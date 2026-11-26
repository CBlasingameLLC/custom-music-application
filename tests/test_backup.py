"""Copies of the database: made whole while the app writes, looked over before they are kept, copied to other folders and read back, pruned to the newest, checked and restored."""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import os
import shutil
import sqlite3
import stat
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from musictoolkit import backup
from musictoolkit.backup import BackupError
from musictoolkit.db.connection import connect

WHEN = datetime(2026, 11, 26, 17, 30, 5)


def fill(path: Path, songs: int = 5, plays: int = 3) -> Path:
    """A real library database with a few songs (the first two favorites), plays and a playlist."""
    conn = connect(path)
    conn.executemany("INSERT INTO tracks (file_path, title, favorite) VALUES (?, ?, ?)", [(f"/m/{n}.mp3", f"Song {n}", 1 if n < 2 else 0) for n in range(songs)])
    conn.executemany("INSERT INTO play_history (track_id, source, played_at_epoch, ms_played) VALUES (1, 'future_scrobble', ?, 1000)", [(1_700_000_000 + n,) for n in range(plays)])
    conn.execute("INSERT INTO playlists (name, source, kind) VALUES ('Mine', 'manual', 'manual')")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return fill(tmp_path / "data" / "library.db")


def counts(path: Path) -> dict[str, int]:
    report = backup.check(path)
    assert report.ok, report.problem
    return report.counts


def aged(path: Path, seconds: float) -> Path:
    """The file, made `seconds` ago."""
    then = time.time() - seconds
    os.utime(path, (then, then))
    return path


def touch(folder: Path, name: str, seconds_ago: float = 0.0) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"x")
    return aged(path, seconds_ago)


# --------------------------------------------------------------------------------------------------------------------------------- a copy


def test_a_copy_is_named_for_the_moment_and_holds_what_the_database_holds(db: Path, tmp_path: Path) -> None:
    (tmp_path / "copies").mkdir()
    made = backup.snapshot(db, tmp_path / "copies", now=WHEN)
    assert made == tmp_path / "copies" / "library-20261126-173005.db"
    assert counts(made) == {"songs": 5, "plays": 3, "playlists": 1, "favorites": 2} == counts(db)
    assert backup.NAME.match(made.name)


def test_a_copy_is_one_file_that_stands_by_itself(db: Path, tmp_path: Path) -> None:
    folder = tmp_path / "copies"
    folder.mkdir()
    made = backup.snapshot(db, folder, now=WHEN)
    assert [path.name for path in folder.iterdir()] == [made.name]  # (no half-written file, no log of a write-ahead journal beside it)
    opened = sqlite3.connect(made)
    assert opened.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    opened.close()


def test_a_copy_has_what_the_app_wrote_that_is_still_in_the_journal_and_not_yet_in_the_file(db: Path, tmp_path: Path) -> None:
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")  # (nothing is moved from the journal into the database file)
    writer.execute("INSERT INTO tracks (file_path, title) VALUES ('/m/new.mp3', 'New')")
    writer.commit()
    assert (db.parent / (db.name + "-wal")).stat().st_size > 0
    (tmp_path / "copies").mkdir()
    made = backup.snapshot(db, tmp_path / "copies", now=WHEN)
    writer.close()
    assert counts(made)["songs"] == 6


def test_a_copy_is_not_made_of_a_database_that_is_not_there_and_none_is_made_by_asking(tmp_path: Path) -> None:
    (tmp_path / "copies").mkdir()
    with pytest.raises(BackupError, match="there is no database to copy"):
        backup.snapshot(tmp_path / "nothing.db", tmp_path / "copies", now=WHEN)
    assert list((tmp_path / "copies").iterdir()) == [] and not (tmp_path / "nothing.db").exists()


def test_a_database_that_cannot_be_read_leaves_nothing_in_the_folder(tmp_path: Path) -> None:
    broken = tmp_path / "library.db"
    broken.write_bytes(b"this is not a database, " * 100)
    (tmp_path / "copies").mkdir()
    with pytest.raises(sqlite3.DatabaseError):
        backup.snapshot(broken, tmp_path / "copies", now=WHEN)
    assert list((tmp_path / "copies").iterdir()) == []


def test_a_database_that_fails_its_own_check_is_not_kept_as_a_copy(db: Path, tmp_path: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode = DELETE")
    root, size = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'tracks'").fetchone()[0], conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    with db.open("r+b") as handle:  # (the first page of the songs' table, written over with what is not a page of a table)
        handle.seek((root - 1) * size)
        handle.write(b"\xff" * 64)
    (tmp_path / "copies").mkdir()
    with pytest.raises(BackupError, match="did not pass its own check"):
        backup.snapshot(db, tmp_path / "copies", now=WHEN)
    assert list((tmp_path / "copies").iterdir()) == []


# ------------------------------------------------------------------------------------------------------------------------------- listing


def test_the_copies_of_a_folder_are_listed_newest_first_and_only_those(tmp_path: Path) -> None:
    folder = tmp_path / "copies"
    for name, ago in (("library-20260101-000000.db", 300), ("library-20260102-000000.db", 100), ("library-20260103-000000.db", 200)):
        touch(folder, name, ago)
    for stranger in ("notes.txt", "library-before-restore-20260101-000000.db", "library-2026.db", "library-20260101-000000.db.partial", "LIBRARY-20260109-000000.db", "xlibrary-20260101-000000.db"):  # (a different day in capitals: on a file system that does not tell capitals apart it would be the copy itself)
        touch(folder, stranger)
    (folder / "library-20260105-000000.db").mkdir()  # (a folder with such a name is no copy... and no file)
    assert [copy.path.name for copy in backup.listing(folder)] == ["library-20260102-000000.db", "library-20260103-000000.db", "library-20260101-000000.db"]
    assert [copy.size for copy in backup.listing(folder)] == [1, 1, 1]


def test_a_folder_that_is_not_there_has_no_copies(tmp_path: Path) -> None:
    assert backup.listing(tmp_path / "nowhere") == []
    (tmp_path / "file").write_bytes(b"x")
    assert backup.listing(tmp_path / "file") == []


def test_a_copy_that_is_dated_ahead_of_the_clock_is_the_oldest_and_not_the_newest(tmp_path: Path) -> None:
    folder, now = tmp_path / "copies", 1_000_000.0
    for name, made in (("library-20260101-000000.db", now - 1000), ("library-20260102-000000.db", now + backup.FUTURE_SLACK + 1), ("library-20260103-000000.db", now + backup.FUTURE_SLACK)):
        os.utime(touch(folder, name), (made, made))  # (the second was written when the clock was ahead; the third only a little: the clocks of two computers differ)
    copies = backup.listing(folder, now=now)
    assert [(copy.path.name, copy.made) for copy in copies] == [
        ("library-20260103-000000.db", now + backup.FUTURE_SLACK), ("library-20260101-000000.db", now - 1000), ("library-20260102-000000.db", 0.0)]


@pytest.mark.skipif(os.name == "nt", reason="on Windows scandir hands over the whole entry with the listing: there is no later call that can find the file gone")
def test_a_copy_that_goes_from_the_folder_while_it_is_being_listed_is_not_in_the_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "copies"
    touch(folder, "library-20260101-000000.db")
    gone = touch(folder, "library-20260102-000000.db")
    real = os.scandir

    def vanishing(path):
        entries = list(real(path))
        gone.unlink()  # (after the folder was read, before the entry was asked about)
        return iter(entries)

    monkeypatch.setattr(os, "scandir", vanishing)
    assert [copy.path.name for copy in backup.listing(folder)] == ["library-20260101-000000.db"]


# --------------------------------------------------------------------------------------------------------------------------------- copying


def test_a_copy_in_another_folder_is_the_same_bytes_with_the_date_it_was_made(db: Path, tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    made = aged(backup.snapshot(db, tmp_path / "a", now=WHEN), 5000)
    other = backup.copy_to(made, tmp_path / "b")
    assert other == tmp_path / "b" / made.name and other.read_bytes() == made.read_bytes()
    assert abs(other.stat().st_mtime - made.stat().st_mtime) < 1 and sorted(path.name for path in (tmp_path / "b").iterdir()) == [made.name]


@pytest.mark.skipif(os.name == "nt", reason="file permissions")
@pytest.mark.parametrize("umask,mode", [(0o000, 0o640), (0o022, 0o640), (0o002, 0o640), (0o077, 0o600)])
def test_a_copy_is_for_its_owner_and_the_group_and_not_for_everyone_and_a_stricter_umask_is_kept(db: Path, tmp_path: Path, umask: int, mode: int) -> None:
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir()
    there.mkdir()
    before = os.umask(umask)
    try:
        copy = backup.snapshot(db, here, now=WHEN)
        other = backup.copy_to(copy, there)
    finally:
        os.umask(before)
    assert stat.S_IMODE(copy.stat().st_mode) == stat.S_IMODE(other.stat().st_mode) == mode


def flip_the_last_byte_when_copying(monkeypatch: pytest.MonkeyPatch) -> None:
    real = shutil.copyfile

    def copyfile(source, target):
        real(source, target)
        with open(target, "r+b") as handle:
            handle.seek(-1, os.SEEK_END)
            last = handle.read(1)
            handle.seek(-1, os.SEEK_END)
            handle.write(bytes([last[0] ^ 0xFF]))

    monkeypatch.setattr(backup.shutil, "copyfile", copyfile)


def test_a_copy_that_is_not_what_was_read_is_not_kept(db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    made = backup.snapshot(db, tmp_path / "a", now=WHEN)
    flip_the_last_byte_when_copying(monkeypatch)
    with pytest.raises(BackupError, match=r"^what was written is not what was read back$"):
        backup.copy_to(made, tmp_path / "b")
    assert list((tmp_path / "b").iterdir()) == []  # (not the copy, and not the half of it)


def test_the_read_back_goes_through_every_byte_of_a_big_file_and_not_only_the_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    big = tmp_path / "a" / "library-20260101-000000.db"
    big.write_bytes(os.urandom(3 * (1 << 20) + 17))  # (more than three blocks of what is read at a time)
    assert backup._digest(big) == hashlib.sha256(big.read_bytes()).hexdigest()
    assert backup.copy_to(big, tmp_path / "b").read_bytes() == big.read_bytes()  # (the same bytes: kept)
    flip_the_last_byte_when_copying(monkeypatch)
    (tmp_path / "c").mkdir()
    with pytest.raises(BackupError, match="what was written is not what was read back"):
        backup.copy_to(big, tmp_path / "c")


def test_a_copy_that_cannot_be_written_leaves_nothing_in_the_folder(db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    made = backup.snapshot(db, tmp_path / "a", now=WHEN)

    def full(source, target):
        Path(target).write_bytes(b"half")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(backup.shutil, "copyfile", full)
    with pytest.raises(OSError):
        backup.copy_to(made, tmp_path / "b")
    assert list((tmp_path / "b").iterdir()) == []


# ---------------------------------------------------------------------------------------------------------------------------------- pruning


def test_only_the_newest_copies_are_kept_and_nothing_else_of_the_folder_is_touched(tmp_path: Path) -> None:
    folder = tmp_path / "copies"
    for number in range(6):
        touch(folder, f"library-2026010{number}-000000.db", 1000 - number * 100)  # (the later the name, the newer)
    keepsakes = [touch(folder, name) for name in ("notes.txt", "library-before-restore-20260101-000000.db", "library-2026.db")]
    assert backup.prune(folder, keep=2) == 4
    assert sorted(path.name for path in folder.iterdir() if backup.NAME.match(path.name)) == ["library-20260104-000000.db", "library-20260105-000000.db"]
    assert all(path.exists() for path in keepsakes)


def test_nothing_is_taken_away_when_there_are_not_more_than_are_kept(tmp_path: Path) -> None:
    folder = tmp_path / "copies"
    for number in range(3):
        touch(folder, f"library-2026010{number}-000000.db", number)
    assert backup.prune(folder, keep=3) == 0 and backup.prune(folder, keep=5) == 0
    assert len(list(folder.iterdir())) == 3


def test_the_copy_that_was_just_made_is_not_taken_away_even_when_it_is_not_among_the_newest(tmp_path: Path) -> None:
    folder = tmp_path / "copies"
    older = touch(folder, "library-20260101-000000.db", 5000)
    touch(folder, "library-20260102-000000.db", 100)
    touch(folder, "library-20260103-000000.db", 50)
    assert backup.prune(folder, keep=1, protect=older) == 1  # (the newest stays, the protected stays: one of the two others goes)
    assert older.exists() and sorted(path.name for path in folder.iterdir()) == ["library-20260101-000000.db", "library-20260103-000000.db"]


def test_a_half_written_copy_left_by_a_crash_is_cleared_after_an_hour_and_not_before(tmp_path: Path) -> None:
    folder = tmp_path / "copies"
    old = touch(folder, "library-20260101-000000.db.partial", backup.STALE_PARTIAL + 60)
    fresh = touch(folder, "library-20260102-000000.db.partial", backup.STALE_PARTIAL - 60)  # (one being written now)
    other = touch(folder, "something.db.partial", backup.STALE_PARTIAL + 60)  # (not ours)
    backup.prune(folder, keep=5)
    assert not old.exists() and fresh.exists() and other.exists()


def test_a_copy_dated_ahead_of_the_clock_is_the_first_to_go(tmp_path: Path) -> None:
    folder = tmp_path / "copies"
    touch(folder, "library-20260101-000000.db", 300)
    touch(folder, "library-20260102-000000.db", 200)
    touch(folder, "library-20260103-000000.db", -86400 * 400)
    backup.prune(folder, keep=2)
    assert sorted(path.name for path in folder.iterdir()) == ["library-20260101-000000.db", "library-20260102-000000.db"]


# ------------------------------------------------------------------------------------------------------------------------------ a whole run


def test_a_run_makes_a_copy_in_each_folder_and_keeps_each_to_the_newest(db: Path, tmp_path: Path) -> None:
    primary, drive = tmp_path / "data" / "backups", tmp_path / "drive"
    drive.mkdir()
    for number in range(4):
        for folder in (primary, drive):
            touch(folder, f"library-2026010{number}-000000.db", 9000 - number * 100)
    outcomes = backup.run(db, primary, [drive], keep=3, now=WHEN)
    assert [(o.folder, o.problem, o.pruned) for o in outcomes] == [(primary, None, 2), (drive, None, 2)]
    for outcome in outcomes:
        assert outcome.path == outcome.folder / "library-20261126-173005.db" and outcome.size == outcome.path.stat().st_size > 0
        assert len(backup.listing(outcome.folder)) == 3 and counts(outcome.path)["songs"] == 5
    assert outcomes[0].path.read_bytes() == outcomes[1].path.read_bytes()


def test_the_first_folder_is_made_and_the_others_are_not(db: Path, tmp_path: Path) -> None:
    outcomes = backup.run(db, tmp_path / "data" / "backups", [tmp_path / "unmounted" / "backups"], keep=3, now=WHEN)
    assert outcomes[0].problem is None and outcomes[0].path.is_file()
    assert outcomes[1].path is None and outcomes[1].problem == "the folder is not there (is the drive connected?)" and not (tmp_path / "unmounted").exists()


def test_a_folder_that_cannot_be_written_says_why_in_words_and_the_others_go_on(db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    primary, full, denied, fine = tmp_path / "p", tmp_path / "full", tmp_path / "denied", tmp_path / "fine"
    for folder in (full, denied, fine):
        folder.mkdir()
    real = backup.shutil.copyfile

    def copyfile(source, target):
        if Path(target).parent == full:
            raise OSError(errno.ENOSPC, "No space left on device")
        if Path(target).parent == denied:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(source, target)

    monkeypatch.setattr(backup.shutil, "copyfile", copyfile)
    outcomes = backup.run(db, primary, [full, denied, fine], keep=3, now=WHEN)
    assert [o.problem for o in outcomes] == [None, "No space left on device", "Permission denied", None]
    assert (fine / "library-20261126-173005.db").is_file() and list(full.iterdir()) == [] and list(denied.iterdir()) == []


def test_when_the_first_folder_cannot_be_written_the_next_one_gets_the_copy_that_is_made_from_the_database(db: Path, tmp_path: Path) -> None:
    (tmp_path / "file").write_bytes(b"x")  # (a file where the folder should be: it cannot be made)
    drive = tmp_path / "drive"
    drive.mkdir()
    outcomes = backup.run(db, tmp_path / "file" / "backups", [drive], keep=3, now=WHEN)
    assert outcomes[0].path is None and outcomes[0].problem
    assert outcomes[1].problem is None and counts(outcomes[1].path)["songs"] == 5


def test_a_folder_named_twice_gets_one_copy(db: Path, tmp_path: Path) -> None:
    primary = tmp_path / "data" / "backups"
    primary.mkdir(parents=True)
    outcomes = backup.run(db, primary, [primary, tmp_path / "data" / ".." / "data" / "backups"], keep=3, now=WHEN)
    assert len(outcomes) == 1 and len(backup.listing(primary)) == 1


def test_a_database_that_is_not_there_is_said_in_words_for_every_folder(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    outcomes = backup.run(tmp_path / "nothing.db", tmp_path / "p", [drive], keep=3, now=WHEN)
    assert [o.problem for o in outcomes] == ["there is no database to copy"] * 2 and not (tmp_path / "nothing.db").exists()


def test_a_problem_that_is_not_an_os_error_is_said_with_its_kind(db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(backup, "snapshot", boom)
    assert backup.run(db, tmp_path / "p", keep=3)[0].problem == "RuntimeError: boom"


# ----------------------------------------------------------------------------------------------------------------------------- catching up


def test_a_copy_that_was_made_is_put_in_each_folder_as_it_is_and_each_is_kept_to_the_newest(db: Path, tmp_path: Path) -> None:
    primary, drive, other = tmp_path / "data" / "backups", tmp_path / "drive", tmp_path / "other"
    for folder in (primary, drive, other):
        folder.mkdir(parents=True)
    copy = backup.snapshot(db, primary, now=WHEN)
    os.utime(copy, (1_700_000_000, 1_700_000_000))  # (made some time ago: its date goes with it)
    for number in range(4):
        path = touch(drive, f"library-2022010{number}-000000.db")
        os.utime(path, (1_650_000_000 + number, 1_650_000_000 + number))  # (older than the copy)
    outcomes = backup.catch_up(copy, [drive, other], primary, keep=3)
    assert [(o.folder, o.problem, o.pruned) for o in outcomes] == [(drive, None, 2), (other, None, 0)]
    for outcome in outcomes:
        assert outcome.path == outcome.folder / copy.name and outcome.size == copy.stat().st_size and outcome.path.read_bytes() == copy.read_bytes()
        assert outcome.path.stat().st_mtime == 1_700_000_000  # (the date it was made, not the date it was copied)
    assert [c.path.name for c in backup.listing(drive)] == [copy.name, "library-20220103-000000.db", "library-20220102-000000.db"]
    assert len(backup.listing(other)) == 1 and [c.path for c in backup.listing(primary)] == [copy]


def test_catching_up_makes_the_primary_folder_and_not_the_others(db: Path, tmp_path: Path) -> None:
    copy = backup.snapshot(db, tmp_path, now=WHEN)
    primary, unmounted = tmp_path / "data" / "backups", tmp_path / "unmounted" / "backups"
    outcomes = backup.catch_up(copy, [primary, unmounted], primary, keep=3)
    assert outcomes[0].problem is None and outcomes[0].path == primary / copy.name and outcomes[0].path.is_file()
    assert outcomes[1].path is None and outcomes[1].problem == "the folder is not there (is the drive connected?)" and not (tmp_path / "unmounted").exists()


def test_catching_up_says_why_a_folder_could_not_be_written_in_words_and_the_rest_go_on(db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = backup.snapshot(db, tmp_path, now=WHEN)
    full, boom, fine = tmp_path / "full", tmp_path / "boom", tmp_path / "fine"
    for folder in (full, boom, fine):
        folder.mkdir()
    real = backup.copy_to

    def copy_to(source: Path, folder: Path) -> Path:
        if folder == full:
            raise OSError(errno.ENOSPC, "No space left on device")
        if folder == boom:
            raise RuntimeError("boom")
        return real(source, folder)

    monkeypatch.setattr(backup, "copy_to", copy_to)
    outcomes = backup.catch_up(copy, [full, boom, fine], tmp_path / "p", keep=3)
    assert [o.problem for o in outcomes] == ["No space left on device", "RuntimeError: boom", None] and (fine / copy.name).is_file()


def test_catching_up_with_nothing_to_give_to_does_nothing(db: Path, tmp_path: Path) -> None:
    assert backup.catch_up(backup.snapshot(db, tmp_path, now=WHEN), [], tmp_path / "p", keep=3) == []
    assert not (tmp_path / "p").exists()


# ---------------------------------------------------------------------------------------------------------------------------------- checking


def test_a_sound_copy_says_so_with_what_it_holds_and_its_layout(db: Path, tmp_path: Path) -> None:
    (tmp_path / "copies").mkdir()
    report = backup.check(backup.snapshot(db, tmp_path / "copies", now=WHEN))
    assert report.ok and report.problem is None and report.counts == {"songs": 5, "plays": 3, "playlists": 1, "favorites": 2} and report.schema == backup.latest_schema() > 0


@pytest.mark.parametrize("content,said", [
    (b"this is not a database, " * 100, "this is not a Music Toolkit database"),
    (b"", "this is not a Music Toolkit database"),
])
def test_a_file_that_is_not_a_database_of_this_app_says_so(tmp_path: Path, content: bytes, said: str) -> None:
    path = tmp_path / "library-20260101-000000.db"
    path.write_bytes(content)
    report = backup.check(path)
    assert not report.ok and report.problem.startswith(said) and report.counts == {}


def test_a_database_of_something_else_is_not_one_of_this_apps(tmp_path: Path) -> None:
    path = tmp_path / "other.db"
    other = sqlite3.connect(path)
    other.execute("CREATE TABLE thing (id INTEGER)")
    other.commit()
    other.close()
    report = backup.check(path)
    assert not report.ok and "no such table" in report.problem and report.problem.startswith("this is not a Music Toolkit database")


def test_a_copy_that_is_not_there_says_so(tmp_path: Path) -> None:
    report = backup.check(tmp_path / "nothing.db")
    assert not report.ok and report.problem == "unable to open database file"
    assert not (tmp_path / "nothing.db").exists()


def test_a_damaged_copy_says_where_it_is_not_sound(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode = DELETE")
    root, size = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'tracks'").fetchone()[0], conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    with db.open("r+b") as handle:
        handle.seek((root - 1) * size)
        handle.write(b"\xff" * 64)
    report = backup.check(db)
    assert not report.ok and report.problem == "it is damaged (database disk image is malformed)"


def test_a_copy_is_looked_at_without_changing_it_or_leaving_anything_beside_it(db: Path, tmp_path: Path) -> None:
    (tmp_path / "copies").mkdir()
    made = backup.snapshot(db, tmp_path / "copies", now=WHEN)
    before = (made.read_bytes(), made.stat().st_mtime_ns)
    backup.check(made)
    assert (made.read_bytes(), made.stat().st_mtime_ns) == before and [path.name for path in made.parent.iterdir()] == [made.name]


def test_a_copy_on_a_folder_whose_name_has_odd_characters_is_checked_too(db: Path, tmp_path: Path) -> None:
    folder = tmp_path / "my backups #1 (new) & 100%"
    folder.mkdir()
    assert backup.check(backup.snapshot(db, folder, now=WHEN)).ok


# --------------------------------------------------------------------------------------------------------------------------------- restoring


def test_a_restore_puts_back_what_the_copy_had_and_keeps_what_was_there_first(db: Path, tmp_path: Path) -> None:
    (tmp_path / "copies").mkdir()
    made = backup.snapshot(db, tmp_path / "copies", now=WHEN)
    conn = connect(db)
    conn.execute("DELETE FROM play_history")
    conn.execute("DELETE FROM playlists")
    conn.execute("UPDATE tracks SET favorite = 0")
    conn.commit()
    conn.close()
    kept = backup.restore(made, db, tmp_path / "copies", now=datetime(2026, 11, 27, 8, 0, 0))
    assert counts(db) == {"songs": 5, "plays": 3, "playlists": 1, "favorites": 2}
    assert kept == tmp_path / "copies" / "library-before-restore-20261127-080000.db" and counts(kept) == {"songs": 5, "plays": 0, "playlists": 0, "favorites": 0}
    assert not backup.NAME.match(kept.name)  # (pruning does not take it away)
    backup.prune(tmp_path / "copies", keep=1)
    assert kept.exists()


def test_a_restore_works_while_the_app_has_the_database_open_and_the_app_sees_what_was_restored(db: Path, tmp_path: Path) -> None:
    (tmp_path / "copies").mkdir()
    made = backup.snapshot(db, tmp_path / "copies", now=WHEN)
    app = connect(db)  # (the app: open, in WAL mode)
    app.execute("DELETE FROM playlists")
    app.commit()
    backup.restore(made, db, tmp_path / "copies", now=WHEN)
    assert app.execute("SELECT COUNT(*) FROM playlists").fetchone()[0] == 1
    assert app.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    app.close()


def test_a_restore_where_there_is_no_database_makes_it_and_has_nothing_to_keep(db: Path, tmp_path: Path) -> None:
    (tmp_path / "copies").mkdir()
    made = backup.snapshot(db, tmp_path / "copies", now=WHEN)
    target = tmp_path / "new" / "data" / "library.db"
    assert backup.restore(made, target, tmp_path / "new" / "kept", now=WHEN) is None
    assert counts(target) == counts(db) and not (tmp_path / "new" / "kept").exists()


def test_a_copy_that_is_not_sound_is_not_restored_and_the_database_is_not_touched(db: Path, tmp_path: Path) -> None:
    bad = tmp_path / "library-20260101-000000.db"
    bad.write_bytes(b"this is not a database, " * 100)
    before = db.read_bytes()
    with pytest.raises(BackupError, match="cannot be restored: this is not a Music Toolkit database"):
        backup.restore(bad, db, tmp_path / "kept", now=WHEN)
    assert db.read_bytes() == before and not (tmp_path / "kept").exists()


def test_a_copy_made_by_a_newer_version_is_not_restored(db: Path, tmp_path: Path) -> None:
    (tmp_path / "copies").mkdir()
    made = backup.snapshot(db, tmp_path / "copies", now=WHEN)
    newer = sqlite3.connect(made)
    newer.execute(f"PRAGMA user_version = {backup.latest_schema() + 1}")
    newer.commit()
    newer.close()
    before = db.read_bytes()
    with pytest.raises(BackupError, match=f"made by a newer version of the app \\(its layout is {backup.latest_schema() + 1}, this one knows {backup.latest_schema()}\\)"):
        backup.restore(made, db, tmp_path / "copies", now=WHEN)
    assert db.read_bytes() == before


def test_a_copy_made_by_an_older_version_is_restored_and_the_app_brings_it_up_to_date(db: Path, tmp_path: Path) -> None:
    from musictoolkit.db.connection import _migration_files

    old = sqlite3.connect(tmp_path / "old.db")  # (a database as the version before the last change of layout made it)
    for version, _name, sql in _migration_files():
        if version < backup.latest_schema():
            old.executescript(sql)
            old.execute(f"PRAGMA user_version = {version}")
    old.execute("INSERT INTO tracks (file_path, title, favorite) VALUES ('/m/old.mp3', 'Old', 1)")
    old.commit()
    old.close()
    assert backup.check(tmp_path / "old.db").schema == backup.latest_schema() - 1
    backup.restore(tmp_path / "old.db", db, tmp_path / "copies", now=WHEN)
    assert counts(db)["songs"] == 1
    app = connect(db)  # (opening it runs the change of layout that it lacks)
    assert app.execute("PRAGMA user_version").fetchone()[0] == backup.latest_schema()
    assert "no_audio" in [row[1] for row in app.execute("PRAGMA table_info(tracks)")]
    app.close()


# -------------------------------------------------------------------------------------------------------------------- the small things


def test_a_result_is_a_value_that_cannot_be_changed_and_says_nothing_until_something_is_said() -> None:
    folder, path = Path("/x"), Path("/x/library-20260101-000000.db")
    assert dataclasses.astuple(backup.Outcome(folder)) == (folder, None, 0, 0, None)
    report = backup.Report(False)
    assert (report.ok, report.problem, report.schema, report.counts) == (False, None, 0, {})
    for value, attribute in ((backup.Copy(path, 1, 2.0), "size"), (backup.Outcome(folder), "size"), (backup.Report(True), "ok"), (backup.Settings(), "hours")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, attribute, 5)


@pytest.mark.parametrize("hours,keep", [(0, 1), (0.5, 1), (1, 1), (0.001, 365), (6, 7)])
def test_the_smallest_settings_that_make_sense_are_accepted(hours: float, keep: int) -> None:
    database = SimpleNamespace(backup_hours=hours, backup_keep=keep, backup_folders=[])
    assert backup.settings_of(database) == backup.Settings(hours, keep, ())


class _Tracked(sqlite3.Connection):
    closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> list:
    """Every connection `backup` opens to a database: what it was asked for, and whether it was closed."""
    opened: list = []

    class Module:
        def __getattr__(self, name: str):
            return getattr(sqlite3, name)

        @staticmethod
        def connect(*args, **kwargs):
            connection = sqlite3.connect(*args, factory=_Tracked, **kwargs)
            connection.asked = (args, kwargs)
            opened.append(connection)
            return connection

    monkeypatch.setattr(backup, "sqlite3", Module())
    return opened


def test_every_connection_is_closed_when_the_work_is_done_whether_it_worked_or_not(connections: list, db: Path, tmp_path: Path) -> None:
    (tmp_path / "c").mkdir()
    made = backup.snapshot(db, tmp_path / "c", now=WHEN)
    assert len(connections) == 2 and all(c.closed for c in connections)  # (the database and the copy)
    assert [c.asked for c in connections] == [((db,), {"timeout": 30}), ((tmp_path / "c" / (made.name + ".partial"),), {})]  # (it waits for a writer, up to half a minute)
    connections.clear()
    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):  # (the copy cannot be opened: the database that was is let go, and it is the reason that is said)
        backup.snapshot(db, tmp_path / "no" / "such" / "folder", now=WHEN)
    assert len(connections) == 1 and connections[0].closed
    connections.clear()
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a database " * 50)
    with pytest.raises(sqlite3.DatabaseError):
        backup.snapshot(bad, tmp_path / "c", now=WHEN)
    assert len(connections) == 2 and all(c.closed for c in connections)
    connections.clear()
    assert backup.check(made).ok
    assert len(connections) == 1 and connections[0].closed
    assert backup.check(bad).ok is False and len(connections) == 2 and connections[1].closed
    for connection in connections:  # (a copy is opened by its address, read only: never by a name that could be taken for something else, and never changed)
        args, kwargs = connection.asked
        assert kwargs == {"uri": True} and args[0].startswith("file:") and args[0].endswith("?mode=ro&immutable=1")
    connections.clear()
    backup.restore(made, tmp_path / "again.db", tmp_path / "kept", now=WHEN)
    assert len(connections) == 3 and all(c.closed for c in connections)  # (the look at the copy, the copy, the database)
    assert [c.asked[1].get("uri") for c in connections[:2]] == [True, True] and connections[2].asked[1] == {"timeout": 30}


def test_a_copy_that_opens_but_breaks_one_of_its_own_rules_says_so_and_what_layout_it_has(tmp_path: Path) -> None:
    path = tmp_path / "library-20260101-000000.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (a INTEGER, b INTEGER)")
    conn.execute("CREATE INDEX i ON t (a)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(n, 100 - n) for n in range(20)])
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("UPDATE sqlite_master SET sql = 'CREATE INDEX i ON t (b)' WHERE name = 'i'")  # (the index holds what it was made of, and says it is of something else)
    conn.commit()
    conn.close()
    report = backup.check(path)
    conn = sqlite3.connect(path)
    verdict = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    conn.close()
    assert len(verdict) > 1 and verdict != ["ok"]  # (SQLite says several things: it is the first that is passed on)
    assert report.ok is False and report.problem == f"it did not pass its check ({verdict[0]})" and report.schema == 7 and report.counts == {}


def test_a_half_written_copy_is_cleared_when_it_is_more_than_an_hour_old_to_the_second(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 2_000_000_000.0
    monkeypatch.setattr(backup, "time", SimpleNamespace(time=lambda: now))
    exactly, over = touch(tmp_path, "library-20260101-000000.db.partial"), touch(tmp_path, "library-20260102-000000.db.partial")
    os.utime(exactly, (now - backup.STALE_PARTIAL, now - backup.STALE_PARTIAL))
    os.utime(over, (now - backup.STALE_PARTIAL - 0.5, now - backup.STALE_PARTIAL - 0.5))
    backup.prune(tmp_path, keep=3)
    assert exactly.exists() and not over.exists()


def test_a_copy_that_is_already_gone_when_it_is_to_be_taken_away_is_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for number in range(3):
        touch(tmp_path, f"library-2026010{number}-000000.db", 1000 * (number + 1))
    real = backup.listing
    monkeypatch.setattr(backup, "listing", lambda folder, now=None: real(folder, now) + [backup.Copy(folder / "library-20250101-000000.db", 1, 1.0)])  # (listed, and not there)
    assert backup.prune(tmp_path, keep=2) == 2 and len(real(tmp_path)) == 2


def test_a_half_written_copy_that_is_already_gone_when_it_is_to_be_taken_away_is_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    name = "library-20260101-000000.db.partial"
    ghost = SimpleNamespace(name=name, path=str(tmp_path / name), stat=lambda: SimpleNamespace(st_mtime=0.0))
    real = os.scandir
    monkeypatch.setattr(backup.os, "scandir", lambda folder: [*real(folder), ghost])
    assert backup.prune(tmp_path, keep=3) == 0


def test_the_first_folder_is_made_with_the_folders_above_it(db: Path, tmp_path: Path) -> None:
    primary = tmp_path / "a" / "b" / "c" / "backups"
    outcomes = backup.run(db, primary, keep=3, now=WHEN)
    assert outcomes[0].problem is None and (primary / "library-20261126-173005.db").is_file()
    again = tmp_path / "x" / "y" / "backups"
    caught = backup.catch_up(outcomes[0].path, [again], again, keep=3)
    assert caught[0].problem is None and (again / "library-20261126-173005.db").is_file()


def test_what_a_restore_keeps_goes_in_a_folder_made_with_the_folders_above_it(db: Path, tmp_path: Path) -> None:
    (tmp_path / "copies").mkdir()
    made = backup.snapshot(db, tmp_path / "copies", now=WHEN)
    kept = backup.restore(made, db, tmp_path / "p" / "q" / "kept", now=WHEN)
    assert kept is not None and kept.parent == tmp_path / "p" / "q" / "kept" and kept.is_file()
