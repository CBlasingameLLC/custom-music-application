"""The sync engine on a phone or player without a drive letter (MTP), run against the stand-in helper.

The engine is the same code as for a folder (tests/test_sync_device.py); what differs here is what an MTP device
cannot do: there is no rename, so a failed copy must be cleaned up by hand, names that differ only in case are
the same name, and everything goes through a helper process that can fail or vanish.
"""

from __future__ import annotations

import errno
import shutil
import sys
from pathlib import Path

import pytest

from musictoolkit.sync import mirror
from musictoolkit.sync.mtp import MtpHelper, MtpTarget
from tests.test_mtp import FAKE, SERIAL, control, write_device
from tests.test_mtp import helper, phones, target  # noqa: F401  (fixtures)
from tests.test_sync_device import FREE, SCHEME, add, conn, library, manifest, rows  # noqa: F401  (conn and library are fixtures)


def on_device(phones: Path, *parts: str) -> Path:
    """Where the stand-in helper keeps a file the engine put under Music/ on the phone's first storage."""
    return phones.joinpath(SERIAL, "s1", "Music", *parts)


def new_device(conn) -> int:
    return mirror.get_or_create_device(conn, f"mtp:{SERIAL}", "Pixel")


def plan_for(conn, device_id, target, selected, scheme=SCHEME):
    return mirror.plan_sync(conn, device_id, target, selected, scheme, target.free_space())


def sync(conn, device_id, target, selected, prune=False, **kw):
    plan = plan_for(conn, device_id, target, selected)
    return plan, mirror.run_sync(conn, device_id, target, plan, prune=prune, **kw)


# ------------------------------------------------------------------------------------------- the first sync


def test_a_first_sync_puts_everything_under_the_music_folder(conn, library, phones, target) -> None:
    a = add(conn, library, "one.mp3", 300, title="One", track_number=1)
    b = add(conn, library, "two.mp3", 500, title='Two: "Live"?', track_number=2)
    device_id = new_device(conn)

    plan, result = sync(conn, device_id, target, rows(conn, a, b))

    assert (len(plan.to_copy), plan.total_bytes_to_copy, plan.skipped) == (2, 800, [])
    assert (result.copied, result.bytes_copied, result.errors, result.aborted) == (2, 800, [], None)
    assert on_device(phones, "Artist", "Album", "01 - One.mp3").stat().st_size == 300
    assert on_device(phones, "Artist", "Album", "02 - Two_ _Live__.mp3").exists(), "characters FAT cannot hold are replaced"
    saved = conn.execute("SELECT * FROM sync_manifest WHERE track_id = ?", (a,)).fetchone()
    assert (saved["status"], saved["size"], saved["dest_relative_path"]) == ("synced", 300, "Artist/Album/01 - One.mp3")


def test_previewing_touches_nothing_on_the_device(conn, library, phones, target) -> None:
    a = add(conn, library, "one.mp3")
    device_id = new_device(conn)

    plan_for(conn, device_id, target, rows(conn, a))

    assert not on_device(phones).exists()


def test_a_second_sync_copies_nothing_new(conn, library, target) -> None:
    a = add(conn, library, "one.mp3")
    device_id = new_device(conn)
    sync(conn, device_id, target, rows(conn, a))

    plan = plan_for(conn, device_id, target, rows(conn, a))

    assert (plan.to_copy, plan.unchanged, plan.to_prune) == ([], 1, [])


def test_the_reason_each_song_will_be_copied_is_named(conn, library, phones, target) -> None:
    gone = add(conn, library, "gone.mp3", title="Gone", track_number=1)
    cut = add(conn, library, "cut.mp3", title="Cut", track_number=2)
    edited = add(conn, library, "edited.mp3", title="Edited", track_number=3)
    fresh = add(conn, library, "fresh.mp3", title="Fresh", track_number=4)
    device_id = new_device(conn)
    sync(conn, device_id, target, rows(conn, gone, cut, edited))
    on_device(phones, "Artist", "Album", "01 - Gone.mp3").unlink()
    on_device(phones, "Artist", "Album", "02 - Cut.mp3").write_bytes(b"cut short")
    path = Path(conn.execute("SELECT file_path FROM tracks WHERE id = ?", (edited,)).fetchone()[0])
    path.write_bytes(path.read_bytes() + b"more")

    plan = plan_for(conn, device_id, target, rows(conn, gone, cut, edited, fresh))

    assert {item.row["id"]: item.reason for item in plan.to_copy} == {gone: "missing", cut: "damaged", edited: "changed", fresh: "new"}


