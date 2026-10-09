"""Moving files for Organize: lyrics and covers follow, folders are tidied, and every batch can be undone."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from musictoolkit.db.connection import connect
from musictoolkit.ingest import moves, organizer
from tests.test_organizer import SCHEME, _insert_track

JPEG = b"\xff\xd8\xff\xe0 not really a picture"


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    root.mkdir()
    return root.resolve()


def add_song(conn, folder: Path, name: str = "raw.mp3", **tags) -> tuple[int, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"audio:" + name.encode())
    tags = {"artist": "Artist", "album": "Album", "title": "Title", "track_number": 1, **tags}
    return _insert_track(conn, str(path), **tags), path


def organize(conn, library: Path, scheme: str = SCHEME):
    plan = organizer.propose_organization(conn, library, scheme)
    return plan, organizer.apply_with_log(conn, plan.proposals, library)


def path_of(conn, track_id: int) -> Path:
    return Path(conn.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,)).fetchone()["file_path"])


# ------------------------------------------------------------------------------------- companions follow


def test_the_lyrics_file_moves_with_its_song(conn, library) -> None:
    track_id, song = add_song(conn, library)
    song.with_suffix(".lrc").write_text("[00:01.00]la la")

    _, result = organize(conn, library)

    new = library / "Artist" / "Album" / "01 - Title.mp3"
    assert result.moved == 1 and new.exists()
    assert new.with_suffix(".lrc").read_text() == "[00:01.00]la la"
    assert not song.with_suffix(".lrc").exists()
    assert path_of(conn, track_id) == new


def test_an_upper_case_lyrics_extension_is_found_too(conn, library) -> None:
    _, song = add_song(conn, library)
    song.with_suffix(".LRC").write_text("[00:01.00]hello")

    organize(conn, library)

    moved = library / "Artist" / "Album"
    assert [p.name for p in moved.iterdir() if p.suffix.lower() == ".lrc"], "lyrics were left behind"


def test_folder_art_is_copied_so_the_album_keeps_its_cover(conn, library) -> None:
    raw = library / "Downloads"
    _, song = add_song(conn, raw)
    (raw / "cover.jpg").write_bytes(JPEG)

    organize(conn, library)

    assert (library / "Artist" / "Album" / "cover.jpg").read_bytes() == JPEG
    assert not raw.exists(), "the folder held only a song and a cover that now lives at the destination"


def test_an_existing_cover_at_the_destination_is_not_overwritten(conn, library) -> None:
    raw = library / "Downloads"
    add_song(conn, raw)
    (raw / "cover.jpg").write_bytes(b"old cover")
    dest = library / "Artist" / "Album"
    dest.mkdir(parents=True)
    (dest / "folder.jpg").write_bytes(b"better cover")

    organize(conn, library)

    assert (dest / "folder.jpg").read_bytes() == b"better cover"
    assert not (dest / "cover.jpg").exists()


def test_a_cover_is_copied_once_for_an_album_not_per_song(conn, library) -> None:
    raw = library / "Downloads"
    add_song(conn, raw, "a.mp3", title="One", track_number=1)
    add_song(conn, raw, "b.mp3", title="Two", track_number=2)
    (raw / "cover.jpg").write_bytes(JPEG)

    _, result = organize(conn, library)

    assert result.moved == 2
    rows = conn.execute("SELECT sidecars_json FROM file_moves WHERE batch_id = ?", (result.batch_id,)).fetchall()
    assert sum('art_copy' in r["sidecars_json"] for r in rows) == 1


def test_a_folder_that_still_holds_other_music_keeps_its_cover_and_stays(conn, library) -> None:
    mixed = library / "Mixed"
    add_song(conn, mixed, "stay.mp3", artist="Mixed", album="Mixed", title="Stay", track_number=1)
    add_song(conn, mixed, "go.mp3", title="Go", track_number=1)
    (mixed / "cover.jpg").write_bytes(JPEG)

    plan = organizer.propose_organization(conn, library, SCHEME)
    organizer.apply_with_log(conn, [p for p in plan.proposals if p.old_path.name == "go.mp3"], library)  # one song only

    assert (mixed / "stay.mp3").exists() and (mixed / "cover.jpg").exists()
    assert (library / "Artist" / "Album" / "cover.jpg").exists(), "the song that left took a copy of the cover"


# ------------------------------------------------------------------------------------- tidying folders


def test_emptied_folders_are_removed_up_to_the_library_folder(conn, library) -> None:
    deep = library / "Downloads" / "2020" / "Batch 1"
    add_song(conn, deep)
    (deep / "Thumbs.db").write_bytes(b"junk")
    (deep.parent / "desktop.ini").write_bytes(b"junk")

    _, result = organize(conn, library)

    assert not (library / "Downloads").exists()
    assert library.exists()
    assert result.tidied_folders == 3


def test_a_parent_holding_anything_else_is_left_alone(conn, library) -> None:
    deep = library / "Downloads" / "2020"
    add_song(conn, deep)
    (library / "Downloads" / "notes.txt").write_text("keep me")

    organize(conn, library)

    assert not deep.exists()
    assert (library / "Downloads" / "notes.txt").read_text() == "keep me"


def test_a_parent_folder_with_a_picture_is_not_deleted(conn, library) -> None:
    """A folder.jpg one level up may be an artist photo; only the album folder's own cover may go."""
    album = library / "Odd Artist" / "Album"
    add_song(conn, album, artist="Someone", album="Else")
    (album.parent / "folder.jpg").write_bytes(JPEG)
    (album / "cover.jpg").write_bytes(JPEG)

    organize(conn, library)

    assert (album.parent / "folder.jpg").exists()
    assert not album.exists()


