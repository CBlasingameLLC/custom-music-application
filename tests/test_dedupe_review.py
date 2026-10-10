"""The duplicate finder's safety net: keep the best copy, park the rest, hand over what was attached, undo it all."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from musictoolkit.db.connection import connect
from musictoolkit.ingest import cleanup, dedupe, moves
from musictoolkit.ingest.scanner import QUARANTINE_DIR, scan_library
from tests.conftest import make_track

JPEG = b"\xff\xd8\xff\xe0 pretend cover"


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = (tmp_path / "library").resolve()
    root.mkdir()
    return root


def add(conn, path: Path, content: bytes = b"audio", **fields) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    columns = {"artist": "Band", "title": "Song", "duration_seconds": 180.0, "format": path.suffix.lstrip("."), "bitrate": 128_000,
               "file_size": len(content), "is_missing": 0, **fields}
    names = ", ".join(["file_path", *columns])
    conn.execute(f"INSERT INTO tracks ({names}) VALUES ({', '.join('?' * (len(columns) + 1))})", [str(path), *columns.values()])
    conn.commit()
    return conn.execute("SELECT id FROM tracks WHERE file_path = ?", (str(path),)).fetchone()["id"]


def path_of(conn, track_id: int) -> Path:
    return Path(conn.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,)).fetchone()["file_path"])


def state_of(conn, track_id: int) -> int:
    return conn.execute("SELECT is_missing FROM tracks WHERE id = ?", (track_id,)).fetchone()["is_missing"]


def play(conn, track_id: int, n: int = 1) -> None:
    for i in range(n):
        conn.execute(
            "INSERT INTO play_history (track_id, source, played_at_epoch, raw_artist_name, raw_track_name) VALUES (?, 'future_scrobble', ?, 'Band', 'Song')",
            (track_id, 1_700_000_000 + track_id * 1000 + i),
        )
    conn.commit()


def playlist(conn, name: str, track_ids: list[int]) -> int:
    conn.execute("INSERT INTO playlists (name, source) VALUES (?, 'manual')", (name,))
    pid = conn.execute("SELECT id FROM playlists WHERE name = ?", (name,)).fetchone()["id"]
    for position, track_id in enumerate(track_ids):
        conn.execute("INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)", (pid, track_id, position))
    conn.commit()
    return pid


def members(conn, pid: int) -> list[int]:
    return [r["track_id"] for r in conn.execute("SELECT track_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (pid,))]


def plays_of(conn, track_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM play_history WHERE track_id = ?", (track_id,)).fetchone()[0]


def choose(conn, library: Path, keeper: int, *dups: int):
    return dedupe.quarantine(conn, [(keeper, list(dups))], [library])


# ------------------------------------------------------------------------------------------- choosing the keeper


def test_lossless_beats_a_higher_bitrate_lossy_copy(conn, library) -> None:
    mp3 = add(conn, library / "a.mp3", format="mp3", bitrate=320_000)
    flac = add(conn, library / "a.flac", format="flac", bitrate=900_000)
    assert dedupe._pick_keeper(conn, [mp3, flac]) == flac


def test_then_bitrate_then_size_then_tags_then_plays_then_age(conn, library) -> None:
    low = add(conn, library / "low.mp3", b"x" * 10, bitrate=128_000)
    high = add(conn, library / "high.mp3", b"x" * 10, bitrate=320_000)
    assert dedupe._pick_keeper(conn, [low, high]) == high

    small = add(conn, library / "small.mp3", b"x" * 10, bitrate=192_000)
    big = add(conn, library / "big.mp3", b"x" * 99, bitrate=192_000)
    assert dedupe._pick_keeper(conn, [small, big]) == big

    bare = add(conn, library / "bare.mp3", b"x" * 10, bitrate=192_000)
    tagged = add(conn, library / "tagged.mp3", b"x" * 10, bitrate=192_000, album="Album", year=2001, genre="Rock")
    assert dedupe._pick_keeper(conn, [bare, tagged]) == tagged

    quiet = add(conn, library / "quiet.mp3", b"x" * 10)
    loved = add(conn, library / "loved.mp3", b"x" * 10)
    play(conn, loved, 3)
    assert dedupe._pick_keeper(conn, [quiet, loved]) == loved

    first = add(conn, library / "first.mp3", b"x" * 10)
    second = add(conn, library / "second.mp3", b"x" * 10)
    assert dedupe._pick_keeper(conn, [second, first]) == first


# ------------------------------------------------------------------------------------------- finding


def test_groups_span_every_library_folder_when_given_none(conn, tmp_path, library) -> None:
    other = (tmp_path / "other").resolve()
    other.mkdir()
    add(conn, library / "a.mp3")
    add(conn, other / "b.mp3")
    assert len(dedupe.find_duplicate_groups(conn, None)) == 1
    assert len(dedupe.find_duplicate_groups(conn, [library, other])) == 1
    assert dedupe.find_duplicate_groups(conn, library) == []  # only one copy lives in this folder


def test_a_group_the_user_said_is_not_a_duplicate_stays_gone(conn, library) -> None:
    add(conn, library / "a.mp3", musicbrainz_recording_id="mb-1")
    add(conn, library / "b.mp3", musicbrainz_recording_id="mb-1")
    [group] = dedupe.find_duplicate_groups(conn, library)

    assert dedupe.ignore_groups(conn, [group.ignore_key]) == 1
    # not resurfacing under the artist/title match either: the pair was already judged
    assert dedupe.find_duplicate_groups(conn, library, ignored=dedupe.load_ignored(conn)) == []
    assert dedupe.reset_ignored(conn) == 1
    assert len(dedupe.find_duplicate_groups(conn, library, ignored=dedupe.load_ignored(conn))) == 1


def test_only_files_of_equal_size_are_ever_read_for_the_exact_check(conn, library, monkeypatch) -> None:
    add(conn, library / "same1.bin.mp3", b"identical", artist=None, title=None)
    add(conn, library / "same2.bin.mp3", b"identical", artist=None, title=None)
    add(conn, library / "other.mp3", b"a different length entirely", artist=None, title=None)
    read: list[str] = []
    real = dedupe.compute_content_hash
    monkeypatch.setattr(dedupe, "compute_content_hash", lambda p: read.append(p.name) or real(p))

    [group] = dedupe.find_duplicate_groups(conn, library, use_content_hash=True)

    assert group.reason == "content_hash" and group.identical
    assert sorted(read) == ["same1.bin.mp3", "same2.bin.mp3"]
    dedupe.find_duplicate_groups(conn, library, use_content_hash=True)
    assert len(read) == 2, "the second check reuses the remembered hashes"


def test_a_group_found_by_tags_is_marked_identical_only_when_the_bytes_match(conn, library) -> None:
    add(conn, library / "x1.mp3", b"same bytes")
    add(conn, library / "x2.mp3", b"same bytes")
    add(conn, library / "y1.mp3", b"first encode", title="Other")
    add(conn, library / "y2.mp3", b"2nd encode!!", title="Other")  # same length, different content

    groups = {g.key.split("::")[1]: g for g in dedupe.find_duplicate_groups(conn, library, use_content_hash=True)}

    assert groups["song"].identical is True and groups["other"].identical is False
    assert all(g.reason == "normalized_artist_title_duration" for g in groups.values())


def test_a_file_that_cannot_be_read_is_not_called_identical(conn, library) -> None:
    add(conn, library / "a.mp3", b"same", artist=None, title=None)
    gone = add(conn, library / "b.mp3", b"same", artist=None, title=None)
    (library / "b.mp3").unlink()
    assert dedupe.find_duplicate_groups(conn, library, use_content_hash=True) == []
    assert state_of(conn, gone) == 0  # a scan decides what is missing, not this check


def test_describing_groups_puts_the_best_copy_first_and_the_biggest_savings_on_top(conn, library) -> None:
    add(conn, library / "small1.mp3", b"x" * 100, artist="Small", title="One", bitrate=128_000)
    add(conn, library / "small2.mp3", b"x" * 100, artist="Small", title="One", bitrate=320_000)
    add(conn, library / "big1.mp3", b"x" * 9000, artist="Big", title="Two", bitrate=128_000)
    add(conn, library / "big2.mp3", b"x" * 9000, artist="Big", title="Two", bitrate=128_000)
    keep = add(conn, library / "big3.flac", b"x" * 20000, artist="Big", title="Two", format="flac", bitrate=800_000)

    described = dedupe.describe_groups(conn, dedupe.find_duplicate_groups(conn, library))

    assert [g["key"].split("::")[0].split(":")[1] for g in described] == ["big", "small"]
    big = described[0]
    assert big["keeper"] == keep and big["members"][0]["id"] == keep
    assert big["wasted"] == 18000 and [m["format"] for m in big["members"]] == ["flac", "mp3", "mp3"]
    assert described[1]["members"][0]["path"].endswith("small2.mp3")


# ------------------------------------------------------------------------------------------- the review folder


def test_the_removed_copy_goes_to_the_review_folder_and_leaves_the_library(conn, library) -> None:
    keeper = add(conn, library / "Band" / "Album" / "01 - Song.flac", format="flac", bitrate=900_000)
    dup = add(conn, library / "Downloads" / "Song.mp3")
    (library / "Downloads" / "Song.lrc").write_text("[00:01.00]hi")

    result = choose(conn, library, keeper, dup)

    review = library / QUARANTINE_DIR / "Downloads" / "Song.mp3"
    assert (result.moved, result.skipped) == (1, 0) and review.exists()
    assert (review.with_suffix(".lrc")).read_text() == "[00:01.00]hi", "lyrics travel with the song"
    assert not (library / "Downloads").exists(), "the emptied folder is tidied"
    assert state_of(conn, dup) == 2 and path_of(conn, dup) == review
    assert state_of(conn, keeper) == 0 and path_of(conn, keeper).exists()
    visible = [r["id"] for r in conn.execute("SELECT id FROM tracks WHERE is_missing = 0")]
    assert visible == [keeper]
    assert dedupe.find_duplicate_groups(conn, library) == []


def test_a_song_never_moves_unless_the_copy_to_keep_is_really_there(conn, library) -> None:
    keeper = add(conn, library / "keep.mp3")
    dup = add(conn, library / "dup.mp3")
    (library / "keep.mp3").unlink()

    result = choose(conn, library, keeper, dup)

    assert (result.moved, result.skipped) == (0, 1) and "missing" in result.errors[0]["error"]
    assert (library / "dup.mp3").exists() and state_of(conn, dup) == 0


def test_a_song_outside_every_library_folder_is_left_alone(conn, tmp_path, library) -> None:
    keeper = add(conn, library / "keep.mp3")
    stray = add(conn, tmp_path / "elsewhere" / "stray.mp3")

    result = choose(conn, library, keeper, stray)

    assert result.skipped == 1 and "not inside" in result.errors[0]["error"]
    assert (tmp_path / "elsewhere" / "stray.mp3").exists()


def test_a_name_already_in_the_review_folder_gets_a_number(conn, library) -> None:
    keeper = add(conn, library / "keep.mp3", bitrate=320_000)
    first = add(conn, library / "x" / "dup.mp3")
    choose(conn, library, keeper, first)
    # a different song later arrives at the very same place and is removed too
    again = add(conn, library / "x" / "dup.mp3", b"later")
    choose(conn, library, keeper, again)

    names = sorted(p.name for p in (library / QUARANTINE_DIR / "x").iterdir())
    assert names == ["dup (2).mp3", "dup.mp3"]


def test_the_cover_of_a_removed_album_copy_is_kept_with_it(conn, library) -> None:
    keeper = add(conn, library / "Band" / "Album" / "01.mp3", bitrate=320_000)
    dup = add(conn, library / "Copy of Album" / "01.mp3")
    (library / "Copy of Album" / "cover.jpg").write_bytes(JPEG)

    choose(conn, library, keeper, dup)

    assert (library / QUARANTINE_DIR / "Copy of Album" / "cover.jpg").read_bytes() == JPEG
    assert not (library / "Copy of Album").exists()


def test_scans_ignore_the_review_folder(conn, tmp_path) -> None:
    root = tmp_path / "music"
    keep = make_track(root / "Band", "keep.mp3", title="Song", artist="Band", album="A")
    dup = make_track(root / "Downloads", "dup.mp3", title="Song", artist="Band", album="A")
    scan_library(conn, root)
    ids = {Path(r["file_path"]).name: r["id"] for r in conn.execute("SELECT id, file_path FROM tracks")}
    resolved = root.resolve()

    result = dedupe.quarantine(conn, [(ids[keep.name], [ids[dup.name]])], [resolved])
    assert result.moved == 1
    again = scan_library(conn, root)

    assert (again.added, again.missing) == (0, 0)
    assert state_of(conn, ids[dup.name]) == 2, "the scan did not revive or lose the parked copy"
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2


# ------------------------------------------------------------------------------------------- what is carried over


def test_plays_playlist_entries_rating_and_favorite_move_to_the_copy_that_stays(conn, library) -> None:
    keeper = add(conn, library / "keep.flac", format="flac", bitrate=900_000, rating=2)
    dup = add(conn, library / "dup.mp3", rating=5, favorite=1)
    play(conn, keeper, 2)
    play(conn, dup, 3)
    only_dup = playlist(conn, "Road trip", [dup])
    both = playlist(conn, "Both", [dup, keeper])
    only_keeper = playlist(conn, "Studio", [keeper])

    choose(conn, library, keeper, dup)

    assert plays_of(conn, keeper) == 5 and plays_of(conn, dup) == 0
    assert members(conn, only_dup) == [keeper]
    assert members(conn, both) == [keeper] and members(conn, only_keeper) == [keeper]
    row = conn.execute("SELECT rating, favorite FROM tracks WHERE id = ?", (keeper,)).fetchone()
    assert (row["rating"], row["favorite"]) == (5, 1)


def test_a_lower_rating_on_the_removed_copy_does_not_lower_the_kept_one(conn, library) -> None:
    keeper = add(conn, library / "keep.mp3", bitrate=320_000, rating=4)
    dup = add(conn, library / "dup.mp3", rating=1)
    choose(conn, library, keeper, dup)
    assert conn.execute("SELECT rating FROM tracks WHERE id = ?", (keeper,)).fetchone()["rating"] == 4


def test_restoring_a_copy_brings_back_the_file_and_everything_that_was_carried_over(conn, library) -> None:
    keeper = add(conn, library / "keep.flac", format="flac", bitrate=900_000, rating=2)
    dup = add(conn, library / "Down" / "dup.mp3", rating=5, favorite=1)
    (library / "Down" / "dup.lrc").write_text("words")
    play(conn, keeper, 2)
    play(conn, dup, 3)
    only_dup = playlist(conn, "Road trip", [dup])
    both = playlist(conn, "Both", [dup, keeper])
    choose(conn, library, keeper, dup)

    result = dedupe.restore(conn, [dup], [library])

    assert (result.moved, result.skipped) == (1, 0)
    assert (library / "Down" / "dup.mp3").exists() and (library / "Down" / "dup.lrc").read_text() == "words"
    assert not (library / QUARANTINE_DIR).exists(), "the emptied review folder goes too"
    assert state_of(conn, dup) == 0 and path_of(conn, dup) == library / "Down" / "dup.mp3"
    assert (plays_of(conn, keeper), plays_of(conn, dup)) == (2, 3)
    assert members(conn, only_dup) == [dup] and sorted(members(conn, both)) == sorted([dup, keeper])
    row = conn.execute("SELECT rating, favorite FROM tracks WHERE id = ?", (keeper,)).fetchone()
    assert (row["rating"], row["favorite"]) == (2, 0)


def test_undoing_the_whole_batch_is_the_same_as_restoring_each_copy(conn, library) -> None:
    keeper = add(conn, library / "keep.mp3", bitrate=320_000)
    first = add(conn, library / "one.mp3")
    second = add(conn, library / "two.mp3")
    play(conn, first, 2)
    result = choose(conn, library, keeper, first, second)

    undone = moves.undo_batch(conn, result.batch_id, "quarantine", [library])

    assert undone.moved == 2 and (library / "one.mp3").exists() and (library / "two.mp3").exists()
    assert plays_of(conn, first) == 2
    assert [state_of(conn, i) for i in (keeper, first, second)] == [0, 0, 0]


def test_a_restored_copy_with_a_forgotten_log_still_goes_home(conn, library) -> None:
    keeper = add(conn, library / "keep.mp3", bitrate=320_000)
    dup = add(conn, library / "Band" / "Album" / "dup.mp3")
    choose(conn, library, keeper, dup)
    conn.execute("DELETE FROM file_moves")
    conn.commit()

    assert dedupe.list_quarantined(conn)["items"][0]["original"] == str(library / "Band" / "Album" / "dup.mp3")
    result = dedupe.restore(conn, [dup], [library])

    assert result.moved == 1 and (library / "Band" / "Album" / "dup.mp3").exists() and state_of(conn, dup) == 0


def test_restore_refuses_to_overwrite_a_file_that_took_the_old_place(conn, library) -> None:
    keeper = add(conn, library / "keep.mp3", bitrate=320_000)
    dup = add(conn, library / "dup.mp3")
    choose(conn, library, keeper, dup)
    (library / "dup.mp3").write_bytes(b"someone else's file")

    result = dedupe.restore(conn, [dup], [library])

    assert result.skipped == 1 and (library / "dup.mp3").read_bytes() == b"someone else's file"
    assert state_of(conn, dup) == 2


def test_cancelling_halfway_keeps_what_moved_logged_and_undoable(conn, library) -> None:
    keeper = add(conn, library / "keep.mp3", bitrate=320_000)
    dups = [add(conn, library / f"d{n}.mp3", title=f"T{n}") for n in range(3)]

    class Cancelled(Exception):
        pass

    def stop(done: int, total: int) -> None:
        if done >= 1:
            raise Cancelled

    with pytest.raises(Cancelled):
        dedupe.quarantine(conn, [(keeper, dups)], [library], on_progress=stop)

    assert [state_of(conn, i) for i in dups] == [2, 0, 0]
    [batch] = moves.list_batches(conn, "quarantine")
    assert batch["files"] == 1 and moves.undo_batch(conn, batch["batch_id"], "quarantine", [library]).moved == 1


def test_one_batch_can_span_several_library_folders(conn, tmp_path, library) -> None:
    other = (tmp_path / "other").resolve()
    keeper = add(conn, library / "keep.mp3", bitrate=320_000)
    here = add(conn, library / "here.mp3")
    there = add(conn, other / "there.mp3")

    result = dedupe.quarantine(conn, [(keeper, [here, there])], [library, other])

    assert result.moved == 2
    assert (library / QUARANTINE_DIR / "here.mp3").exists() and (other / QUARANTINE_DIR / "there.mp3").exists()
    assert [b["files"] for b in moves.list_batches(conn, "quarantine")] == [2]


def test_the_command_line_helper_keeps_the_best_copy(conn, library) -> None:
    flac = add(conn, library / "a.flac", format="flac", bitrate=900_000)
    mp3 = add(conn, library / "a.mp3")
    groups = dedupe.find_duplicate_groups(conn, library)

    assert dedupe.apply_quarantine(conn, groups, library) == 1
    assert (state_of(conn, flac), state_of(conn, mp3)) == (0, 2)


# ------------------------------------------------------------------------------------------- the review list


def test_the_review_list_says_where_each_copy_came_from(conn, library) -> None:
    keeper = add(conn, library / "keep.mp3", bitrate=320_000)
    dup = add(conn, library / "Band" / "dup.mp3", b"twelve bytes")
    choose(conn, library, keeper, dup)

    listing = dedupe.list_quarantined(conn)

    assert listing["total"] == 1
    [item] = listing["items"]
    assert item["original"] == str(library / "Band" / "dup.mp3") and item["exists"] and item["size"] == 12
    assert dedupe.quarantined_summary(conn) == {"count": 1, "bytes": 12}
    assert dedupe.quarantined_ids(conn) == [dup]


# ------------------------------------------------------------------------------------------- purging


class Bin:
    """A stand-in for the Recycle Bin that records what was thrown away."""

    def __init__(self, folder: Path, fail_on: str | None = None) -> None:
        self.folder = folder
        self.fail_on = fail_on
        self.thrown: list[str] = []

    def __call__(self, path: str) -> None:
        if self.fail_on and self.fail_on in path:
            raise PermissionError("access denied")
        self.folder.mkdir(exist_ok=True)
        shutil.move(path, self.folder / f"{len(self.thrown)}-{Path(path).name}")
        self.thrown.append(Path(path).name)


def parked(conn, library) -> tuple[int, int]:
    keeper = add(conn, library / "keep.mp3", bitrate=320_000)
    dup = add(conn, library / "Band" / "dup.mp3")
    (library / "Band" / "dup.lrc").write_text("words")
    choose(conn, library, keeper, dup)
    return keeper, dup


def test_purging_moves_the_copy_and_its_lyrics_to_the_bin_and_forgets_it(conn, tmp_path, library) -> None:
    keeper, dup = parked(conn, library)
    bin_ = Bin(tmp_path / "bin")

    result = dedupe.purge(conn, [dup], [library], trash=bin_)

    assert (result.moved, result.skipped) == (1, 0) and sorted(bin_.thrown) == ["dup.lrc", "dup.mp3"]
    assert conn.execute("SELECT COUNT(*) FROM tracks WHERE id = ?", (dup,)).fetchone()[0] == 0
    assert not (library / QUARANTINE_DIR).exists()
    assert (library / "keep.mp3").exists() and state_of(conn, keeper) == 0


def test_purging_keeps_what_the_copy_left_behind_consistent(conn, tmp_path, library) -> None:
    """The row is referenced by playlists, history and device sync state; none of that may block or dangle."""
    keeper = add(conn, library / "keep.mp3", bitrate=320_000)
    dup = add(conn, library / "dup.mp3")
    conn.execute("INSERT INTO devices (label) VALUES ('Walkman')")
    conn.execute("INSERT INTO sync_manifest (device_id, track_id, dest_relative_path, status) VALUES (1, ?, 'x/dup.mp3', 'synced')", (dup,))
    conn.commit()
    choose(conn, library, keeper, dup)
    play(conn, dup, 0)  # nothing attached to the copy any more: it was handed over

    dedupe.purge(conn, [dup], [library], trash=Bin(tmp_path / "bin"))

    assert conn.execute("SELECT COUNT(*) FROM sync_manifest").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_song_still_in_the_library_cannot_be_purged(conn, tmp_path, library) -> None:
    add(conn, library / "keep.mp3")
    live = add(conn, library / "live.mp3")
    bin_ = Bin(tmp_path / "bin")

    result = dedupe.purge(conn, [live], [library], trash=bin_)

    assert (result.moved, result.skipped) == (0, 1) and bin_.thrown == [] and (library / "live.mp3").exists()


def test_only_files_inside_a_review_folder_can_be_purged(conn, tmp_path, library) -> None:
    """Even a row flagged as parked is not trusted: the path itself has to be in a review folder."""
    stray = add(conn, library / "Band" / "precious.mp3", is_missing=2)
    bin_ = Bin(tmp_path / "bin")

    result = dedupe.purge(conn, [stray], [library], trash=bin_)

    assert result.skipped == 1 and "review folder" in result.errors[0]["error"]
    assert (library / "Band" / "precious.mp3").exists() and bin_.thrown == []


def test_a_copy_the_bin_refuses_stays_put_and_listed(conn, tmp_path, library) -> None:
    keeper, dup = parked(conn, library)
    other = add(conn, library / "other.mp3")
    choose(conn, library, keeper, other)

    result = dedupe.purge(conn, [dup, other], [library], trash=Bin(tmp_path / "bin", fail_on="dup.mp3"))

    assert (result.moved, result.skipped) == (1, 1) and "Recycle Bin" in result.errors[0]["error"]
    assert state_of(conn, dup) == 2 and (library / QUARANTINE_DIR / "Band" / "dup.mp3").exists()


def test_purging_removes_a_row_whose_file_is_already_gone(conn, tmp_path, library) -> None:
    keeper, dup = parked(conn, library)
    path_of(conn, dup).unlink()

    result = dedupe.purge(conn, [dup], [library], trash=Bin(tmp_path / "bin"))

    assert result.moved == 1 and conn.execute("SELECT COUNT(*) FROM tracks WHERE id = ?", (dup,)).fetchone()[0] == 0


def test_a_lyrics_file_shared_by_two_formats_goes_with_the_last_of_them(conn, tmp_path, library) -> None:
    keeper = add(conn, library / "keep.flac", format="flac", bitrate=900_000)
    first = add(conn, library / "x" / "same.mp3")
    second = add(conn, library / "x" / "same.ogg", format="ogg")
    (library / "x" / "same.lrc").write_text("shared words")
    choose(conn, library, keeper, first, second)
    bin_ = Bin(tmp_path / "bin")

    dedupe.purge(conn, [first], [library], trash=bin_)
    assert (library / QUARANTINE_DIR / "x" / "same.lrc").exists(), "the other copy still needs it"
    dedupe.purge(conn, [second], [library], trash=bin_)

    assert sorted(bin_.thrown) == ["same.lrc", "same.mp3", "same.ogg"]


def test_the_real_recycle_bin_path_works(conn, tmp_path, library, monkeypatch) -> None:
    """Goes through send2trash itself. On Linux it needs a trash folder to exist, so give it one."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    keeper, dup = parked(conn, library)
    target = path_of(conn, dup)

    result = dedupe.purge(conn, [dup], [library])

    assert result.moved == 1 and result.skipped == 0, result.errors
    assert not target.exists()