def test_a_folder_someone_deleted_on_the_phone_is_noticed(conn, library, phones, target) -> None:
    a = add(conn, library, "one.mp3", title="One", track_number=1)
    b = add(conn, library, "two.mp3", title="Two", track_number=2)
    device_id = new_device(conn)
    sync(conn, device_id, target, rows(conn, a, b))

    shutil.rmtree(on_device(phones, "Artist"))

    plan = plan_for(conn, device_id, target, rows(conn, a, b))

    assert sorted(item.reason for item in plan.to_copy) == ["missing", "missing"]


# ------------------------------------------------------------------------------------------- when the phone misbehaves


def test_a_copy_cut_short_is_removed_not_left_to_look_finished(conn, library, phones, target) -> None:
    a = add(conn, library, "one.mp3", 400, title="One", track_number=1)
    device_id = new_device(conn)
    control(phones, fail_put={"match": "01 - One", "code": "failed", "partial": True})

    _, result = sync(conn, device_id, target, rows(conn, a))

    assert result.copied == 0 and len(result.errors) == 1 and result.aborted is None
    assert not list(on_device(phones).rglob("*.mp3")), "half a song at its final name would play as a broken song"
    assert manifest(conn, device_id) == {}, "nothing is recorded as copied"


def test_one_bad_copy_does_not_stop_the_others(conn, library, phones, target) -> None:
    ids = [add(conn, library, f"s{n}.mp3", 100, title=f"S{n}", track_number=n) for n in range(1, 4)]
    device_id = new_device(conn)
    control(phones, fail_put={"match": "02 - S2", "code": "failed", "partial": True})

    _, result = sync(conn, device_id, target, rows(conn, *ids))

    assert result.copied == 2 and len(result.errors) == 1
    assert sorted(manifest(conn, device_id)) == [ids[0], ids[2]]


def test_a_full_phone_stops_the_run_and_keeps_what_was_copied(conn, library, phones) -> None:
    write_device(phones, "SMALL", capacity=250, name="Small")
    ids = [add(conn, library, f"s{n}.mp3", 100, title=f"S{n}", track_number=n) for n in range(1, 5)]
    device_id = mirror.get_or_create_device(conn, "mtp:SMALL", "Small")

    with MtpHelper([sys.executable, str(FAKE), str(phones)]) as helper:
        small = MtpTarget.connect(helper, serial="SMALL", storage="s1", base="Music")
        plan = mirror.plan_sync(conn, device_id, small, rows(conn, *ids), SCHEME, FREE)  # the plan was told there is room
        result = mirror.run_sync(conn, device_id, small, plan)

    assert result.copied == 2 and "full" in result.aborted
    assert sorted(manifest(conn, device_id)) == ids[:2]


def test_an_unplugged_phone_stops_the_run(conn, library, phones, target) -> None:
    ids = [add(conn, library, f"s{n}.mp3", 100, title=f"S{n}", track_number=n) for n in range(1, 5)]
    device_id = new_device(conn)

    def pulled_out_after_the_first(done, total, copied):
        if done == 1:
            control(phones, unplugged=[SERIAL])

    plan = plan_for(conn, device_id, target, rows(conn, *ids))
    result = mirror.run_sync(conn, device_id, target, plan, on_progress=pulled_out_after_the_first)

    assert result.copied == 1 and "stopped answering" in result.aborted
    assert result.errors[-1]["error"] == "The device was disconnected"
    assert sorted(manifest(conn, device_id)) == ids[:1]


def test_cancelling_keeps_what_was_copied_so_far(conn, library, target) -> None:
    ids = [add(conn, library, f"s{n}.mp3", 100, title=f"S{n}", track_number=n) for n in range(1, 5)]
    device_id = new_device(conn)

    class Cancelled(Exception):
        pass

    def stop_after_two(done, total, copied):
        if done >= 2:
            raise Cancelled

    plan = plan_for(conn, device_id, target, rows(conn, *ids))
    with pytest.raises(Cancelled):
        mirror.run_sync(conn, device_id, target, plan, on_progress=stop_after_two)

    assert sorted(manifest(conn, device_id)) == ids[:2]
    again = plan_for(conn, device_id, target, rows(conn, *ids))
    assert (len(again.to_copy), again.unchanged) == (2, 2), "the next sync picks up where this one stopped"


