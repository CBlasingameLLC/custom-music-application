"""The device sync API: add a device, choose what goes on it, preview, run, and the safety checks around that."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from musictoolkit.sync import device_detect, mirror
from musictoolkit.web.routers import devices as devices_router
from tests.conftest import ids_by_title

ALL = [{"kind": "all"}]
FOLK = [{"kind": "genre", "value": "Folk"}]  # the two Back Roads songs
ELECTRONIC = [{"kind": "genre", "value": "Electronic"}]  # the three Northern Lights songs


@pytest.fixture
def card(tmp_path: Path) -> Path:
    folder = tmp_path / "walkman" / "MUSIC"
    folder.mkdir(parents=True)
    return folder.resolve()


def add_device(web, card: Path, label: str | None = None):
    body = {"path": str(card)}
    if label:
        body["label"] = label
    response = web.client.post("/api/devices", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def finish(web, response, expect: str = "done"):
    assert response.status_code == 200, response.text
    job = web.ctx.jobs.wait(response.json()["job"]["id"], timeout=30)
    assert job.status == expect, job.error
    return job


def preview(web, device, sources=ALL, **extra):
    return finish(web, web.client.post(f"/api/devices/{device['id']}/preview", json={"sources": sources, **extra}))


def run_sync(web, device, job, **extra):
    return web.client.post(f"/api/devices/{device['id']}/sync", json={"job_id": job.id, **extra})


def copied_files(card: Path) -> list[str]:
    return sorted(p.relative_to(card).as_posix() for p in card.rglob("*") if p.is_file())


# ------------------------------------------------------------------------------------------- adding devices


def test_a_folder_becomes_a_device_with_room_and_nothing_synced(web, card) -> None:
    device = add_device(web, card)

    assert device["label"] == "MUSIC" and device["path"] == str(card)
    assert device["connected"] is True and device["synced"] == 0 and device["free"] > 0
    assert device["prefs"] == {"sources": [], "scheme": None, "playlists": True, "covers": True}
    assert [d["id"] for d in web.client.get("/api/devices").json()["items"]] == [device["id"]]


def test_a_new_device_is_named_for_its_folder_or_for_a_whole_drive_after_the_drive() -> None:
    folder = Path("/media/walkman/MUSIC")
    assert devices_router._label_for(folder, "WALKMAN", None) == "MUSIC", "a folder on a drive keeps its own name"
    assert devices_router._label_for(Path("/"), "WALKMAN", None) == "WALKMAN", "a whole drive takes the drive's name"
    assert devices_router._label_for(Path("/"), None, None) == str(Path("/"))
    assert devices_router._label_for(folder, "WALKMAN", "  Gym player ") == "Gym player", "the person's own name wins"


def test_a_device_can_be_named_and_added_only_once(web, card) -> None:
    first = add_device(web, card, "My Walkman")
    again = add_device(web, card, "Something else")
    assert first["label"] == "My Walkman" and again["id"] == first["id"]
    assert len(web.client.get("/api/devices").json()["items"]) == 1
    renamed = web.client.patch(f"/api/devices/{first['id']}", json={"label": "Gym player"}).json()
    assert renamed["label"] == "Gym player"
    assert web.client.patch(f"/api/devices/{first['id']}", json={"label": "  "}).status_code == 422


def test_the_music_library_itself_is_never_a_device(web, music_dir, card) -> None:
    for folder in (music_dir, music_dir / "Aurora Vale", music_dir.parent):
        response = web.client.post("/api/devices", json={"path": str(folder)})
        assert response.status_code == 422 and "library" in response.json()["detail"], folder
    assert web.client.post("/api/devices", json={"path": str(card.parent / "nowhere")}).status_code == 422
    assert web.client.get("/api/devices").json()["items"] == []


def test_the_volume_list_flags_drives_already_added_and_the_one_holding_the_library(web, music_dir, card, monkeypatch) -> None:
    device = add_device(web, card)
    candidates = [
        device_detect.DeviceCandidate(str(card), "X:", "exFAT", 1000, 400, True),
        device_detect.DeviceCandidate(str(music_dir), "C:", "NTFS", 5000, 100, False),
    ]
    monkeypatch.setattr(device_detect, "list_candidate_devices", lambda: candidates)

    items = {i["mount_path"]: i for i in web.client.get("/api/devices/volumes").json()["items"]}

    assert items[str(card)]["device_id"] == device["id"] and items[str(card)]["holds_library"] is False
    assert items[str(music_dir)]["device_id"] is None and items[str(music_dir)]["holds_library"] is True


def test_a_drive_with_its_own_music_folder_offers_that_folder(web, tmp_path, monkeypatch) -> None:
    player = tmp_path / "player"
    (player / "MUSIC").mkdir(parents=True)
    plain = tmp_path / "stick"
    plain.mkdir()
    candidates = [
        device_detect.DeviceCandidate(str(player), "E:", "exFAT", 1000, 400, True),
        device_detect.DeviceCandidate(str(plain), "F:", "FAT32", 1000, 400, True),
    ]
    monkeypatch.setattr(device_detect, "list_candidate_devices", lambda: candidates)

    items = {i["mount_path"]: i for i in web.client.get("/api/devices/volumes").json()["items"]}

    assert items[str(player)]["music_folder"] == str(player / "MUSIC")
    assert items[str(plain)]["music_folder"] is None


def test_every_device_comes_with_the_default_layout_and_the_sync_result_names_its_device(web, card) -> None:
    default = web.ctx.config.sync.device_scheme
    device = add_device(web, card)
    assert device["default_scheme"] == default
    listing = web.client.get("/api/devices").json()
    assert listing["default_scheme"] == default and listing["items"][0]["default_scheme"] == default
    assert web.client.get(f"/api/devices/{device['id']}").json()["default_scheme"] == default

    result = finish(web, run_sync(web, device, preview(web, device, FOLK))).result

    assert result["device_id"] == device["id"] and result["label"] == device["label"] and result["copied"] == 2


def test_forgetting_a_device_leaves_its_files_alone(web, card) -> None:
    device = add_device(web, card)
    finish(web, run_sync(web, device, preview(web, device)))
    before = copied_files(card)

    assert web.client.delete(f"/api/devices/{device['id']}").json() == {"ok": True}

    assert copied_files(card) == before and before
    assert web.client.get(f"/api/devices/{device['id']}").status_code == 404
    with web.ctx.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sync_manifest").fetchone()[0] == 0


# ------------------------------------------------------------------------------------------- preview and sync


def test_a_preview_says_what_would_be_copied_without_touching_the_device(web, card) -> None:
    device = add_device(web, card)

    job = preview(web, device)

    result = job.result
    assert (result["selected"], result["to_copy"], result["unchanged"], result["to_prune"], result["skipped"]) == (9, 9, 0, 0, 0)
    assert result["reasons"] == {"new": 9} and result["bytes"] > 0 and result["enough_space"] is True
    assert copied_files(card) == []
    page = web.client.get(f"/api/devices/{device['id']}/preview/{job.id}").json()
    assert page["total"] == 9
    targets = {item["title"]: item["to"] for item in page["items"]}
    assert targets["Glacier"] == "Aurora Vale/Northern Lights/01 - Glacier.mp3"
    assert page["items"][0]["reason"] == "new"


def test_nothing_is_written_without_a_preview_of_the_same_device(web, card) -> None:
    device = add_device(web, card)
    assert web.client.post(f"/api/devices/{device['id']}/sync", json={"job_id": "nope"}).status_code == 409
    job = preview(web, device)
    assert web.client.post(f"/api/devices/{device['id']}/sync", json={"job_id": "someone-elses"}).status_code == 409
    assert run_sync(web, device, job).status_code == 200


def test_syncing_copies_the_songs_and_a_second_preview_finds_nothing_left(web, card) -> None:
    device = add_device(web, card)
    job = preview(web, device)

    result = finish(web, run_sync(web, device, job)).result

    assert (result["copied"], result["errors"], result["aborted"]) == (9, [], None)
    assert len(copied_files(card)) == 9
    assert not [f for f in copied_files(card) if f.endswith(mirror.PARTIAL_SUFFIX)]
    after = web.client.get(f"/api/devices/{device['id']}").json()
    assert after["synced"] == 9 and after["last_synced_at"] and after["synced_bytes"] > 0
    assert after["prefs"]["sources"] == ALL
    again = preview(web, device)
    assert (again.result["to_copy"], again.result["unchanged"]) == (0, 9)
    assert web.client.post(f"/api/devices/{device['id']}/sync", json={"job_id": job.id}).status_code == 409, "a used-up preview is gone"


def test_removing_songs_from_the_device_needs_the_exact_count(web, card) -> None:
    device = add_device(web, card)
    finish(web, run_sync(web, device, preview(web, device)))
    ids = ids_by_title(web.client)
    web.client.patch(f"/api/tracks/{ids['Glacier']}", json={"favorite": True})

    job = preview(web, device, [{"kind": "favorites"}])
    assert (job.result["selected"], job.result["to_copy"], job.result["to_prune"]) == (1, 0, 8)
    pruned = web.client.get(f"/api/devices/{device['id']}/preview/{job.id}", params={"kind": "prune"}).json()
    assert pruned["total"] == 8 and {"path", "title", "size"} <= set(pruned["items"][0])

    assert run_sync(web, device, job, prune=True).status_code == 422
    assert run_sync(web, device, job, prune=True, confirm_prune=3).status_code == 422
    kept_everything = finish(web, run_sync(web, device, job, prune=False)).result
    assert (kept_everything["pruned"], len(copied_files(card))) == (0, 9)

    job = preview(web, device, [{"kind": "favorites"}])
    result = finish(web, run_sync(web, device, job, prune=True, confirm_prune=8)).result
    assert result["pruned"] == 8
    assert [f for f in copied_files(card) if f.endswith(".mp3")] == ["Aurora Vale/Northern Lights/01 - Glacier.mp3"]


def test_a_sync_that_cannot_fit_is_refused_until_removals_make_room(web, card, monkeypatch) -> None:
    device = add_device(web, card)
    finish(web, run_sync(web, device, preview(web, device, FOLK)))  # the card holds the two Folk songs
    sizing = preview(web, device, ELECTRONIC).result  # real disk, plenty of room: 3 songs to add, the 2 Folk songs to drop
    assert (sizing["to_copy"], sizing["to_prune"]) == (3, 2) and sizing["prune_bytes"] > 0
    # one byte short of what the new songs need, so only dropping the old ones can make room
    short = SimpleNamespace(free=sizing["bytes"] - 1, total=10**9, used=10**9 - sizing["bytes"])
    monkeypatch.setattr(devices_router.shutil, "disk_usage", lambda p: short)

    full = preview(web, device, ELECTRONIC)

    assert full.result["enough_space"] is False and full.result["enough_space_with_removals"] is True
    refused = run_sync(web, device, full)
    assert refused.status_code == 409 and "Not enough room" in refused.json()["detail"]
    assert run_sync(web, device, full, prune=True, confirm_prune=full.result["to_prune"]).status_code == 200


def test_the_folder_layout_for_a_device_can_differ_from_the_library(web, card) -> None:
    device = add_device(web, card)
    job = preview(web, device, scheme="{artist}/{title}.{ext}")
    finish(web, run_sync(web, device, job))
    assert (card / "Aurora Vale" / "Glacier.mp3").exists()
    assert web.client.post(f"/api/devices/{device['id']}/preview", json={"sources": ALL, "scheme": "{nonsense}.{ext}"}).status_code == 422


def test_bad_sources_are_refused_before_a_job_starts(web, card) -> None:
    device = add_device(web, card)
    post = web.client.post
    assert post(f"/api/devices/{device['id']}/preview", json={"sources": []}).status_code == 422
    assert post(f"/api/devices/{device['id']}/preview", json={"sources": [{"kind": "everything"}]}).status_code == 422
    assert post(f"/api/devices/{device['id']}/preview", json={"sources": [{"kind": "rating", "min": 9}]}).status_code == 422
    assert [j for j in web.client.get("/api/jobs").json()["jobs"] if j["kind"] == "sync-preview"] == []


def test_a_playlist_is_written_to_the_device_with_relative_paths_and_removed_when_dropped(web, card) -> None:
    ids = ids_by_title(web.client)
    playlist = web.client.post("/api/playlists", json={"name": "Road trip", "track_ids": [ids["Wires"], ids["Glacier"]]}).json()
    device = add_device(web, card)
    source = [{"kind": "playlist", "id": playlist["id"]}]

    result = finish(web, run_sync(web, device, preview(web, device, source))).result

    assert (result["copied"], result["playlists_written"]) == (2, 1)
    lines = (card / "Playlists" / "Road trip.m3u8").read_text(encoding="utf-8").splitlines()
    assert lines == ["#EXTM3U", "../Mara Quinn/Static Hearts/01 - Wires.mp3", "../Aurora Vale/Northern Lights/01 - Glacier.mp3"]

    result = finish(web, run_sync(web, device, preview(web, device, ALL))).result
    assert (result["playlists_written"], result["playlists_removed"]) == (0, 1)
    assert not (card / "Playlists").exists()


def test_playlists_can_be_left_off_the_device(web, card) -> None:
    ids = ids_by_title(web.client)
    playlist = web.client.post("/api/playlists", json={"name": "Mix", "track_ids": [ids["Wires"]]}).json()
    device = add_device(web, card)
    job = preview(web, device, [{"kind": "playlist", "id": playlist["id"]}], playlists=False)
    finish(web, run_sync(web, device, job))
    assert not (card / "Playlists").exists()


def test_the_choices_are_remembered_per_device(web, card) -> None:
    device = add_device(web, card)
    saved = web.client.put(
        f"/api/devices/{device['id']}/prefs",
        json={"sources": [{"kind": "rating", "min": 4}], "scheme": "{artist}/{title}.{ext}", "playlists": False, "covers": False},
    ).json()
    assert saved["prefs"] == {"sources": [{"kind": "rating", "min": 4}], "scheme": "{artist}/{title}.{ext}", "playlists": False, "covers": False}
    assert web.client.get(f"/api/devices/{device['id']}").json()["prefs"]["covers"] is False
    assert web.client.put(f"/api/devices/{device['id']}/prefs", json={"sources": [{"kind": "x"}]}).status_code == 422


def test_covers_follow_the_songs_unless_switched_off(web, card, music_dir) -> None:
    (music_dir / "Mara Quinn" / "Static Hearts" / "cover.jpg").write_bytes(b"cover")
    device = add_device(web, card)
    finish(web, run_sync(web, device, preview(web, device, [{"kind": "genre", "value": "Indie Rock"}])))
    assert (card / "Mara Quinn" / "Static Hearts" / "cover.jpg").read_bytes() == b"cover"


# ------------------------------------------------------------------------------------------- a device that is not there


def test_a_device_that_is_unplugged_cannot_be_previewed_or_synced(web, card) -> None:
    device = add_device(web, card)
    shutil.rmtree(card)

    described = web.client.get(f"/api/devices/{device['id']}").json()
    assert described["connected"] is False and described["free"] is None
    refused = web.client.post(f"/api/devices/{device['id']}/preview", json={"sources": ALL})
    assert refused.status_code == 409 and "not connected" in refused.json()["detail"]


def test_a_different_drive_on_the_same_letter_is_never_written_to(web, card, monkeypatch) -> None:
    monkeypatch.setattr(device_detect, "volume_info", lambda path: device_detect.VolumeInfo("WALKMAN", "AAAA0001", "exFAT"))
    device = add_device(web, card)
    assert device["connected"] is True
    monkeypatch.setattr(device_detect, "volume_info", lambda path: device_detect.VolumeInfo("USB STICK", "BBBB0002", "FAT32"))

    described = web.client.get(f"/api/devices/{device['id']}").json()
    assert (described["connected"], described["different_drive"]) == (False, True)
    refused = web.client.post(f"/api/devices/{device['id']}/preview", json={"sources": ALL})
    assert refused.status_code == 409 and "different drive" in refused.json()["detail"]
    assert copied_files(card) == []


def test_a_device_whose_letter_changed_is_found_again_by_its_serial(web, card, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(device_detect, "volume_info", lambda path: device_detect.VolumeInfo("WALKMAN", "AAAA0001", "exFAT"))
    device = add_device(web, card)
    elsewhere = tmp_path / "now-here"
    elsewhere.mkdir()
    shutil.rmtree(card)
    monkeypatch.setattr(device_detect, "list_candidate_devices", lambda: [device_detect.DeviceCandidate(str(elsewhere), "F:", "exFAT", 1, 1, True)])

    described = web.client.get(f"/api/devices/{device['id']}").json()
    assert described["connected"] is False and described["moved_to"] == str(elsewhere)

    moved = web.client.patch(f"/api/devices/{device['id']}", json={"path": str(elsewhere)}).json()
    assert moved["path"] == str(elsewhere.resolve()) and moved["connected"] is True


def test_pointing_a_device_at_a_different_drive_is_refused(web, card, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(device_detect, "volume_info", lambda path: device_detect.VolumeInfo("WALKMAN", "AAAA0001", "exFAT"))
    device = add_device(web, card)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(device_detect, "volume_info", lambda path: device_detect.VolumeInfo("OTHER", "CCCC0003", "exFAT"))

    refused = web.client.patch(f"/api/devices/{device['id']}", json={"path": str(other)})

    assert refused.status_code == 409 and "different drive" in refused.json()["detail"]


# ------------------------------------------------------------------------------------------- running jobs


def test_a_second_sync_is_refused_while_one_runs_and_a_cancel_keeps_the_copies(web, card, monkeypatch) -> None:
    device = add_device(web, card)
    started, gate = threading.Event(), threading.Event()
    real = mirror._copy_file

    def slow(source, dest):
        started.set()
        gate.wait(10)
        return real(source, dest)

    monkeypatch.setattr(mirror, "_copy_file", slow)
    job = preview(web, device)
    running = run_sync(web, device, job).json()["job"]
    assert started.wait(10)
    try:
        again = preview(web, device)
        assert run_sync(web, device, again).status_code == 409
        assert web.client.post(f"/api/jobs/{running['id']}/cancel").status_code == 200
    finally:
        gate.set()
    assert web.ctx.jobs.wait(running["id"]).status == "cancelled"
    monkeypatch.setattr(mirror, "_copy_file", real)

    synced = web.client.get(f"/api/devices/{device['id']}").json()["synced"]
    assert 1 <= synced < 9
    resumed = preview(web, device)
    assert (resumed.result["to_copy"], resumed.result["unchanged"]) == (9 - synced, synced)