# ------------------------------------------------------------------------------------------- forgetting and missing files


def test_forgetting_a_song_clears_what_points_at_it_but_keeps_the_listening_history(conn, library) -> None:
    keep = add(conn, library / "keep.mp3")
    gone = add(conn, library / "gone.mp3", is_missing=1)
    play(conn, gone, 2)
    pid = playlist(conn, "Mix", [keep, gone])
    conn.execute("INSERT INTO tag_proposals (track_id, proposed_json, confidence, created_at) VALUES (?, '{}', 0.9, 'now')", (gone,))
    conn.commit()

    assert cleanup.forget_tracks(conn, [gone]) == 1

    assert members(conn, pid) == [keep]
    assert conn.execute("SELECT COUNT(*) FROM play_history WHERE track_id IS NULL").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM tag_proposals").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_the_missing_report_groups_songs_by_library_folder(conn, tmp_path, library) -> None:
    usb = (tmp_path / "usb").resolve()  # never created: a drive that is not plugged in
    here = add(conn, library / "gone.mp3", is_missing=1)
    gone = add(conn, library / "Deep" / "gone2.mp3", is_missing=1)
    add(conn, library / "fine.mp3")
    for name in ("a.mp3", "b.mp3"):
        conn.execute("INSERT INTO tracks (file_path, is_missing, title) VALUES (?, 1, ?)", (str(usb / name), name))
    conn.execute("INSERT INTO tracks (file_path, is_missing, title) VALUES (?, 1, 'removed')", (str(tmp_path / "old-root" / "z.mp3"),))
    conn.commit()
    roots = [str(library), str(usb)]

    summary = cleanup.missing_summary(conn, roots)

    assert summary["total"] == 5 and summary["elsewhere"] == 1
    assert [(r["path"], r["count"], r["available"]) for r in summary["roots"]] == [(str(library), 2, True), (str(usb), 2, False)]
    listing = cleanup.list_missing(conn, roots, root=str(library))
    assert listing["total"] == 2 and {i["id"] for i in listing["items"]} == {here, gone}
    assert cleanup.list_missing(conn, roots, root=cleanup.OTHER)["total"] == 1
    assert len(cleanup.list_missing(conn, roots, offset=1, limit=2)["items"]) == 2