# ------------------------------------------------------------------------------------------- what cannot go


def test_names_that_differ_only_in_case_are_the_same_name_on_a_phone(conn, library, target) -> None:
    first = add(conn, library, "a/one.mp3", title="Song", track_number=1)
    second = add(conn, library, "b/two.mp3", title="SONG", track_number=1)
    device_id = new_device(conn)

    plan, result = sync(conn, device_id, target, rows(conn, first, second))

    assert result.copied == 1
    assert [(s.track_id, s.reason) for s in plan.skipped] == [(second, "collision")]


def test_a_song_whose_file_is_gone_is_skipped_not_fatal(conn, library, target) -> None:
    here = add(conn, library, "here.mp3", title="Here", track_number=1)
    gone = add(conn, library, "gone.mp3", title="Gone", track_number=2)
    (library / "gone.mp3").unlink()
    device_id = new_device(conn)

    plan, result = sync(conn, device_id, target, rows(conn, here, gone))

    assert result.copied == 1 and plan.skipped[0].reason == "source_missing"


# ------------------------------------------------------------------------------------------- removing and moving


def test_removal_only_happens_when_asked_and_only_touches_files_the_app_copied(conn, library, phones, target) -> None:
    keep = add(conn, library, "keep.mp3", title="Keep", track_number=1)
    drop = add(conn, library, "drop.mp3", title="Drop", track_number=2)
    device_id = new_device(conn)
    sync(conn, device_id, target, rows(conn, keep, drop))
    stranger = on_device(phones, "Other", "mine.mp3")
    stranger.parent.mkdir(parents=True)
    stranger.write_bytes(b"not copied by this app")

    plan, kept = sync(conn, device_id, target, rows(conn, keep), prune=False)
    assert len(plan.to_prune) == 1 and kept.pruned == 0 and on_device(phones, "Artist", "Album", "02 - Drop.mp3").exists()

    _, pruned = sync(conn, device_id, target, rows(conn, keep), prune=True)

    assert pruned.pruned == 1 and not on_device(phones, "Artist", "Album", "02 - Drop.mp3").exists()
    assert on_device(phones, "Artist", "Album", "01 - Keep.mp3").exists() and stranger.read_bytes() == b"not copied by this app"
    assert sorted(manifest(conn, device_id)) == [keep]


def test_emptied_folders_are_tidied_but_never_the_music_folder_itself(conn, library, phones, target) -> None:
    solo = add(conn, library, "solo.mp3", title="Solo", album="Lonely", track_number=1)
    device_id = new_device(conn)
    sync(conn, device_id, target, rows(conn, solo))

    sync(conn, device_id, target, [], prune=True)

    assert not on_device(phones, "Artist").exists(), "the artist and album folders went with the last song"
    assert on_device(phones).is_dir(), "the Music folder is the person's"


def test_a_folder_holding_something_else_is_kept(conn, library, phones, target) -> None:
    solo = add(conn, library, "solo.mp3", title="Solo", album="Shared", track_number=1)
    device_id = new_device(conn)
    sync(conn, device_id, target, rows(conn, solo))
    (on_device(phones, "Artist", "Shared") / "booklet.pdf").write_bytes(b"pdf")

    sync(conn, device_id, target, [], prune=True)

    assert (on_device(phones, "Artist", "Shared") / "booklet.pdf").exists()
    assert not (on_device(phones, "Artist", "Shared") / "01 - Solo.mp3").exists()


def test_a_changed_layout_moves_the_copy_and_removes_the_old_one(conn, library, phones, target) -> None:
    a = add(conn, library, "one.mp3", title="One", track_number=1)
    device_id = new_device(conn)
    sync(conn, device_id, target, rows(conn, a))

    plan = mirror.plan_sync(conn, device_id, target, rows(conn, a), "{artist}/{title}.{ext}", FREE)
    assert plan.to_copy[0].reason == "moved"
    mirror.run_sync(conn, device_id, target, plan)

    assert on_device(phones, "Artist", "One.mp3").exists() and not on_device(phones, "Artist", "Album").exists()
    assert manifest(conn, device_id)[a] == "Artist/One.mp3"


