from pathlib import Path

from musictoolkit.db.connection import connect
from musictoolkit.sync import mirror

SCHEME = "{album_artist}/{album}/{track:02d} - {title}.{ext}"


def _insert_track(conn, file_path: str, **overrides) -> int:
    fields = {
        "album_artist": None, "artist": None, "album": None, "title": None,
        "track_number": None, "file_size": 100, "file_hash": None, "is_missing": 0,
    }
    fields.update(overrides)
    columns = ", ".join(["file_path"] + list(fields.keys()))
    placeholders = ", ".join(["?"] * (len(fields) + 1))
    conn.execute(f"INSERT INTO tracks ({columns}) VALUES ({placeholders})", [file_path] + list(fields.values()))
    conn.commit()
    return conn.execute("SELECT * FROM tracks WHERE file_path = ?", (file_path,)).fetchone()["id"]


def test_sanitize_for_fat_strips_invalid_chars_and_truncates() -> None:
    assert mirror.sanitize_for_fat('Song: "Title"?') == "Song_ _Title__"
    assert len(mirror.sanitize_for_fat("x" * 300)) == 255


def test_plan_and_apply_sync_copies_new_files(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    device = tmp_path / "device"
    device.mkdir()

    source = library / "raw.mp3"
    source.write_bytes(b"audio-bytes")
    track_id = _insert_track(
        conn, str(source.resolve()),
        artist="Artist", album_artist="Artist", album="Album", title="Title",
        track_number=1, file_size=source.stat().st_size,
    )
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()

    device_id = mirror.get_or_create_device(conn, str(device))
    plan = mirror.plan_sync(conn, device_id, device, [row], SCHEME, free_bytes_on_device=10_000)

    assert len(plan.to_copy) == 1
    assert mirror.has_sufficient_space(plan)

    copied, pruned = mirror.apply_sync(conn, device_id, device, plan, prune=False)
    assert copied == 1
    assert pruned == 0
    assert (device / "Artist" / "Album" / "01 - Title.mp3").exists()

    manifest_row = conn.execute(
        "SELECT * FROM sync_manifest WHERE device_id = ? AND track_id = ?", (device_id, track_id)
    ).fetchone()
    assert manifest_row["status"] == "synced"
    conn.close()


def test_resync_with_no_changes_is_a_noop(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    device = tmp_path / "device"
    device.mkdir()
    source = library / "raw.mp3"
    source.write_bytes(b"audio-bytes")
    track_id = _insert_track(
        conn, str(source.resolve()), artist="Artist", album="Album", title="Title", track_number=1,
        file_size=source.stat().st_size,
    )
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    device_id = mirror.get_or_create_device(conn, str(device))

    plan1 = mirror.plan_sync(conn, device_id, device, [row], SCHEME, 10_000)
    mirror.apply_sync(conn, device_id, device, plan1, prune=False)

    row_again = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    plan2 = mirror.plan_sync(conn, device_id, device, [row_again], SCHEME, 10_000)

    assert len(plan2.to_copy) == 0
    assert plan2.unchanged == 1
    conn.close()


def test_deselecting_a_track_prunes_it_on_next_sync_with_prune_flag(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    device = tmp_path / "device"
    device.mkdir()
    source = library / "raw.mp3"
    source.write_bytes(b"audio-bytes")
    track_id = _insert_track(
        conn, str(source.resolve()), artist="Artist", album="Album", title="Title", track_number=1,
        file_size=source.stat().st_size,
    )
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    device_id = mirror.get_or_create_device(conn, str(device))

    plan1 = mirror.plan_sync(conn, device_id, device, [row], SCHEME, 10_000)
    mirror.apply_sync(conn, device_id, device, plan1, prune=False)
    synced_path = device / "Artist" / "Album" / "01 - Title.mp3"
    assert synced_path.exists()

    # Track no longer selected (empty selection this time).
    plan2 = mirror.plan_sync(conn, device_id, device, [], SCHEME, 10_000)
    assert len(plan2.to_prune) == 1

    copied, pruned = mirror.apply_sync(conn, device_id, device, plan2, prune=True)
    assert pruned == 1
    assert not synced_path.exists()
    assert conn.execute("SELECT COUNT(*) AS c FROM sync_manifest").fetchone()["c"] == 0
    conn.close()


def test_prune_not_applied_without_prune_flag(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    device = tmp_path / "device"
    device.mkdir()
    source = library / "raw.mp3"
    source.write_bytes(b"audio-bytes")
    track_id = _insert_track(
        conn, str(source.resolve()), artist="Artist", album="Album", title="Title", track_number=1,
        file_size=source.stat().st_size,
    )
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    device_id = mirror.get_or_create_device(conn, str(device))
    plan1 = mirror.plan_sync(conn, device_id, device, [row], SCHEME, 10_000)
    mirror.apply_sync(conn, device_id, device, plan1, prune=False)
    synced_path = device / "Artist" / "Album" / "01 - Title.mp3"

    plan2 = mirror.plan_sync(conn, device_id, device, [], SCHEME, 10_000)
    copied, pruned = mirror.apply_sync(conn, device_id, device, plan2, prune=False)

    assert pruned == 0
    assert synced_path.exists()  # untouched — prune wasn't requested
    conn.close()


def test_retag_changes_dest_path_and_removes_stale_copy(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    library = tmp_path / "library"
    library.mkdir()
    device = tmp_path / "device"
    device.mkdir()
    source = library / "raw.mp3"
    source.write_bytes(b"audio-bytes")
    track_id = _insert_track(
        conn, str(source.resolve()), artist="Artist", album="Album", title="Old Title", track_number=1,
        file_size=source.stat().st_size,
    )
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    device_id = mirror.get_or_create_device(conn, str(device))
    plan1 = mirror.plan_sync(conn, device_id, device, [row], SCHEME, 10_000)
    mirror.apply_sync(conn, device_id, device, plan1, prune=False)
    old_path = device / "Artist" / "Album" / "01 - Old Title.mp3"
    assert old_path.exists()

    # Retag the track (title changes -> new computed destination path).
    conn.execute("UPDATE tracks SET title = 'New Title' WHERE id = ?", (track_id,))
    conn.commit()
    row2 = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()

    plan2 = mirror.plan_sync(conn, device_id, device, [row2], SCHEME, 10_000)
    assert len(plan2.to_copy) == 1
    mirror.apply_sync(conn, device_id, device, plan2, prune=False)

    new_path = device / "Artist" / "Album" / "01 - New Title.mp3"
    assert new_path.exists()
    assert not old_path.exists()
    conn.close()


def test_has_sufficient_space() -> None:
    plan = mirror.SyncPlan(total_bytes_to_copy=500, free_bytes_on_device=1000)
    assert mirror.has_sufficient_space(plan)
    plan2 = mirror.SyncPlan(total_bytes_to_copy=2000, free_bytes_on_device=1000)
    assert not mirror.has_sufficient_space(plan2)