def test_folders_outside_the_library_are_never_touched(conn, tmp_path, library) -> None:
    other = tmp_path / "elsewhere"
    _, song = add_song(conn, other)
    # a (hand-edited) move that crosses out of the library root: nothing outside it may be removed
    mover = moves.Mover(conn, "organize", library)
    mover.move(1, song, library / "moved.mp3")
    result = mover.finish()

    assert other.exists() and result.tidied_folders == 0


def test_prune_never_removes_the_stop_folder_itself(library) -> None:
    (library / "Thumbs.db").write_bytes(b"junk")
    assert moves.prune_empty_folders(library, library, allow_art=True) == 0
    assert library.exists() and (library / "Thumbs.db").exists()


def test_prune_refuses_a_look_alike_sibling_of_the_library(tmp_path, library) -> None:
    sibling = tmp_path / (library.name + "-backup")
    sibling.mkdir()
    assert moves.prune_empty_folders(sibling, library, allow_art=True) == 0
    assert sibling.exists()


# ------------------------------------------------------------------------------------- undo


def test_undo_puts_songs_lyrics_and_covers_back(conn, library) -> None:
    raw = library / "Downloads"
    track_id, song = add_song(conn, raw)
    song.with_suffix(".lrc").write_text("[00:01.00]words")
    (raw / "cover.jpg").write_bytes(JPEG)

    _, result = organize(conn, library)
    assert not raw.exists()

    undone = moves.undo_batch(conn, result.batch_id, "organize", [library])

    assert undone.moved == 1 and undone.errors == []
    assert song.read_bytes().startswith(b"audio:")
    assert song.with_suffix(".lrc").read_text() == "[00:01.00]words"
    assert (raw / "cover.jpg").read_bytes() == JPEG
    assert not (library / "Artist").exists(), "the folders the move created are tidied away again"
    assert path_of(conn, track_id) == song


def test_a_batch_can_only_be_undone_once(conn, library) -> None:
    add_song(conn, library)
    _, result = organize(conn, library)

    first = moves.undo_batch(conn, result.batch_id, "organize", [library])
    second = moves.undo_batch(conn, result.batch_id, "organize", [library])

    assert (first.moved, second.moved) == (1, 0)
    assert moves.list_batches(conn, "organize")[0]["undoable"] == 0


