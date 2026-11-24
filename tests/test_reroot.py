"""A library copied somewhere else (a Windows PC's D:\\Music to a Pi's /srv/music/library) is told where it is: every path in the database
changes, every song keeps its number, rating and plays, and the next scan finds every song unchanged."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from musictoolkit.cli import app as cli_app
from musictoolkit.config import load_config
from musictoolkit.db.connection import connect
from musictoolkit.ingest import reroot
from musictoolkit.ingest.reroot import Mapping, RerootConflict, RerootError, apply_reroot, backup_database, plan_reroot, style_of
from musictoolkit.ingest.scanner import scan_library
from tests.conftest import make_track

# ---------------------------------------------------------------------------------------------------- where is a path now


def test_a_path_is_windows_when_it_has_a_drive_letter_or_is_a_network_share() -> None:
    assert [style_of(p) for p in ("D:\\Music", "d:/music", "\\\\nas\\music", " C:\\x ")] == ["windows"] * 4
    assert [style_of(p) for p in ("/srv/music", "//weird/but/posix", "music", "")] == ["posix"] * 4


@pytest.mark.parametrize("source,target,message", [
    ("", "/srv/music", "Both folders are needed."),
    ("D:\\Music", "   ", "Both folders are needed."),
    ("music", "/srv/music", r"^--from must be a whole path"),
    ("D:\\Music", "music/library", r"^--to must be a whole path"),
    ("D:\\Music", "d:/music/", "The two folders are the same"),
    ("/srv/music/", "/srv/music", "The two folders are the same"),
])
def test_two_folders_that_cannot_be_used_are_refused_in_words(source: str, target: str, message: str) -> None:
    with pytest.raises(RerootError, match=message):
        Mapping.of(source, target)


@pytest.mark.parametrize("source,target,old,new", [
    # a Windows library on a Pi
    ("D:\\Music", "/srv/music/library", "D:\\Music\\Artist\\Album\\01 - Song.flac", "/srv/music/library/Artist/Album/01 - Song.flac"),
    ("D:\\Music", "/srv/music/library", "D:\\Music", "/srv/music/library"),
    ("D:\\Music\\", "/srv/music/library/", "D:\\Music\\a.flac", "/srv/music/library/a.flac"),
    ("d:/music", "/srv/music/library", "D:\\MUSIC\\Artist\\a.flac", "/srv/music/library/Artist/a.flac"),  # (Windows ignores case and accepts either slash)
    ("D:\\Music", "/srv/music/library", "D:/Music/Artist/a.flac", "/srv/music/library/Artist/a.flac"),
    ("D:\\", "/srv/music", "D:\\a.flac", "/srv/music/a.flac"),
    ("\\\\nas\\share\\music", "/srv/music", "\\\\NAS\\share\\music\\x\\y.mp3", "/srv/music/x/y.mp3"),
    ("D:\\Music", "/srv/music/library", "D:\\Music\\Björk\\Homogenic\\01 – Hunter.flac", "/srv/music/library/Björk/Homogenic/01 – Hunter.flac"),
    # one Windows place to another
    ("D:\\Music", "E:\\Media\\Music", "D:\\Music\\a\\b.flac", "E:\\Media\\Music\\a\\b.flac"),
    ("D:\\Music", "E:/Media", "d:\\music\\a/b.flac", "E:\\Media\\a\\b.flac"),
    # a Pi library to a Windows PC, and one folder of a Pi to another
    ("/mnt/music", "D:\\Music", "/mnt/music/a/b.flac", "D:\\Music\\a\\b.flac"),
    ("/home/me/Music", "/srv/music/library", "/home/me/Music/a/b.flac", "/srv/music/library/a/b.flac"),
    ("/home/me/Music", "/srv/music/library", "/home/me/Music", "/srv/music/library"),
    ("/home/me/Music", "/srv/music/library", "/home/me/Music/a\\b.flac", "/srv/music/library/a\\b.flac"),  # (a backslash in a name there is part of the name)
    ("/", "/srv/data", "/a/b.flac", "/srv/data/a/b.flac"),
])
def test_a_path_under_the_old_folder_is_the_same_place_under_the_new_one(source: str, target: str, old: str, new: str) -> None:
    assert Mapping.of(source, target).map(old) == new


@pytest.mark.parametrize("source,target,value", [
    ("D:\\Music", "/srv/music/library", "D:\\Music2\\a.flac"),  # (starts like the folder, is not in it)
    ("D:\\Music", "/srv/music/library", "D:\\Musical"),
    ("D:\\Music", "/srv/music/library", "E:\\Music\\a.flac"),
    ("D:\\Music", "/srv/music/library", "/srv/music/library/a.flac"),
    ("D:\\Music", "/srv/music/library", ""),
    ("D:\\Music", "/srv/music/library", None),
    ("/home/me/Music", "/srv/music/library", "/home/me/Music2/a.flac"),
    ("/home/me/Music", "/srv/music/library", "/home/me/music/a.flac"),  # (case matters there)
    ("/home/me/Music", "/srv/music/library", "D:\\Music\\a.flac"),
    ("/", "/srv/data", "relative/a.flac"),
])
def test_a_path_that_is_not_inside_the_old_folder_is_left_alone(source: str, target: str, value: str | None) -> None:
    assert Mapping.of(source, target).map(value) is None


# ---------------------------------------------------------------------------------------------------- what would change


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "lib.db")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def _song(conn, path: str, **fields: object) -> int:
    columns = ", ".join(["file_path", *fields])
    marks = ", ".join("?" for _ in range(1 + len(fields)))
    return conn.execute(f"INSERT INTO tracks ({columns}) VALUES ({marks})", (path, *fields.values())).lastrowid


def _windows_library(conn) -> dict[str, int]:
    """A database as a Windows PC makes it: songs under D:\\Music, one on another drive, and the histories that name paths."""
    ids = {
        "a": _song(conn, "D:\\Music\\Artist\\Album\\01 - A.flac", rating=5, favorite=1, file_size=111, file_mtime=1700000000.25),
        "b": _song(conn, "D:\\Music\\Artist\\Album\\02 - B.flac", rating=3),
        "c": _song(conn, "d:\\music\\Other\\c.mp3"),
        "away": _song(conn, "E:\\Elsewhere\\away.mp3"),
    }
    conn.execute("INSERT INTO file_moves (batch_id, kind, track_id, old_path, new_path, moved_at) VALUES ('b1', 'organize', ?, ?, ?, 'now')",
                 (ids["a"], "D:\\Music\\Unsorted\\a.flac", "D:\\Music\\Artist\\Album\\01 - A.flac"))
    conn.execute("INSERT INTO file_moves (batch_id, kind, track_id, old_path, new_path, moved_at) VALUES ('b2', 'quarantine', ?, ?, ?, 'now')",
                 (ids["b"], "D:\\Music\\x.flac", "E:\\Parking\\x.flac"))
    conn.execute("INSERT INTO disc_rips (batch_id, album, artist, format, quality, accuracy, root, folder, cover_path, started_at) "
                 "VALUES ('r1', 'Alb', 'Art', 'flac', 'lossless', 'verified', 'D:\\Music', 'D:\\Music\\Art\\Alb', 'D:\\Music\\Art\\Alb\\cover.jpg', 'now')")
    rip = conn.execute("SELECT id FROM disc_rips").fetchone()[0]
    conn.execute("INSERT INTO disc_rip_tracks (rip_id, number, title, file_path) VALUES (?, 1, 'One', 'D:\\Music\\Art\\Alb\\01.flac')", (rip,))
    conn.execute("INSERT INTO conversions (batch_id, title, format, quality, destination, scheme, started_at) VALUES ('c1', 'T', 'opus', '128', 'D:\\Exports', 's', 'now')")
    conversion = conn.execute("SELECT id FROM conversions").fetchone()[0]
    conn.execute("INSERT INTO conversion_items (conversion_id, title, source, target, action) VALUES (?, 'T', 'D:\\Music\\Artist\\Album\\01 - A.flac', 'D:\\Exports\\a.opus', 'convert')", (conversion,))
    conn.execute("INSERT INTO disc_burns (batch_id, kind, title, started_at, output) VALUES ('u1', 'iso', 'Disc', 'now', 'D:\\Music\\disc.iso')")
    burn = conn.execute("SELECT id FROM disc_burns").fetchone()[0]
    conn.execute("INSERT INTO disc_burn_items (burn_id, position, title, source) VALUES (?, 1, 'One', 'D:\\Music\\Artist\\Album\\01 - A.flac')", (burn,))
    conn.execute("INSERT INTO play_history (track_id, source, played_at_epoch) VALUES (?, 'future_scrobble', 1700000000)", (ids["a"],))
    conn.commit()
    return ids


def test_the_plan_counts_every_column_and_changes_nothing(conn) -> None:
    _windows_library(conn)
    before = conn.execute("SELECT file_path FROM tracks ORDER BY id").fetchall()
    plan = plan_reroot(conn, Mapping.of("D:\\Music", "/srv/music/library"))
    assert plan.counts == {
        ("tracks", "file_path"): 3,
        ("file_moves", "old_path"): 2,
        ("file_moves", "new_path"): 1,
        ("disc_rips", "root"): 1,
        ("disc_rips", "folder"): 1,
        ("disc_rips", "cover_path"): 1,
        ("disc_rip_tracks", "file_path"): 1,
        ("conversion_items", "source"): 1,
        ("disc_burns", "output"): 1,
        ("disc_burn_items", "source"): 1,
    }  # (the conversion's destination, D:\Exports, and the parking place on E: are not under the library)
    assert (plan.songs, plan.songs_outside, plan.conflicts) == (4, 1, [])
    assert plan.examples(2) == [
        ("D:\\Music\\Artist\\Album\\01 - A.flac", "/srv/music/library/Artist/Album/01 - A.flac"),
        ("D:\\Music\\Artist\\Album\\02 - B.flac", "/srv/music/library/Artist/Album/02 - B.flac"),
    ]
    assert conn.execute("SELECT file_path FROM tracks ORDER BY id").fetchall() == before


def test_a_database_from_before_a_feature_has_no_table_for_it_and_is_planned_all_the_same(conn) -> None:
    _windows_library(conn)
    conn.execute("DROP TABLE conversion_items")
    conn.execute("DROP TABLE disc_burn_items")
    counts = plan_reroot(conn, Mapping.of("D:\\Music", "/srv/music/library")).counts
    assert ("conversion_items", "source") not in counts and counts[("tracks", "file_path")] == 3


def test_a_database_that_lacks_a_column_of_a_newer_feature_is_planned_all_the_same(conn) -> None:
    _windows_library(conn)
    conn.execute("ALTER TABLE disc_rips DROP COLUMN cover_path")
    counts = plan_reroot(conn, Mapping.of("D:\\Music", "/srv/music/library")).counts
    assert ("disc_rips", "cover_path") not in counts and counts[("disc_rips", "folder")] == 1 and counts[("tracks", "file_path")] == 3


def test_an_empty_path_is_nobodys_even_when_the_old_folder_is_the_whole_disk(conn) -> None:
    assert Mapping.of("/", "/srv/data").map("") is None
    _song(conn, "/a/b.flac")
    conn.execute("INSERT INTO disc_rips (batch_id, album, artist, format, quality, accuracy, root, folder, cover_path, started_at) "
                 "VALUES ('r1', 'Alb', 'Art', 'flac', 'lossless', 'verified', '/a', '', '', 'now')")
    plan = plan_reroot(conn, Mapping.of("/", "/srv/data"))
    assert ("disc_rips", "folder") not in plan.counts and ("disc_rips", "cover_path") not in plan.counts
    assert plan.counts[("disc_rips", "root")] == 1 and plan.counts[("tracks", "file_path")] == 1


def test_a_library_that_is_empty_or_elsewhere_has_nothing_to_change(conn) -> None:
    plan = plan_reroot(conn, Mapping.of("D:\\Music", "/srv/music"))
    assert (plan.changes, plan.songs, plan.songs_outside) == ([], 0, 0)
    _song(conn, "E:\\Other\\a.mp3")
    plan = plan_reroot(conn, Mapping.of("D:\\Music", "/srv/music"))
    assert (plan.changes, plan.songs, plan.songs_outside) == ([], 1, 1)


def test_a_song_that_would_get_a_path_another_song_already_has_is_a_conflict_and_is_not_changed(conn) -> None:
    taken = _song(conn, "/srv/music/library/a.flac")
    mine = _song(conn, "D:\\Music\\a.flac")
    fine = _song(conn, "D:\\Music\\b.flac")
    plan = plan_reroot(conn, Mapping.of("D:\\Music", "/srv/music/library"))
    assert plan.conflicts == [("D:\\Music\\a.flac", "/srv/music/library/a.flac")]
    assert [(row, new) for _, _, row, _, new in plan.changes] == [(fine, "/srv/music/library/b.flac")]
    assert (plan.songs, plan.songs_outside) == (3, 1) and taken != mine


def test_two_songs_that_would_land_on_the_same_path_are_a_conflict_for_the_second(conn) -> None:
    _song(conn, "D:\\Music\\a.flac")
    _song(conn, "D:/Music/a.flac")  # (the same place spelled with the other slash)
    plan = plan_reroot(conn, Mapping.of("D:\\Music", "/srv/music"))
    assert len(plan.changes) == 1 and plan.conflicts == [("D:/Music/a.flac", "/srv/music/a.flac")]


def test_moving_a_library_into_a_folder_of_itself_works_for_every_song(conn) -> None:
    ids = [_song(conn, f"/srv/music/{n}.flac") for n in "ab"]
    plan = plan_reroot(conn, Mapping.of("/srv/music", "/srv/music/library"))
    assert plan.conflicts == [] and [(row, new) for _, _, row, _, new in plan.changes] == [(ids[0], "/srv/music/library/a.flac"), (ids[1], "/srv/music/library/b.flac")]


# ---------------------------------------------------------------------------------------------------- making the change


def _paths(db: Path, table: str, column: str) -> list[str | None]:
    with sqlite3.connect(db) as raw:
        return [row[0] for row in raw.execute(f"SELECT {column} FROM {table} ORDER BY rowid")]


def test_the_change_is_made_in_every_column_and_each_song_keeps_everything_else(tmp_path: Path, conn) -> None:
    ids = _windows_library(conn)
    conn.commit()
    plan = plan_reroot(conn, Mapping.of("D:\\Music", "/srv/music/library"))
    copy = apply_reroot(tmp_path / "lib.db", plan)
    db = tmp_path / "lib.db"
    assert _paths(db, "tracks", "file_path") == [
        "/srv/music/library/Artist/Album/01 - A.flac", "/srv/music/library/Artist/Album/02 - B.flac", "/srv/music/library/Other/c.mp3", "E:\\Elsewhere\\away.mp3",
    ]
    assert _paths(db, "file_moves", "old_path") == ["/srv/music/library/Unsorted/a.flac", "/srv/music/library/x.flac"]
    assert _paths(db, "file_moves", "new_path") == ["/srv/music/library/Artist/Album/01 - A.flac", "E:\\Parking\\x.flac"]
    assert _paths(db, "disc_rips", "root") == ["/srv/music/library"]
    assert _paths(db, "disc_rips", "folder") == ["/srv/music/library/Art/Alb"]
    assert _paths(db, "disc_rips", "cover_path") == ["/srv/music/library/Art/Alb/cover.jpg"]
    assert _paths(db, "disc_rip_tracks", "file_path") == ["/srv/music/library/Art/Alb/01.flac"]
    assert _paths(db, "conversions", "destination") == ["D:\\Exports"]
    assert _paths(db, "conversion_items", "source") == ["/srv/music/library/Artist/Album/01 - A.flac"]
    assert _paths(db, "conversion_items", "target") == ["D:\\Exports\\a.opus"]
    assert _paths(db, "disc_burns", "output") == ["/srv/music/library/disc.iso"]
    assert _paths(db, "disc_burn_items", "source") == ["/srv/music/library/Artist/Album/01 - A.flac"]
    with sqlite3.connect(db) as raw:
        row = raw.execute("SELECT id, rating, favorite, file_size, file_mtime FROM tracks WHERE id = ?", (ids["a"],)).fetchone()
        assert row == (ids["a"], 5, 1, 111, 1700000000.25)  # (the same song: its number, rating, favorite, size and time)
        assert raw.execute("SELECT track_id FROM play_history").fetchone()[0] == ids["a"]
        assert raw.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 4
    assert copy.parent == tmp_path and copy.name.startswith("lib.db.before-reroot-")


def test_the_copy_made_first_is_a_whole_database_as_it_was(tmp_path: Path, conn) -> None:
    _windows_library(conn)
    copy = apply_reroot(tmp_path / "lib.db", plan_reroot(conn, Mapping.of("D:\\Music", "/srv/music/library")))
    assert _paths(copy, "tracks", "file_path")[0] == "D:\\Music\\Artist\\Album\\01 - A.flac"
    with sqlite3.connect(copy) as raw:
        assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_the_copy_is_named_for_the_time_it_was_made(tmp_path: Path, conn) -> None:
    conn.commit()
    copy = backup_database(tmp_path / "lib.db", datetime(2026, 11, 23, 9, 5, 7))
    assert copy == tmp_path / "lib.db.before-reroot-20261123-090507" and copy.is_file()


def test_a_conflict_stops_everything_and_makes_no_copy_unless_told_to_leave_those_songs(tmp_path: Path, conn) -> None:
    _song(conn, "/srv/music/library/a.flac")
    _song(conn, "D:\\Music\\a.flac")
    _song(conn, "D:\\Music\\b.flac")
    conn.commit()
    plan = plan_reroot(conn, Mapping.of("D:\\Music", "/srv/music/library"))
    with pytest.raises(RerootConflict, match=r"^1 songs would get a path that another song already has\.$"):
        apply_reroot(tmp_path / "lib.db", plan)
    assert _paths(tmp_path / "lib.db", "tracks", "file_path") == ["/srv/music/library/a.flac", "D:\\Music\\a.flac", "D:\\Music\\b.flac"]
    assert [p.name for p in tmp_path.iterdir() if "before-reroot" in p.name] == []
    apply_reroot(tmp_path / "lib.db", plan, skip_conflicts=True)
    assert _paths(tmp_path / "lib.db", "tracks", "file_path") == ["/srv/music/library/a.flac", "D:\\Music\\a.flac", "/srv/music/library/b.flac"]


def test_a_failure_part_way_changes_nothing(tmp_path: Path, conn) -> None:
    _windows_library(conn)
    conn.execute("CREATE TRIGGER boom BEFORE UPDATE ON disc_rips BEGIN SELECT RAISE(ABORT, 'boom'); END")
    conn.commit()
    plan = plan_reroot(conn, Mapping.of("D:\\Music", "/srv/music/library"))
    with pytest.raises(sqlite3.DatabaseError, match="boom"):
        apply_reroot(tmp_path / "lib.db", plan)
    assert _paths(tmp_path / "lib.db", "tracks", "file_path")[0] == "D:\\Music\\Artist\\Album\\01 - A.flac"  # (the songs were changed first, and are back)
    assert _paths(tmp_path / "lib.db", "file_moves", "old_path")[0] == "D:\\Music\\Unsorted\\a.flac"
    assert any("before-reroot" in p.name for p in tmp_path.iterdir())  # (the copy is there)


def test_doing_it_again_finds_nothing_left_to_change(tmp_path: Path, conn) -> None:
    _windows_library(conn)
    mapping = Mapping.of("D:\\Music", "/srv/music/library")
    apply_reroot(tmp_path / "lib.db", plan_reroot(conn, mapping))
    assert plan_reroot(conn, mapping).changes == []


def test_the_folders_of_the_settings_that_are_under_the_old_place_are_found() -> None:
    mapping = Mapping.of("D:\\Music", "/srv/music/library")
    assert reroot.map_folders(["D:\\Music", "E:\\Other", "d:\\music\\Sub", ""], mapping) == [
        ("D:\\Music", "/srv/music/library"), ("d:\\music\\Sub", "/srv/music/library/Sub"),
    ]


# ---------------------------------------------------------------------------------------------------- the point of it all


def test_after_the_change_a_scan_of_the_copied_files_finds_every_song_unchanged_and_nothing_is_lost(tmp_path: Path) -> None:
    here = tmp_path.resolve() / "pi" / "library"
    for n, title in enumerate(["One", "Two", "Three"], start=1):
        make_track(here / "Artist" / "Album", f"{n:02d} - {title}.mp3", title=title, artist="Artist", album="Album", tracknumber=n)
    db = tmp_path / "lib.db"
    conn = connect(db)
    conn.row_factory = sqlite3.Row
    assert scan_library(conn, here).added == 3
    ids = [row["id"] for row in conn.execute("SELECT id FROM tracks ORDER BY id")]
    conn.execute("UPDATE tracks SET rating = 4, favorite = 1 WHERE id = ?", (ids[0],))
    conn.execute("INSERT INTO play_history (track_id, source, played_at_epoch) VALUES (?, 'future_scrobble', 1700000000)", (ids[0],))
    conn.execute("INSERT INTO playlists (name) VALUES ('Mix')")
    conn.commit()
    # this is what the database looked like on the PC the files were copied from: the same songs under D:\Music
    for song_id, path in [(row["id"], row["file_path"]) for row in conn.execute("SELECT id, file_path FROM tracks")]:
        conn.execute("UPDATE tracks SET file_path = ? WHERE id = ?", ("D:\\Music\\" + str(Path(path).relative_to(here)).replace("/", "\\"), song_id))
    conn.commit()
    assert conn.execute("SELECT file_path FROM tracks WHERE id = ?", (ids[0],)).fetchone()[0] == "D:\\Music\\Artist\\Album\\01 - One.mp3"

    apply_reroot(db, plan_reroot(conn, Mapping.of("D:\\Music", str(here))))
    conn.close()
    conn = connect(db)
    conn.row_factory = sqlite3.Row
    result = scan_library(conn, here)

    assert (result.unchanged, result.added, result.updated, result.missing) == (3, 0, 0, 0)  # (no tag was read: the size and time are what they were)
    assert [row["id"] for row in conn.execute("SELECT id FROM tracks ORDER BY id")] == ids
    song = conn.execute("SELECT rating, favorite, is_missing FROM tracks WHERE id = ?", (ids[0],)).fetchone()
    assert (song["rating"], song["favorite"], song["is_missing"]) == (4, 1, 0)
    assert conn.execute("SELECT COUNT(*) FROM play_history WHERE track_id = ?", (ids[0],)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0] == 1
    conn.close()


# ---------------------------------------------------------------------------------------------------- the command


def _command_setup(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / "config.toml"
    config.write_text(
        '[library]\nroots = ["D:\\\\Music", "E:\\\\Elsewhere"]\n[disc]\nrip_root = "D:\\\\Music"\n[database]\npath = "./data/library.db"\n[logging]\ndir = "./logs"\n',
        encoding="utf-8",
    )
    conn = connect(tmp_path / "data" / "library.db")
    conn.row_factory = sqlite3.Row
    _windows_library(conn)
    conn.close()
    return config, tmp_path / "data" / "library.db"


def test_the_command_says_what_it_would_do_and_does_not_do_it(tmp_path: Path) -> None:
    config, db = _command_setup(tmp_path)
    result = CliRunner().invoke(cli_app, ["--config", str(config), "library", "reroot", "--from", "D:\\Music", "--to", "/srv/music/library"])
    assert result.exit_code == 0, result.output
    assert "Dry run of re-rooting D:\\Music -> /srv/music/library" in result.output
    assert "tracks.file_path: 3" in result.output and "songs under the old folder: 3 of 4  (outside it, left alone: 1)" in result.output
    assert "e.g. D:\\Music\\Artist\\Album\\01 - A.flac  ->  /srv/music/library/Artist/Album/01 - A.flac" in result.output
    assert "config folder: D:\\Music  ->  /srv/music/library   (use --update-config, or Settings, to change it)" in result.output
    assert "Nothing was changed. Add --apply" in result.output
    assert _paths(db, "tracks", "file_path")[0].startswith("D:\\Music")
    assert not [p for p in db.parent.iterdir() if "before-reroot" in p.name]


def test_the_command_with_apply_changes_the_database_keeps_a_copy_and_says_what_next(tmp_path: Path) -> None:
    config, db = _command_setup(tmp_path)
    result = CliRunner().invoke(cli_app, ["--config", str(config), "library", "reroot", "--from", "D:\\Music", "--to", "/srv/music/library", "--apply"])
    assert result.exit_code == 0, result.output
    assert _paths(db, "tracks", "file_path")[0] == "/srv/music/library/Artist/Album/01 - A.flac"
    (copy,) = [p for p in db.parent.iterdir() if "before-reroot" in p.name]
    assert f"Done: 13 paths now point to /srv/music/library. The database as it was is kept in {copy}." in result.output
    assert "Next: make sure [library] roots names /srv/music/library, then run `mtk scan /srv/music/library`" in result.output
    assert load_config(config).library.roots == ["D:\\Music", "E:\\Elsewhere"]  # (the config is only touched when asked)


def test_the_command_can_change_the_folders_in_the_config_too(tmp_path: Path) -> None:
    config, db = _command_setup(tmp_path)
    result = CliRunner().invoke(cli_app, ["--config", str(config), "library", "reroot", "--from", "D:\\Music", "--to", "/srv/music/library", "--apply", "--update-config"])
    assert result.exit_code == 0, result.output
    changed = load_config(config)
    assert changed.library.roots == ["/srv/music/library", "E:\\Elsewhere"] and changed.disc.rip_root == "/srv/music/library"
    assert "config.toml now names the new folders" in result.output


def test_the_rip_folder_is_changed_too_when_it_is_inside_the_old_place_without_being_a_library_folder_itself(tmp_path: Path) -> None:
    config, db = _command_setup(tmp_path)
    config.write_text(config.read_text(encoding="utf-8").replace('rip_root = "D:\\\\Music"', 'rip_root = "D:\\\\Music\\\\Rips"'), encoding="utf-8")
    result = CliRunner().invoke(cli_app, ["--config", str(config), "library", "reroot", "--from", "D:\\Music", "--to", "/srv/music/library", "--apply", "--update-config"])
    assert result.exit_code == 0, result.output
    assert "config folder: D:\\Music\\Rips  ->  /srv/music/library/Rips" in result.output
    assert load_config(config).disc.rip_root == "/srv/music/library/Rips"


def test_the_config_is_left_exactly_as_it_was_when_none_of_its_folders_is_under_the_old_place(tmp_path: Path) -> None:
    config, db = _command_setup(tmp_path)
    config.write_text('# my notes stay\n[library]\nroots = ["E:\\\\Elsewhere"]\n[disc]\nrip_root = "E:\\\\Elsewhere"\n[database]\npath = "./data/library.db"\n[logging]\ndir = "./logs"\n', encoding="utf-8")
    before = config.read_bytes()
    result = CliRunner().invoke(cli_app, ["--config", str(config), "library", "reroot", "--from", "D:\\Music", "--to", "/srv/music/library", "--apply", "--update-config"])
    assert result.exit_code == 0, result.output
    assert config.read_bytes() == before and "config.toml now names the new folders" not in result.output
    assert "config folder:" not in result.output


def test_the_command_stops_at_a_conflict_and_leaves_exactly_those_songs_when_told_to(tmp_path: Path) -> None:
    config, db = _command_setup(tmp_path)
    conn = connect(db)
    _song(conn, "/srv/music/library/Other/c.mp3")
    conn.commit()
    conn.close()
    args = ["--config", str(config), "library", "reroot", "--from", "D:\\Music", "--to", "/srv/music/library"]
    dry = CliRunner().invoke(cli_app, args)
    assert dry.exit_code == 1 and "1 songs would get a path that another song already has, for example d:\\music\\Other\\c.mp3 -> /srv/music/library/Other/c.mp3" in dry.output
    stopped = CliRunner().invoke(cli_app, [*args, "--apply"])
    assert stopped.exit_code == 1 and "Nothing was changed: 1 songs would get a path that another song already has." in stopped.output
    assert _paths(db, "tracks", "file_path")[0] == "D:\\Music\\Artist\\Album\\01 - A.flac"
    allowed = CliRunner().invoke(cli_app, [*args, "--apply", "--skip-conflicts"])
    assert allowed.exit_code == 0, allowed.output
    assert "d:\\music\\Other\\c.mp3" in _paths(db, "tracks", "file_path") and _paths(db, "tracks", "file_path")[0] == "/srv/music/library/Artist/Album/01 - A.flac"
    assert CliRunner().invoke(cli_app, [*args, "--skip-conflicts"]).exit_code == 0  # (a dry run that will be allowed to go on is not an error)


@pytest.mark.parametrize("args,words", [
    (["--from", "music", "--to", "/srv/music"], "--from must be a whole path"),
    (["--from", "D:\\Music", "--to", "d:/music"], "The two folders are the same"),
    (["--from", "", "--to", "/srv/music"], "Both folders are needed."),
])
def test_the_command_refuses_folders_that_cannot_be_used(tmp_path: Path, args: list[str], words: str) -> None:
    config, db = _command_setup(tmp_path)
    result = CliRunner().invoke(cli_app, ["--config", str(config), "library", "reroot", *args, "--apply"])
    assert result.exit_code == 2 and words in result.output
    assert _paths(db, "tracks", "file_path")[0].startswith("D:\\Music")


# ---------------------------------------------------------------------------------------------------- a Windows user folder (not D:\Music)

USER_MUSIC = "C:\\Users\\you\\Music"


@pytest.mark.parametrize("source", [USER_MUSIC, USER_MUSIC + "\\", USER_MUSIC + "\\\\", "C:/Users/you/Music", "c:\\USERS\\You\\music", " C:\\Users\\you\\Music "])
def test_a_windows_user_folder_is_the_place_however_it_is_written(source: str) -> None:
    mapping = Mapping.of(source, "/srv/music/library")
    inside = {
        "C:\\Users\\you\\Music\\Beyoncé\\Lemonade\\01 - Pray You Catch Me.mp3": "/srv/music/library/Beyoncé/Lemonade/01 - Pray You Catch Me.mp3",
        "c:/users/YOU/music/Sigur Rós/( )/01 Untitled #1.mp3": "/srv/music/library/Sigur Rós/( )/01 Untitled #1.mp3",  # (the rest keeps the spelling the database has)
        "C:\\Users\\you\\Music\\Guns N' Roses\\Appetite & Destruction\\01 - It's So Easy.flac": "/srv/music/library/Guns N' Roses/Appetite & Destruction/01 - It's So Easy.flac",
        "C:\\Users\\you\\Music\\song.mp3": "/srv/music/library/song.mp3",
        "C:\\Users\\you\\Music": "/srv/music/library",
    }
    for old, new in inside.items():
        assert mapping.map(old) == new, old
    for outside in (
        "C:\\Users\\you\\Music2\\a.mp3", "C:\\Users\\you\\Musical\\a.mp3", "C:\\Users\\you\\MusicOld\\a.mp3", "C:\\Users\\you\\Documents\\Music\\a.mp3",
        "D:\\Users\\you\\Music\\a.mp3", "C:\\Users\\you2\\Music\\a.mp3", "C:\\Users\\you\\Music.mp3", "/srv/music/library/a.mp3", "", None,
    ):
        assert mapping.map(outside) is None, outside


def _user_folder_library(conn) -> list[str]:
    paths = [
        "C:\\Users\\you\\Music\\Beyoncé\\Lemonade\\01 - Pray You Catch Me.mp3",
        "C:\\Users\\you\\Music\\Sigur Rós\\( )\\01 Untitled #1.mp3",
        "C:\\Users\\you\\Music\\Guns N' Roses\\Appetite & Destruction\\01 - It's So Easy.mp3",
    ]
    for path in paths:
        _song(conn, path)
    conn.commit()
    return paths


def test_every_song_of_a_library_in_a_windows_user_folder_lands_in_the_new_library_and_the_scan_finds_it_unchanged(tmp_path: Path) -> None:
    here = tmp_path / "srv" / "music" / "library"
    relative = ["Beyoncé/Lemonade/01 - Pray You Catch Me.mp3", "Sigur Rós/( )/01 Untitled #1.mp3", "Guns N' Roses/Appetite & Destruction/01 - It's So Easy.mp3"]
    db = tmp_path / "pc.db"
    conn = connect(db)
    conn.row_factory = sqlite3.Row
    ids = []
    for name in relative:
        song = make_track(here / Path(name).parent, Path(name).name, title=Path(name).stem)
        stat = song.stat()
        ids.append(_song(conn, "C:\\Users\\you\\Music\\" + name.replace("/", "\\"), file_size=stat.st_size, file_mtime=stat.st_mtime, title=Path(name).stem))
    conn.commit()
    plan = plan_reroot(conn, Mapping.of(USER_MUSIC, str(here)))
    assert (len(plan.changes), plan.conflicts, plan.songs_outside) == (3, [], 0)
    conn.close()
    apply_reroot(db, plan)
    conn = connect(db)
    conn.row_factory = sqlite3.Row
    result = scan_library(conn, here)
    assert (result.unchanged, result.added, result.updated, result.missing) == (3, 0, 0, 0)
    assert [r["id"] for r in conn.execute("SELECT id FROM tracks ORDER BY id")] == ids
    assert {Path(r["file_path"]).relative_to(here).as_posix() for r in conn.execute("SELECT file_path FROM tracks")} == set(relative)
    conn.close()


# ---------------------------------------------------------------------------------------------------- where the database has its songs


def _folders(tmp_path: Path, *paths: str, missing: tuple[str, ...] = ()) -> list[tuple[str, int]]:
    conn = connect(tmp_path / "lib.db")
    conn.row_factory = sqlite3.Row
    for path in paths:
        _song(conn, path)
    for path in missing:
        _song(conn, path, is_missing=1)
    conn.commit()
    try:
        return reroot.song_folders(conn)
    finally:
        conn.close()


def test_the_folders_that_hold_the_songs_are_the_deepest_ones_that_hold_nearly_all_of_them(tmp_path: Path) -> None:
    songs = [f"C:\\Users\\you\\Music\\Artist {n % 20}\\Album {n % 3}\\{n:02d}.mp3" for n in range(40)]
    assert _folders(tmp_path, *songs) == [("C:\\Users\\you\\Music", 40)]


def test_a_folder_that_holds_less_than_nine_in_ten_is_where_it_stops(tmp_path: Path) -> None:
    nine = [f"D:\\Music\\Rock\\a{n}.mp3" for n in range(9)] + ["D:\\Music\\Jazz\\b.mp3"]
    assert _folders(tmp_path, *nine) == [("D:\\Music\\Rock", 10)]  # (nine of ten: the one that is not is not enough to stop it)
    (tmp_path / "lib.db").unlink()
    eight = [f"D:\\Music\\Rock\\a{n}.mp3" for n in range(8)] + ["D:\\Music\\Jazz\\b.mp3", "D:\\Music\\Jazz\\c.mp3"]
    assert _folders(tmp_path, *eight) == [("D:\\Music", 10)]  # (eight of ten: two ways, so this is the folder)


def test_songs_that_sit_in_the_folder_itself_count_against_going_deeper(tmp_path: Path) -> None:
    songs = ["D:\\Music\\loose1.mp3", "D:\\Music\\loose2.mp3"] + [f"D:\\Music\\Album\\{n}.mp3" for n in range(8)]
    assert _folders(tmp_path, *songs) == [("D:\\Music", 10)]  # (eight of ten go one way: not enough, because two are here)


def test_one_drive_is_one_place_however_its_letter_and_folders_were_cased_and_the_first_spelling_is_shown(tmp_path: Path) -> None:
    assert _folders(tmp_path, "C:\\Users\\You\\Music\\a\\1.mp3", "c:/users/you/music/b/2.mp3", "C:\\USERS\\YOU\\MUSIC\\c\\3.mp3") == [("C:\\Users\\You\\Music", 3)]


def test_each_drive_share_and_linux_root_is_a_place_of_its_own_the_biggest_first_and_at_most_four(tmp_path: Path) -> None:
    found = _folders(
        tmp_path,
        *[f"C:\\Users\\you\\Music\\x{n}.mp3" for n in range(5)],
        "E:\\Rips\\A\\1.flac", "E:\\Rips\\B\\2.flac",
        *[f"\\\\nas\\media\\music\\A\\{n}.mp3" for n in range(3)],
        "/srv/music/library/a/1.mp3", "/srv/music/library/b/2.mp3", "/srv/music/library/b/3.mp3", "/srv/other/x.mp3",
        "Z:\\Odd\\1.mp3",
    )
    assert found == [("C:\\Users\\you\\Music", 5), ("/srv", 4), ("\\\\nas\\media\\music\\A", 3), ("E:\\Rips", 2)]
    # (the four Linux songs are one place, "/", and not nine in ten go the same way past /srv, so that is its folder; the fifth place, one song on Z:, is cut off by the limit)


def test_missing_songs_and_an_empty_database_say_nothing(tmp_path: Path) -> None:
    assert _folders(tmp_path, "D:\\Music\\a.mp3", missing=("E:\\Gone\\b.mp3",)) == [("D:\\Music", 1)]
    (tmp_path / "lib.db").unlink()
    assert _folders(tmp_path) == []


def test_a_song_directly_in_the_root_of_a_drive_or_of_the_disk_is_found_too(tmp_path: Path) -> None:
    assert _folders(tmp_path, "D:\\a.mp3", "/b.mp3") == [("/", 1), ("D:\\", 1)]


# ---------------------------------------------------------------------------------------------------- the command, when --from is not what the database has


def test_the_command_says_where_the_songs_are_when_some_of_them_are_elsewhere(tmp_path: Path) -> None:
    config, db = _command_setup(tmp_path)
    result = CliRunner().invoke(cli_app, ["--config", str(config), "library", "reroot", "--from", "D:\\Music", "--to", "/srv/music/library"])
    assert result.exit_code == 0, result.output
    assert "the database has its songs in: D:\\Music (3), E:\\Elsewhere (1)" in result.output


def test_the_command_says_nothing_of_where_the_songs_are_when_all_of_them_are_in_the_folder(tmp_path: Path) -> None:
    config, db = _command_setup(tmp_path)
    conn = connect(db)
    conn.execute("DELETE FROM tracks WHERE file_path LIKE 'E:%'")
    conn.commit()
    conn.close()
    result = CliRunner().invoke(cli_app, ["--config", str(config), "library", "reroot", "--from", "D:\\Music", "--to", "/srv/music/library"])
    assert result.exit_code == 0 and "the database has its songs in" not in result.output


@pytest.mark.parametrize("apply", [False, True])
def test_a_folder_that_holds_no_song_is_an_error_that_names_the_folders_that_do_and_changes_nothing(tmp_path: Path, apply: bool) -> None:
    config, db = _command_setup(tmp_path)
    conn = connect(db)
    conn.execute("DELETE FROM tracks WHERE file_path NOT LIKE 'D:%' AND file_path NOT LIKE 'd:%'")
    conn.execute("UPDATE tracks SET file_path = REPLACE(REPLACE(file_path, 'D:\\Music', 'C:\\Users\\you\\Music'), 'd:\\music', 'C:\\Users\\you\\Music')")
    conn.commit()
    conn.close()
    before = _paths(db, "tracks", "file_path")
    args = ["--config", str(config), "library", "reroot", "--from", "D:\\Music", "--to", "/srv/music/library", *(["--apply"] if apply else [])]
    result = CliRunner().invoke(cli_app, args)
    assert result.exit_code == 1, result.output
    assert "the database has its songs in: C:\\Users\\you\\Music (3)" in result.output
    assert "No song is in D:\\Music, so there is nothing to change. Use one of the folders above as --from." in result.output
    assert "Nothing was changed" not in result.output and "Done:" not in result.output
    assert _paths(db, "tracks", "file_path") == before
    assert not [p for p in db.parent.iterdir() if "before-reroot" in p.name]  # (no copy of the database either: nothing was going to be done)
    fixed = CliRunner().invoke(cli_app, [*args[:5], "C:\\Users\\you\\Music", *args[6:]])
    assert fixed.exit_code == 0, fixed.output
    assert "songs under the old folder: 3 of 3" in fixed.output


def test_a_database_with_no_songs_at_all_is_told_there_is_nothing_to_change_without_a_hint(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[database]\npath = "./data/library.db"\n[logging]\ndir = "./logs"\n', encoding="utf-8")
    connect(tmp_path / "data" / "library.db").close()
    result = CliRunner().invoke(cli_app, ["--config", str(config), "library", "reroot", "--from", "D:\\Music", "--to", "/srv/music/library"])
    assert result.exit_code == 1 and "No song is in D:\\Music, so there is nothing to change." in result.output
    assert "Use one of the folders" not in result.output and "the database has its songs in" not in result.output


def test_a_doubled_separator_is_one_separator_on_every_kind_of_path(tmp_path: Path) -> None:
    assert _folders(tmp_path, "D:\\Music\\\\Rock\\a.mp3", "D:\\Music\\Rock\\b.mp3", "D:/Music//Rock/c.mp3") == [("D:\\Music\\Rock", 3)]
    (tmp_path / "lib.db").unlink()
    assert _folders(tmp_path, "\\\\nas3\\share\\\\a\\1.mp3", "\\\\nas3\\share\\a\\2.mp3") == [("\\\\nas3\\share\\a", 2)]
    (tmp_path / "lib.db").unlink()
    assert _folders(tmp_path, "/srv//music/a.mp3", "/srv/music//b.mp3") == [("/srv/music", 2)]


def test_a_share_with_one_song_is_the_folder_of_the_song_and_not_the_song(tmp_path: Path) -> None:
    assert _folders(tmp_path, "\\\\nas2\\share\\x\\only.mp3") == [("\\\\nas2\\share\\x", 1)]
    (tmp_path / "lib.db").unlink()
    assert _folders(tmp_path, "D:\\Music\\only.mp3") == [("D:\\Music", 1)]
    (tmp_path / "lib.db").unlink()
    assert _folders(tmp_path, "/srv/music/only.mp3") == [("/srv/music", 1)]


def test_linux_folders_are_told_apart_by_case_and_joined_with_slashes(tmp_path: Path) -> None:
    assert _folders(tmp_path, "/srv/Music/a/1.mp3", "/srv/music/b/2.mp3") == [("/srv", 2)]  # (two folders: Linux names do not ignore case)
    (tmp_path / "lib.db").unlink()
    deep = [f"/srv/music/library/Artist {n}/Album/{n}.mp3" for n in range(5)]
    assert _folders(tmp_path, *deep) == [("/srv/music/library", 5)]


def test_the_folder_that_nearly_all_go_through_is_followed_with_those_songs_only(tmp_path: Path) -> None:
    rock = [f"D:\\Music\\Rock\\Metal\\{n}.mp3" for n in range(9)] + ["D:\\Music\\Rock\\Punk\\x.mp3"]
    assert _folders(tmp_path, *rock, "D:\\Music\\Jazz\\Bebop\\y.mp3") == [("D:\\Music\\Rock\\Metal", 11)]  # (ten of eleven are Rock: and nine of those ten are Metal)
