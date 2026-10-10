"""Syncing to a device: what is previewed, what is copied, what is removed, and what survives an interruption."""

from __future__ import annotations

import errno
import shutil
from pathlib import Path

import pytest

from musictoolkit.db.connection import connect
from musictoolkit.sync import mirror, selection, targets

SCHEME = "{album_artist}/{album}/{track:02d} - {title}.{ext}"
FREE = 10**9


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


@pytest.fixture
def device(tmp_path: Path) -> Path:
    root = (tmp_path / "device").resolve()
    root.mkdir()
    return root


def add(conn, library: Path, name: str, size: int = 100, **tags) -> int:
    path = library / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(range(256)) * (size // 256 + 1))
    path.write_bytes(path.read_bytes()[:size])
    tags = {"artist": "Artist", "album_artist": "Artist", "album": "Album", "title": name.rsplit(".", 1)[0], "track_number": 1, **tags}
    columns = {**tags, "file_size": size, "is_missing": 0}
    names = ", ".join(["file_path", *columns])
    conn.execute(f"INSERT INTO tracks ({names}) VALUES ({', '.join('?' * (len(columns) + 1))})", [str(path), *columns.values()])
    conn.commit()
    return conn.execute("SELECT id FROM tracks WHERE file_path = ?", (str(path),)).fetchone()["id"]


def rows(conn, *ids: int):
    return [conn.execute("SELECT * FROM tracks WHERE id = ?", (i,)).fetchone() for i in ids]


def plan_for(conn, device_id, device, selected, scheme=SCHEME, fs=None, free=FREE):
    return mirror.plan_sync(conn, device_id, device, selected, scheme, free, fs=fs)


def sync(conn, device_id, device, selected, prune=False, **kw):
    plan = plan_for(conn, device_id, device, selected)
    return plan, mirror.run_sync(conn, device_id, device, plan, prune=prune, **kw)


def manifest(conn, device_id) -> dict[int, str]:
    return {r["track_id"]: r["dest_relative_path"] for r in conn.execute("SELECT * FROM sync_manifest WHERE device_id = ?", (device_id,))}


# ------------------------------------------------------------------------------------------- the first sync


def test_a_first_sync_copies_everything_under_the_folder_layout(conn, library, device) -> None:
    a = add(conn, library, "one.mp3", 300, title="One", track_number=1)
    b = add(conn, library, "two.mp3", 500, title='Two: "Live"?', track_number=2)
    device_id = mirror.get_or_create_device(conn, str(device))

    plan, result = sync(conn, device_id, device, rows(conn, a, b))

    assert (len(plan.to_copy), plan.total_bytes_to_copy, plan.unchanged, plan.skipped) == (2, 800, 0, [])
    assert (result.copied, result.bytes_copied, result.errors, result.aborted) == (2, 800, [], None)
    assert (device / "Artist" / "Album" / "01 - One.mp3").stat().st_size == 300
    assert (device / "Artist" / "Album" / '02 - Two_ _Live__.mp3').exists(), "characters FAT cannot hold are replaced"
    assert not list(device.rglob("*" + mirror.PARTIAL_SUFFIX))
    saved = conn.execute("SELECT * FROM sync_manifest WHERE track_id = ?", (a,)).fetchone()
    assert (saved["status"], saved["size"]) == ("synced", 300)
    assert conn.execute("SELECT last_synced_at FROM devices WHERE id = ?", (device_id,)).fetchone()[0]


def test_a_second_sync_copies_nothing_new(conn, library, device) -> None:
    a = add(conn, library, "one.mp3")
    device_id = mirror.get_or_create_device(conn, str(device))
    sync(conn, device_id, device, rows(conn, a))

    plan = plan_for(conn, device_id, device, rows(conn, a))

    assert (plan.to_copy, plan.unchanged, plan.to_prune) == ([], 1, [])


def test_the_reason_each_song_will_be_copied_is_named(conn, library, device) -> None:
    gone = add(conn, library, "gone.mp3", title="Gone", track_number=1)
    cut = add(conn, library, "cut.mp3", title="Cut", track_number=2)
    edited = add(conn, library, "edited.mp3", title="Edited", track_number=3)
    fresh = add(conn, library, "fresh.mp3", title="Fresh", track_number=4)
    device_id = mirror.get_or_create_device(conn, str(device))
    sync(conn, device_id, device, rows(conn, gone, cut, edited))
    (device / "Artist" / "Album" / "01 - Gone.mp3").unlink()
    (device / "Artist" / "Album" / "02 - Cut.mp3").write_bytes(b"cut short")
    path = Path(conn.execute("SELECT file_path FROM tracks WHERE id = ?", (edited,)).fetchone()[0])
    path.write_bytes(path.read_bytes() + b"more")
    shutil.copystat(path, path)  # keep the mtime change that the write made

    plan = plan_for(conn, device_id, device, rows(conn, gone, cut, edited, fresh))

    reasons = {item.row["id"]: item.reason for item in plan.to_copy}
    assert reasons == {gone: "missing", cut: "damaged", edited: "changed", fresh: "new"}


def test_an_interrupted_copy_leaves_no_half_song_behind(conn, library, device, monkeypatch) -> None:
    a = add(conn, library, "one.mp3", 400)
    device_id = mirror.get_or_create_device(conn, str(device))

    def dies_halfway(source, dest):
        Path(dest).write_bytes(b"half")
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(targets.shutil, "copyfile", dies_halfway)
    _, result = sync(conn, device_id, device, rows(conn, a))

    assert result.copied == 0 and len(result.errors) == 1
    assert not list(device.rglob("*.mp3")) and not list(device.rglob("*" + mirror.PARTIAL_SUFFIX))
    assert manifest(conn, device_id) == {}, "nothing is recorded as copied"


def test_a_full_device_stops_the_run_and_keeps_what_was_copied(conn, library, device, monkeypatch) -> None:
    ids = [add(conn, library, f"s{n}.mp3", 100, title=f"S{n}", track_number=n) for n in range(1, 5)]
    device_id = mirror.get_or_create_device(conn, str(device))
    real = targets._copy_file
    calls = []

    def fills_up(source, dest):
        calls.append(dest)
        if len(calls) == 3:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(source, dest)

    monkeypatch.setattr(targets, "_copy_file", fills_up)
    _, result = sync(conn, device_id, device, rows(conn, *ids))

    assert (result.copied, len(calls)) == (2, 3)
    assert "full" in result.aborted
    assert sorted(manifest(conn, device_id)) == ids[:2]


def test_an_unplugged_device_stops_the_run(conn, library, device, monkeypatch) -> None:
    ids = [add(conn, library, f"s{n}.mp3", 100, title=f"S{n}", track_number=n) for n in range(1, 4)]
    device_id = mirror.get_or_create_device(conn, str(device))

    def vanishes(source, dest):
        shutil.rmtree(device)
        raise OSError(errno.EIO, "The device is not ready")

    monkeypatch.setattr(targets, "_copy_file", vanishes)
    _, result = sync(conn, device_id, device, rows(conn, *ids))

    assert result.copied == 0 and "unplugged" in result.aborted and len(result.errors) == 1


def test_cancelling_keeps_what_was_copied_so_far(conn, library, device) -> None:
    ids = [add(conn, library, f"s{n}.mp3", 100, title=f"S{n}", track_number=n) for n in range(1, 5)]
    device_id = mirror.get_or_create_device(conn, str(device))

    class Cancelled(Exception):
        pass

    def stop_after_two(done, total, copied):
        if done >= 2:
            raise Cancelled

    plan = plan_for(conn, device_id, device, rows(conn, *ids))
    with pytest.raises(Cancelled):
        mirror.run_sync(conn, device_id, device, plan, on_progress=stop_after_two)

    assert sorted(manifest(conn, device_id)) == ids[:2]
    again = plan_for(conn, device_id, device, rows(conn, *ids))
    assert (len(again.to_copy), again.unchanged) == (2, 2), "the next sync picks up where this one stopped"


# ------------------------------------------------------------------------------------------- what cannot go


def test_two_songs_that_would_share_a_name_are_not_both_copied(conn, library, device) -> None:
    first = add(conn, library, "a/same.mp3", title="Same", track_number=1)
    second = add(conn, library, "b/same.mp3", title="Same", track_number=1)
    device_id = mirror.get_or_create_device(conn, str(device))

    plan, result = sync(conn, device_id, device, rows(conn, first, second))

    assert result.copied == 1
    assert [(s.track_id, s.reason) for s in plan.skipped] == [(second, "collision")]


def test_a_file_too_big_for_fat32_is_left_out_with_a_reason(conn, library, device, monkeypatch) -> None:
    monkeypatch.setattr(mirror, "FAT_FILE_LIMIT", 150)
    small = add(conn, library, "small.mp3", 100, title="Small", track_number=1)
    big = add(conn, library, "big.mp3", 300, title="Big", track_number=2)
    device_id = mirror.get_or_create_device(conn, str(device))

    on_fat = plan_for(conn, device_id, device, rows(conn, small, big), fs="FAT32")
    on_exfat = plan_for(conn, device_id, device, rows(conn, small, big), fs="exFAT")

    assert [i.row["id"] for i in on_fat.to_copy] == [small]
    assert [(s.track_id, s.reason) for s in on_fat.skipped] == [(big, "too_big")]
    assert len(on_exfat.to_copy) == 2 and on_exfat.skipped == []


def test_a_song_whose_file_is_gone_is_skipped_not_fatal(conn, library, device) -> None:
    here = add(conn, library, "here.mp3", title="Here", track_number=1)
    gone = add(conn, library, "gone.mp3", title="Gone", track_number=2)
    (library / "gone.mp3").unlink()
    device_id = mirror.get_or_create_device(conn, str(device))

    plan = plan_for(conn, device_id, device, rows(conn, here, gone))

    assert len(plan.to_copy) == 1 and plan.skipped[0].reason == "source_missing"


def test_a_path_too_long_for_windows_is_left_out(conn, library, tmp_path) -> None:
    deep = tmp_path / ("d" * 120) / ("e" * 120)
    deep.mkdir(parents=True)
    a = add(conn, library, "x.mp3", title="T" * 60)
    device_id = mirror.get_or_create_device(conn, str(deep))

    plan = plan_for(conn, device_id, deep, rows(conn, a))

    assert plan.to_copy == [] and plan.skipped[0].reason == "path_too_long"


def test_space_needed_counts_what_removal_gives_back(conn, library, device) -> None:
    keep = add(conn, library, "keep.mp3", 400, title="Keep", track_number=1)
    drop = add(conn, library, "drop.mp3", 600, title="Drop", track_number=2)
    new = add(conn, library, "new.mp3", 500, title="New", track_number=3)
    device_id = mirror.get_or_create_device(conn, str(device))
    sync(conn, device_id, device, rows(conn, keep, drop))

    plan = mirror.plan_sync(conn, device_id, device, rows(conn, keep, new), SCHEME, free_bytes_on_device=100)

    assert (plan.total_bytes_to_copy, plan.prune_bytes) == (500, 600)
    assert not mirror.has_sufficient_space(plan) and mirror.has_sufficient_space(plan, prune=True)


# ------------------------------------------------------------------------------------------- removing and moving


def test_removal_only_happens_when_asked_and_only_touches_files_the_app_copied(conn, library, device) -> None:
    keep = add(conn, library, "keep.mp3", title="Keep", track_number=1)
    drop = add(conn, library, "drop.mp3", title="Drop", track_number=2)
    device_id = mirror.get_or_create_device(conn, str(device))
    sync(conn, device_id, device, rows(conn, keep, drop))
    stranger = device / "Other" / "mine.mp3"
    stranger.parent.mkdir()
    stranger.write_bytes(b"not copied by this app")

    plan, kept = sync(conn, device_id, device, rows(conn, keep), prune=False)
    assert len(plan.to_prune) == 1 and kept.pruned == 0 and (device / "Artist" / "Album" / "02 - Drop.mp3").exists()

    _, pruned = sync(conn, device_id, device, rows(conn, keep), prune=True)

    assert pruned.pruned == 1 and not (device / "Artist" / "Album" / "02 - Drop.mp3").exists()
    assert (device / "Artist" / "Album" / "01 - Keep.mp3").exists() and stranger.read_bytes() == b"not copied by this app"
    assert sorted(manifest(conn, device_id)) == [keep]


def test_emptied_folders_on_the_device_are_tidied_up(conn, library, device) -> None:
    solo = add(conn, library, "solo.mp3", title="Solo", album="Lonely", track_number=1)
    stay = add(conn, library, "stay.mp3", title="Stay", album="Home", track_number=1)
    device_id = mirror.get_or_create_device(conn, str(device))
    sync(conn, device_id, device, rows(conn, solo, stay))

    sync(conn, device_id, device, rows(conn, stay), prune=True)

    assert not (device / "Artist" / "Lonely").exists() and (device / "Artist" / "Home").is_dir()


def test_a_changed_layout_moves_the_copy_and_removes_the_old_one(conn, library, device) -> None:
    a = add(conn, library, "one.mp3", title="One", track_number=1)
    device_id = mirror.get_or_create_device(conn, str(device))
    sync(conn, device_id, device, rows(conn, a))

    plan = plan_for(conn, device_id, device, rows(conn, a), scheme="{artist}/{title}.{ext}")
    assert plan.to_copy[0].reason == "moved"
    mirror.run_sync(conn, device_id, device, plan)

    assert (device / "Artist" / "One.mp3").exists() and not (device / "Artist" / "Album").exists()
    assert manifest(conn, device_id)[a].replace("\\", "/") == "Artist/One.mp3"


def test_the_old_copy_stays_if_the_new_one_cannot_be_made(conn, library, device, monkeypatch) -> None:
    a = add(conn, library, "one.mp3", title="One", track_number=1)
    device_id = mirror.get_or_create_device(conn, str(device))
    sync(conn, device_id, device, rows(conn, a))
    plan = plan_for(conn, device_id, device, rows(conn, a), scheme="{artist}/{title}.{ext}")

    def refuses(source, dest):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(targets, "_copy_file", refuses)
    result = mirror.run_sync(conn, device_id, device, plan)

    assert result.copied == 0 and (device / "Artist" / "Album" / "01 - One.mp3").exists()


def test_removal_never_leaves_the_device_folder(conn, library, device, tmp_path) -> None:
    outside = tmp_path / "precious.mp3"
    outside.write_bytes(b"x")

    targets._remove_stale(device, "../precious.mp3")

    assert outside.exists()


# ------------------------------------------------------------------------------------------- covers and playlists


def test_the_folder_picture_goes_along_once_and_is_never_overwritten(conn, library, device) -> None:
    a = add(conn, library, "album/one.mp3", title="One", track_number=1)
    b = add(conn, library, "album/two.mp3", title="Two", track_number=2)
    (library / "album" / "cover.jpg").write_bytes(b"library cover")
    device_id = mirror.get_or_create_device(conn, str(device))
    (device / "Artist" / "Album").mkdir(parents=True)
    (device / "Artist" / "Album" / "folder.jpg").write_bytes(b"already there")

    _, first = sync(conn, device_id, device, rows(conn, a))
    assert first.covers == 0, "the device folder already has a picture"
    (device / "Artist" / "Album" / "folder.jpg").unlink()
    _, second = sync(conn, device_id, device, rows(conn, a, b))

    assert second.covers == 1 and (device / "Artist" / "Album" / "cover.jpg").read_bytes() == b"library cover"


def test_covers_can_be_left_behind(conn, library, device) -> None:
    a = add(conn, library, "album/one.mp3", title="One", track_number=1)
    (library / "album" / "cover.jpg").write_bytes(b"library cover")
    device_id = mirror.get_or_create_device(conn, str(device))

    _, result = sync(conn, device_id, device, rows(conn, a), copy_covers=False)

    assert result.covers == 0 and not (device / "Artist" / "Album" / "cover.jpg").exists()


def test_playlists_are_written_with_paths_relative_to_the_playlist_folder(conn, library, device) -> None:
    a = add(conn, library, "one.mp3", title="One", track_number=1)
    b = add(conn, library, "two.mp3", title="Two", track_number=2, album="Other")
    c = add(conn, library, "three.mp3", title="Three", track_number=3)  # not synced
    device_id = mirror.get_or_create_device(conn, str(device))
    sync(conn, device_id, device, rows(conn, a, b))

    written, names = mirror.write_playlists(conn, device_id, device, [("Road: trip?", [b, a, c])])

    assert (written, names) == (1, ["Road_ trip_.m3u8"])
    lines = (device / "Playlists" / "Road_ trip_.m3u8").read_text(encoding="utf-8").splitlines()
    assert lines == ["#EXTM3U", "../Artist/Other/02 - Two.mp3", "../Artist/Album/01 - One.mp3"]


def test_playlists_the_app_wrote_earlier_can_be_removed(conn, library, device) -> None:
    a = add(conn, library, "one.mp3")
    device_id = mirror.get_or_create_device(conn, str(device))
    sync(conn, device_id, device, rows(conn, a))
    mirror.write_playlists(conn, device_id, device, [("Old", [a]), ("Keep", [a])])
    (device / "Playlists" / "Mine.m3u8").write_text("#EXTM3U\n")  # the person's own, not written by the app

    assert mirror.remove_playlists(device, ["Old.m3u8"]) == 1
    assert sorted(p.name for p in (device / "Playlists").iterdir()) == ["Keep.m3u8", "Mine.m3u8"]
    mirror.remove_playlists(device, ["Keep.m3u8"])
    assert (device / "Playlists" / "Mine.m3u8").exists()


# ------------------------------------------------------------------------------------------- choosing sources


def library_for_sources(conn, library):
    ids = {
        "rock1": add(conn, library, "r1.mp3", title="Rock One", genre="Rock", rating=5, favorite=1, track_number=1),
        "rock2": add(conn, library, "r2.mp3", title="Rock Two", genre="Rock", rating=2, track_number=2),
        "jazz": add(conn, library, "j1.mp3", title="Jazz One", genre="Jazz", rating=4, track_number=3),
        "plain": add(conn, library, "p1.mp3", title="Plain", track_number=4),
    }
    conn.execute("UPDATE tracks SET date_added = datetime('now', '-3 days') WHERE id = ?", (ids["rock1"],))
    conn.execute("UPDATE tracks SET date_added = datetime('now', '-400 days') WHERE id != ?", (ids["rock1"],))
    conn.commit()
    return ids


def titles(rows_):
    return [r["title"] for r in rows_]


def test_sources_pick_slices_of_the_library(conn, library) -> None:
    ids = library_for_sources(conn, library)
    pick = lambda *sources: titles(selection.resolve_sources(conn, list(sources)))  # noqa: E731

    assert pick({"kind": "all"}) == ["Rock One", "Rock Two", "Jazz One", "Plain"]
    assert pick({"kind": "favorites"}) == ["Rock One"]
    assert pick({"kind": "genre", "value": "rock"}) == ["Rock One", "Rock Two"]
    assert pick({"kind": "rating", "min": 4}) == ["Rock One", "Jazz One"]
    assert pick({"kind": "recent", "days": 30}) == ["Rock One"]
    assert ids


def test_sources_are_joined_without_repeats_and_playlists_keep_their_order(conn, library) -> None:
    ids = library_for_sources(conn, library)
    conn.execute("INSERT INTO playlists (name, source, kind) VALUES ('Mix', 'manual', 'manual')")
    for position, key in enumerate(["plain", "jazz", "rock1"]):
        conn.execute("INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (1, ?, ?)", (ids[key], position))
    conn.commit()

    got = selection.resolve_sources(conn, [{"kind": "playlist", "id": 1}, {"kind": "favorites"}, {"kind": "genre", "value": "Rock"}])

    assert titles(got) == ["Plain", "Jazz One", "Rock One", "Rock Two"]


def test_a_smart_playlist_is_worked_out_fresh(conn, library) -> None:
    library_for_sources(conn, library)
    rules = '{"match": "all", "rules": [{"field": "genre", "op": "is", "value": "Jazz"}]}'
    conn.execute("INSERT INTO playlists (name, source, kind, rules_json) VALUES ('Smart', 'manual', 'smart', ?)", (rules,))
    conn.commit()

    assert titles(selection.resolve_sources(conn, [{"kind": "playlist", "id": 1}])) == ["Jazz One"]
    add(conn, library, "j2.mp3", title="Jazz Two", genre="Jazz", track_number=9)
    assert titles(selection.resolve_sources(conn, [{"kind": "playlist", "id": 1}])) == ["Jazz One", "Jazz Two"]


def test_songs_flagged_missing_are_never_selected(conn, library) -> None:
    ids = library_for_sources(conn, library)
    conn.execute("UPDATE tracks SET is_missing = 1 WHERE id = ?", (ids["plain"],))
    conn.commit()

    assert "Plain" not in titles(selection.resolve_sources(conn, [{"kind": "all"}]))


@pytest.mark.parametrize(
    "sources",
    [
        [],
        [{"kind": "nonsense"}],
        [{"kind": "playlist"}],
        [{"kind": "playlist", "id": True}],
        [{"kind": "genre", "value": "  "}],
        [{"kind": "rating", "min": 9}],
        [{"kind": "recent", "days": 0}],
        ["all"],
        [{"kind": "all"}] * 51,
    ],
)
def test_nonsense_sources_are_refused(conn, sources) -> None:
    with pytest.raises(selection.BadSource):
        selection.resolve_sources(conn, sources)


def test_which_playlists_a_selection_includes() -> None:
    sources = [{"kind": "all"}, {"kind": "playlist", "id": 3}, {"kind": "playlist", "id": 5}, {"kind": "playlist", "id": "x"}]
    assert selection.playlist_sources(sources) == [3, 5]