def test_undo_reports_a_song_that_was_moved_away_since(conn, library) -> None:
    _, song = add_song(conn, library)
    _, result = organize(conn, library)
    new = library / "Artist" / "Album" / "01 - Title.mp3"
    new.unlink()

    undone = moves.undo_batch(conn, result.batch_id, "organize", [library])

    assert undone.moved == 0 and undone.skipped == 1
    assert "no longer" in undone.errors[0]["error"]
    assert not song.exists()


def test_undo_never_overwrites_what_now_sits_at_the_old_spot(conn, library) -> None:
    _, song = add_song(conn, library)
    _, result = organize(conn, library)
    song.write_bytes(b"someone else's file")

    undone = moves.undo_batch(conn, result.batch_id, "organize", [library])

    assert undone.skipped == 1 and "already at the original" in undone.errors[0]["error"]
    assert song.read_bytes() == b"someone else's file"
    assert (library / "Artist" / "Album" / "01 - Title.mp3").exists()


def test_undo_keeps_going_after_one_song_cannot_be_restored(conn, library) -> None:
    _, first = add_song(conn, library, "one.mp3", title="One", track_number=1)
    _, second = add_song(conn, library, "two.mp3", title="Two", track_number=2)
    _, result = organize(conn, library)
    (library / "Artist" / "Album" / "01 - One.mp3").unlink()

    undone = moves.undo_batch(conn, result.batch_id, "organize", [library])

    assert (undone.moved, undone.skipped) == (1, 1)
    assert second.exists()


def test_undo_reports_progress(conn, library) -> None:
    add_song(conn, library, "one.mp3", title="One", track_number=1)
    add_song(conn, library, "two.mp3", title="Two", track_number=2)
    _, result = organize(conn, library)
    seen: list[tuple[int, int]] = []

    moves.undo_batch(conn, result.batch_id, "organize", [library], on_progress=lambda d, t: seen.append((d, t)))

    assert seen[0] == (0, 2) and seen[-1] == (2, 2)


# ------------------------------------------------------------------------------------- planning and applying


def test_apply_time_collisions_are_skipped_not_overwritten(conn, library) -> None:
    _, song = add_song(conn, library)
    plan = organizer.propose_organization(conn, library, SCHEME)
    target = plan.proposals[0].new_path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"appeared after the preview")

    result = organizer.apply_with_log(conn, plan.proposals, library)

    assert (result.moved, result.skipped) == (0, 1)
    assert target.read_bytes() == b"appeared after the preview" and song.exists()
    assert conn.execute("SELECT COUNT(*) FROM file_moves").fetchone()[0] == 0


def test_a_name_held_by_a_missing_songs_row_is_a_collision_not_a_crash(conn, library) -> None:
    """The library keeps a row for a file that vanished (it may carry a rating). A path can't belong to two rows."""
    _, song = add_song(conn, library)
    stale = library / "Artist" / "Album" / "01 - Title.mp3"
    _insert_track(conn, str(stale), artist="Gone", is_missing=1)

    plan = organizer.propose_organization(conn, library, SCHEME)
    assert plan.proposals == [] and len(plan.collisions) == 1

    # and if the row appears between preview and apply, the move is skipped before touching the file
    forced = organizer.MoveProposal(track_id=1, old_path=song, new_path=stale)
    result = organizer.apply_with_log(conn, [forced], library)
    assert (result.moved, result.skipped) == (0, 1) and "already lists" in result.errors[0]["error"]
    assert song.exists() and not stale.exists()