def test_the_old_copy_stays_if_the_new_one_cannot_be_made(conn, library, phones, target) -> None:
    a = add(conn, library, "one.mp3", title="One", track_number=1)
    device_id = new_device(conn)
    sync(conn, device_id, target, rows(conn, a))
    plan = mirror.plan_sync(conn, device_id, target, rows(conn, a), "{artist}/{title}.{ext}", FREE)
    control(phones, fail_put={"match": "One.mp3", "code": "failed", "partial": True})

    result = mirror.run_sync(conn, device_id, target, plan)

    assert result.copied == 0 and on_device(phones, "Artist", "Album", "01 - One.mp3").exists()
    assert not on_device(phones, "Artist", "One.mp3").exists()


# ------------------------------------------------------------------------------------------- covers and playlists


def test_the_folder_picture_goes_along_once_and_is_never_overwritten(conn, library, phones, target) -> None:
    a = add(conn, library, "album/one.mp3", title="One", track_number=1)
    b = add(conn, library, "album/two.mp3", title="Two", track_number=2)
    (library / "album" / "cover.jpg").write_bytes(b"library cover")
    device_id = new_device(conn)
    folder = on_device(phones, "Artist", "Album")
    folder.mkdir(parents=True)
    (folder / "folder.jpg").write_bytes(b"already there")

    _, first = sync(conn, device_id, target, rows(conn, a))
    assert first.covers == 0, "the phone's folder already has a picture"
    (folder / "folder.jpg").unlink()
    _, second = sync(conn, device_id, target, rows(conn, a, b))

    assert second.covers == 1 and (folder / "cover.jpg").read_bytes() == b"library cover"


def test_playlists_are_written_with_paths_relative_to_the_playlist_folder(conn, library, phones, target) -> None:
    a = add(conn, library, "one.mp3", title="One", track_number=1)
    b = add(conn, library, "two.mp3", title="Two", track_number=2, album="Other")
    c = add(conn, library, "three.mp3", title="Three", track_number=3)  # not synced
    device_id = new_device(conn)
    sync(conn, device_id, target, rows(conn, a, b))

    written, names = mirror.write_playlists(conn, device_id, target, [("Road: trip?", [b, a, c])])

    assert (written, names) == (1, ["Road_ trip_.m3u8"])
    lines = on_device(phones, "Playlists", "Road_ trip_.m3u8").read_text(encoding="utf-8").splitlines()
    assert lines == ["#EXTM3U", "../Artist/Other/02 - Two.mp3", "../Artist/Album/01 - One.mp3"]


def test_a_playlist_is_replaced_not_duplicated_and_old_ones_can_go(conn, library, phones, target) -> None:
    a = add(conn, library, "one.mp3", title="One", track_number=1)
    b = add(conn, library, "two.mp3", title="Two", track_number=2)
    device_id = new_device(conn)
    sync(conn, device_id, target, rows(conn, a, b))
    mirror.write_playlists(conn, device_id, target, [("Mix", [a]), ("Old", [a])])
    mirror.write_playlists(conn, device_id, target, [("Mix", [a, b])])
    mine = on_device(phones, "Playlists", "Mine.m3u8")
    mine.write_text("#EXTM3U\n")  # the person's own, not written by the app

    assert len(on_device(phones, "Playlists", "Mix.m3u8").read_text(encoding="utf-8").splitlines()) == 3
    assert mirror.remove_playlists(target, ["Old.m3u8", "Gone.m3u8"]) == 1
    assert sorted(p.name for p in on_device(phones, "Playlists").iterdir()) == ["Mine.m3u8", "Mix.m3u8"]


# ------------------------------------------------------------------------------------------- the device itself


def test_free_space_is_what_the_phone_reports(conn, library, phones, target) -> None:
    assert target.free_space() == 10_000_000
    a = add(conn, library, "one.mp3", 4000)
    sync(conn, new_device(conn), target, rows(conn, a))

    assert target.free_space() == 10_000_000 - 4000
    assert target.capacity() == 10_000_000


def test_a_missing_storage_is_explained(helper) -> None:
    with pytest.raises(OSError) as refused:
        MtpTarget.connect(helper, serial=SERIAL, storage="no-such-storage", base="Music")

    assert refused.value.errno == errno.ENODEV and "File transfer" in str(refused.value)