def test_forgetting_missing_songs_keeps_any_whose_file_came_back(conn, library) -> None:
    back = add(conn, library / "back.mp3", is_missing=1)  # the file is there again, no scan has run yet
    gone = add(conn, library / "gone.mp3", is_missing=1)
    (library / "gone.mp3").unlink()

    outcome = cleanup.forget_missing(conn, [str(library)], root=str(library))

    assert outcome == {"forgotten": 1, "kept": 1}
    assert conn.execute("SELECT id FROM tracks WHERE is_missing = 1").fetchall()[0]["id"] == back
    assert conn.execute("SELECT COUNT(*) FROM tracks WHERE id = ?", (gone,)).fetchone()[0] == 0


def test_forgetting_can_target_chosen_songs_or_everything(conn, library) -> None:
    a = add(conn, library / "a.mp3", is_missing=1)
    b = add(conn, library / "b.mp3", is_missing=1)
    c = add(conn, library / "c.mp3", is_missing=1)
    for name in "abc":
        (library / f"{name}.mp3").unlink()

    assert cleanup.forget_missing(conn, [str(library)], track_ids=[a])["forgotten"] == 1
    assert cleanup.forget_missing(conn, [str(library)])["forgotten"] == 2
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0 and b and c


def test_a_missing_song_that_is_not_flagged_missing_is_never_forgotten(conn, library) -> None:
    live = add(conn, library / "live.mp3")
    (library / "live.mp3").unlink()  # vanished, but no scan has noticed
    assert cleanup.forget_missing(conn, [str(library)], track_ids=[live]) == {"forgotten": 0, "kept": 0}


def test_path_buckets_do_not_confuse_a_look_alike_folder(conn, tmp_path, library) -> None:
    sibling = tmp_path / (library.name + "-backup")
    conn.execute("INSERT INTO tracks (file_path, is_missing) VALUES (?, 1)", (str(sibling / "x.mp3"),))
    conn.commit()
    assert cleanup.missing_summary(conn, [str(library)])["elsewhere"] == 1