def test_a_failed_library_update_puts_the_files_back(conn, library, monkeypatch) -> None:
    _, song = add_song(conn, library)
    song.with_suffix(".lrc").write_text("words")
    plan = organizer.propose_organization(conn, library, SCHEME)

    class Broken:
        """Delegates to the real connection, but the log insert fails."""

        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO file_moves"):
                raise __import__("sqlite3").OperationalError("database is locked")
            return self.inner.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    result = organizer.apply_with_log(Broken(conn), plan.proposals, library)

    assert (result.moved, result.skipped) == (0, 1)
    assert song.exists() and song.with_suffix(".lrc").read_text() == "words"
    assert not (library / "Artist" / "Album" / "01 - Title.mp3").exists()
    assert path_of(conn, 1) == song


def test_undo_skips_a_song_whose_old_name_belongs_to_another_row(conn, library) -> None:
    _, song = add_song(conn, library)
    _, result = organize(conn, library)
    _insert_track(conn, str(song), artist="Someone else", is_missing=1)  # a stale row now holds the old path

    undone = moves.undo_batch(conn, result.batch_id, "organize", [library])

    assert undone.skipped == 1 and "lists a different song" in undone.errors[0]["error"]
    assert (library / "Artist" / "Album" / "01 - Title.mp3").exists() and not song.exists()


def test_a_missing_file_is_reported_and_the_rest_still_move(conn, library) -> None:
    _, gone = add_song(conn, library, "gone.mp3", title="Gone", track_number=1)
    add_song(conn, library, "here.mp3", title="Here", track_number=2)
    plan = organizer.propose_organization(conn, library, SCHEME)
    gone.unlink()

    result = organizer.apply_with_log(conn, plan.proposals, library)

    assert (result.moved, result.skipped) == (1, 1)
    assert "missing" in result.errors[0]["error"]


def test_cancelling_keeps_what_moved_so_far_undoable_and_still_tidies(conn, library) -> None:
    raw = library / "Downloads"
    for n in range(1, 4):
        add_song(conn, raw, f"{n}.mp3", title=f"Song {n}", track_number=n)
    plan = organizer.propose_organization(conn, library, SCHEME)
    calls = 0

    class Cancelled(Exception):
        pass

    def stop_after_two(done: int, total: int) -> None:
        nonlocal calls
        calls += 1
        if done >= 2:
            raise Cancelled

    with pytest.raises(Cancelled):
        organizer.apply_with_log(conn, plan.proposals, library, on_progress=stop_after_two)

    batches = moves.list_batches(conn, "organize")
    assert len(batches) == 1 and batches[0]["files"] == 2
    assert len(list(raw.glob("*.mp3"))) == 1, "the third song was never touched"
    undone = moves.undo_batch(conn, batches[0]["batch_id"], "organize", [library])
    assert undone.moved == 2 and len(list(raw.glob("*.mp3"))) == 3


def test_only_files_inside_the_chosen_folder_are_planned(conn, tmp_path, library) -> None:
    other = tmp_path / "other-root"
    add_song(conn, other)
    add_song(conn, library)

    plan = organizer.propose_organization(conn, library, SCHEME)

    assert len(plan.proposals) == 1 and plan.proposals[0].new_path.is_relative_to(library)


def test_songs_already_in_place_are_counted_not_proposed(conn, library) -> None:
    add_song(conn, library / "Artist" / "Album", "01 - Title.mp3")

    plan = organizer.propose_organization(conn, library, SCHEME)

    assert plan.proposals == [] and plan.unchanged == 1


def test_songs_without_tags_are_counted_as_unknown(conn, library) -> None:
    add_song(conn, library, artist=None, album=None, title=None, track_number=None)

    plan = organizer.propose_organization(conn, library, SCHEME)

    assert len(plan.proposals) == 1 and plan.unknown == 1
    assert plan.proposals[0].new_path.parts[-3:-1] == ("Unknown Artist", "Unknown Album")


def test_each_move_is_logged_with_a_shared_batch_id(conn, library) -> None:
    add_song(conn, library, "one.mp3", title="One", track_number=1)
    add_song(conn, library, "two.mp3", title="Two", track_number=2)

    _, result = organize(conn, library)

    rows = conn.execute("SELECT batch_id, kind FROM file_moves").fetchall()
    assert {(r["batch_id"], r["kind"]) for r in rows} == {(result.batch_id, "organize")}
    assert len(rows) == 2


def test_only_the_newest_batches_are_kept(conn, library, monkeypatch) -> None:
    monkeypatch.setattr(moves, "KEEP_BATCHES", 2)
    for n in range(4):
        add_song(conn, library, f"s{n}.mp3", title=f"T{n}", track_number=n + 1)
        organize(conn, library)

    assert len(moves.list_batches(conn, "organize")) == 2


# ------------------------------------------------------------------------------------- Windows-minded names


def test_reserved_device_names_get_a_prefix(conn, library) -> None:
    add_song(conn, library, artist="CON", album="aux", title="NUL")

    plan = organizer.propose_organization(conn, library, SCHEME)

    new = plan.proposals[0].new_path
    assert new.relative_to(library).parts == ("_CON", "_aux", "01 - NUL.mp3"), "the file name has a prefix in front of '01 - '"


def test_trailing_dots_and_spaces_are_dropped_from_every_folder(conn, library) -> None:
    add_song(conn, library, artist="Wrong.", album="Name ", title="X")

    new = organizer.propose_organization(conn, library, SCHEME).proposals[0].new_path

    assert new.relative_to(library).parts[:2] == ("Wrong", "Name")


def test_a_title_cannot_climb_out_of_the_library(conn, library) -> None:
    add_song(conn, library, artist="..", album="..", title="..")

    for proposal in organizer.propose_organization(conn, library, SCHEME).proposals:
        assert proposal.new_path.is_relative_to(library)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file systems are case-sensitive")
def test_case_only_names_are_different_files_on_a_case_sensitive_disk(conn, library) -> None:
    add_song(conn, library / "artist" / "album", "01 - title.mp3", artist="Artist", album="Album", title="Title")

    plan = organizer.propose_organization(conn, library, SCHEME)

    assert len(plan.proposals) == 1 and plan.collisions == []


@pytest.mark.skipif(os.name == "nt", reason="needs a file system where a link can have a different name")
def test_a_hard_link_to_the_same_song_is_not_mistaken_for_a_case_change(tmp_path) -> None:
    src = tmp_path / "a.mp3"
    src.write_bytes(b"x")
    alias = tmp_path / "b.mp3"
    os.link(src, alias)

    with pytest.raises(moves.MoveError, match="already there"):
        moves._rename(src, alias)

    assert src.exists() and alias.exists() and not list(tmp_path.glob("*.moving"))


@pytest.mark.skipif(os.name != "nt", reason="needs a case-insensitive file system")
def test_a_case_only_rename_goes_through_a_temporary_name(tmp_path) -> None:
    src = tmp_path / "a.mp3"
    src.write_bytes(b"x")

    moves._rename(src, tmp_path / "A.mp3")

    assert [p.name for p in tmp_path.iterdir()] == ["A.mp3"]


@pytest.mark.skipif(os.name != "nt", reason="needs a case-insensitive file system")
def test_songs_whose_path_differs_only_in_letter_case_count_as_in_place(conn, library) -> None:
    add_song(conn, library / "artist" / "album", "01 - title.mp3", artist="Artist", album="Album", title="Title")

    plan = organizer.propose_organization(conn, library, SCHEME)

    assert plan.proposals == [] and plan.unchanged == 1


@pytest.mark.skipif(os.name != "nt", reason="needs a case-insensitive file system")
def test_the_library_records_the_folder_spelling_the_disk_really_has(conn, library) -> None:
    """A move into "Artist/Album" lands in the existing "artist/album". The row must say so, or the next scan
    would see the same file under a second name."""
    existing = library / "artist" / "album"
    existing.mkdir(parents=True)
    (existing / "other.mp3").write_bytes(b"x")
    track_id, _ = add_song(conn, library / "Downloads")

    organize(conn, library)

    recorded = path_of(conn, track_id)
    assert recorded.parts[-3:-1] == ("artist", "album") and recorded.exists()
