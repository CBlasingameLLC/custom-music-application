"""Phones and players without a drive letter, through the web API: found, added, previewed and synced.

The helper that talks to them is the stand-in from tests/fake_mtp_helper.py, started exactly as the real one would be.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from musictoolkit.sync import mtp
from tests.conftest import ids_by_title
from tests.test_mtp import FAKE, SERIAL, control, write_device
from tests.test_web_devices import ALL, ELECTRONIC, FOLK, finish, preview, run_sync

PHONE = {"serial": SERIAL, "storage": "s1"}


@pytest.fixture
def phones(tmp_path: Path) -> Path:
    root = tmp_path / "phones"
    write_device(root)
    return root


@pytest.fixture(autouse=True)
def fake_helper(phones: Path, monkeypatch):
    monkeypatch.setenv("MTK_MTP_HELPER", json.dumps([sys.executable, str(FAKE), str(phones)]))
    mtp.forget_listing()
    yield
    mtp.forget_listing()


def on_phone(phones: Path, *parts: str) -> Path:
    return phones.joinpath(SERIAL, "s1", "Music", *parts)


def songs_on_phone(phones: Path) -> list[str]:
    root = on_phone(phones)
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) if root.exists() else []


def add_phone(web, **extra):
    response = web.client.post("/api/devices/mtp", json={**PHONE, **extra})
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------------------------------- finding and adding


def test_phones_that_are_plugged_in_are_listed_with_their_storages(web) -> None:
    listing = web.client.get("/api/devices/mtp", params={"fresh": True}).json()

    assert (listing["available"], listing["error"]) == (True, None)
    (phone,) = listing["devices"]
    assert (phone["name"], phone["serial"], phone["model"]) == ("Pixel", SERIAL, "Pixel 8")
    assert phone["storages"][0]["name"] == "Internal storage" and phone["storages"][0]["device_id"] is None

    added = add_phone(web)
    again = web.client.get("/api/devices/mtp", params={"fresh": True}).json()
    assert again["devices"][0]["storages"][0]["device_id"] == added["id"], "the dialog can say it is already added"


def test_adding_a_phone_names_it_and_remembers_where_the_music_goes(web) -> None:
    device = add_phone(web)

    assert (device["kind"], device["label"], device["connected"]) == ("mtp", "Pixel", True)
    assert device["path"] == "Internal storage / Music" and device["volume_label"] == "Google Pixel 8"
    assert (device["free"], device["total"], device["synced"]) == (10_000_000, 10_000_000, 0)
    assert device["mtp"] == {"model": "Google Pixel 8", "storage": "Internal storage", "folder": "Music", "serial": SERIAL}
    assert [d["id"] for d in web.client.get("/api/devices").json()["items"]] == [device["id"]]

    again = add_phone(web, folder="Somewhere else", label="Other name")
    assert again["id"] == device["id"] and again["mtp"]["folder"] == "Music", "what was copied is recorded relative to the folder"


def test_the_person_can_choose_the_folder_and_the_name(web, phones) -> None:
    device = add_phone(web, folder=" Music\\Gym ", label="  Gym phone ")

    assert (device["label"], device["mtp"]["folder"], device["path"]) == ("Gym phone", "Music/Gym", "Internal storage / Music/Gym")
    finish(web, run_sync(web, device, preview(web, device, FOLK)))
    assert songs_on_phone(phones) == [
        "Gym/The Fernwoods/Back Roads/01 - Gravel Dust.mp3",
        "Gym/The Fernwoods/Back Roads/02 - Porch Light.mp3",
    ]


@pytest.mark.parametrize("folder", ["../escape", "a/../b", "bad:name", 'say "no"', "what?"])
def test_a_folder_name_a_phone_cannot_use_is_refused(web, folder) -> None:
    refused = web.client.post("/api/devices/mtp", json={**PHONE, "folder": folder})

    assert refused.status_code == 422 and "cannot use" in refused.json()["detail"]
    assert web.client.get("/api/devices").json()["items"] == []


def test_a_phone_that_is_not_plugged_in_cannot_be_added(web) -> None:
    unknown = web.client.post("/api/devices/mtp", json={"serial": "NOPE", "storage": "s1"})
    assert unknown.status_code == 422 and "not connected" in unknown.json()["detail"]

    no_storage = web.client.post("/api/devices/mtp", json={"serial": SERIAL, "storage": "gone"})
    assert no_storage.status_code == 422 and "File transfer" in no_storage.json()["detail"]


def test_when_the_helper_is_missing_the_dialog_is_told_why(web, monkeypatch) -> None:
    monkeypatch.delenv("MTK_MTP_HELPER")
    monkeypatch.setattr(mtp, "helper_command", lambda: None)
    mtp.forget_listing()

    listing = web.client.get("/api/devices/mtp").json()
    assert listing["available"] is False and listing["reason"] and listing["devices"] == []
    refused = web.client.post("/api/devices/mtp", json=PHONE)
    assert refused.status_code == 422 and refused.json()["detail"] == listing["reason"]


def test_a_helper_that_fails_to_look_is_reported_not_hidden(web, monkeypatch) -> None:
    monkeypatch.setenv("MTK_MTP_HELPER", f"{sys.executable} -c pass")
    mtp.forget_listing()

    listing = web.client.get("/api/devices/mtp").json()

    assert listing["available"] is True and "did not start" in listing["error"]
    refused = web.client.post("/api/devices/mtp", json=PHONE)
    assert refused.status_code == 422 and "did not start" in refused.json()["detail"]


# ------------------------------------------------------------------------------------------- preview and sync


def test_a_first_sync_copies_to_the_phone_and_a_second_finds_nothing_left(web, phones) -> None:
    device = add_phone(web)

    job = preview(web, device)
    assert (job.result["to_copy"], job.result["free"], job.result["total"]) == (9, 10_000_000, 10_000_000)
    assert songs_on_phone(phones) == [], "previewing writes nothing"

    result = finish(web, run_sync(web, device, job)).result
    assert (result["copied"], result["errors"], result["aborted"], result["label"]) == (9, [], None, "Pixel")
    assert len(songs_on_phone(phones)) == 9
    assert "Aurora Vale/Northern Lights/01 - Glacier.mp3" in songs_on_phone(phones)
    after = web.client.get(f"/api/devices/{device['id']}").json()
    assert after["synced"] == 9 and after["last_synced_at"] and after["free"] < 10_000_000

    again = preview(web, device)
    assert (again.result["to_copy"], again.result["unchanged"]) == (0, 9)


def test_removing_songs_from_a_phone_needs_the_exact_count(web, phones) -> None:
    device = add_phone(web)
    finish(web, run_sync(web, device, preview(web, device)))
    ids = ids_by_title(web.client)
    web.client.patch(f"/api/tracks/{ids['Glacier']}", json={"favorite": True})

    job = preview(web, device, [{"kind": "favorites"}])
    assert (job.result["to_copy"], job.result["to_prune"]) == (0, 8)
    assert run_sync(web, device, job, prune=True).status_code == 422
    assert run_sync(web, device, job, prune=True, confirm_prune=3).status_code == 422

    job = preview(web, device, [{"kind": "favorites"}])
    result = finish(web, run_sync(web, device, job, prune=True, confirm_prune=8)).result
    assert result["pruned"] == 8
    assert songs_on_phone(phones) == ["Aurora Vale/Northern Lights/01 - Glacier.mp3"]


def test_a_playlist_is_written_to_the_phone_and_removed_when_dropped(web, phones) -> None:
    ids = ids_by_title(web.client)
    playlist = web.client.post("/api/playlists", json={"name": "Road trip", "track_ids": [ids["Wires"], ids["Glacier"]]}).json()
    device = add_phone(web)

    result = finish(web, run_sync(web, device, preview(web, device, [{"kind": "playlist", "id": playlist["id"]}]))).result

    assert (result["copied"], result["playlists_written"]) == (2, 1)
    lines = on_phone(phones, "Playlists", "Road trip.m3u8").read_text(encoding="utf-8").splitlines()
    assert lines == ["#EXTM3U", "../Mara Quinn/Static Hearts/01 - Wires.mp3", "../Aurora Vale/Northern Lights/01 - Glacier.mp3"]
    result = finish(web, run_sync(web, device, preview(web, device, ALL))).result
    assert (result["playlists_written"], result["playlists_removed"]) == (0, 1)
    assert not on_phone(phones, "Playlists").exists()


def test_a_phone_that_is_too_full_is_refused_before_anything_is_copied(web, phones) -> None:
    write_device(phones, "TINY", capacity=5000, name="Tiny")
    mtp.forget_listing()
    device = web.client.post("/api/devices/mtp", json={"serial": "TINY", "storage": "s1"}).json()

    job = preview(web, device, ELECTRONIC)

    assert job.result["enough_space"] is False
    refused = run_sync(web, device, job)
    assert refused.status_code == 409 and "Not enough room" in refused.json()["detail"]
    assert not list((phones / "TINY").rglob("*.mp3"))


# ------------------------------------------------------------------------------------------- a phone that is not there


def test_an_unplugged_phone_shows_as_not_connected_and_is_not_written_to(web, phones) -> None:
    device = add_phone(web)
    control(phones, unplugged=[SERIAL])
    mtp.forget_listing()

    described = web.client.get(f"/api/devices/{device['id']}").json()
    assert described["connected"] is False and described["free"] is None and "not connected" in described["note"]
    refused = web.client.post(f"/api/devices/{device['id']}/preview", json={"sources": ALL})
    assert refused.status_code == 409 and "File transfer" in refused.json()["detail"]
    assert songs_on_phone(phones) == []


def test_a_locked_phone_whose_storage_is_hidden_is_explained(web, phones) -> None:
    device = add_phone(web)
    info = json.loads((phones / SERIAL / "device.json").read_text())
    info["storages"] = []  # a locked phone shows up but offers no storage
    (phones / SERIAL / "device.json").write_text(json.dumps(info))
    mtp.forget_listing()

    described = web.client.get(f"/api/devices/{device['id']}").json()

    assert described["connected"] is False and "Unlock it and choose File transfer" in described["note"]


def test_a_phone_pulled_out_during_the_sync_stops_it_and_keeps_what_was_copied(web, phones, monkeypatch) -> None:
    device = add_phone(web)
    job = preview(web, device)
    real_put = mtp.MtpTarget.put
    calls = []

    def put_then_pull_the_cable(self, source, relative):
        size = real_put(self, source, relative)
        calls.append(relative)
        if len(calls) == 3:
            control(phones, unplugged=[SERIAL])
        return size

    monkeypatch.setattr(mtp.MtpTarget, "put", put_then_pull_the_cable)

    result = finish(web, run_sync(web, device, job)).result

    assert result["copied"] == 3 and "stopped answering" in result["aborted"]
    assert web.client.get(f"/api/devices/{device['id']}").json()["synced"] == 3
    control(phones)
    mtp.forget_listing()
    resumed = preview(web, device)
    assert (resumed.result["to_copy"], resumed.result["unchanged"]) == (6, 3), "the next sync picks up where this one stopped"


def test_a_phone_that_goes_away_between_preview_and_sync_is_refused(web, phones) -> None:
    device = add_phone(web)
    job = preview(web, device)
    control(phones, unplugged=[SERIAL])
    mtp.forget_listing()

    refused = run_sync(web, device, job)

    assert refused.status_code == 409
    assert songs_on_phone(phones) == []


# ------------------------------------------------------------------------------------------- managing it


def test_a_phone_has_no_folder_to_point_at_but_can_be_renamed_and_forgotten(web, phones, tmp_path) -> None:
    device = add_phone(web)
    finish(web, run_sync(web, device, preview(web, device, FOLK)))

    assert web.client.patch(f"/api/devices/{device['id']}", json={"label": "Gym phone"}).json()["label"] == "Gym phone"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    refused = web.client.patch(f"/api/devices/{device['id']}", json={"path": str(elsewhere)})
    assert refused.status_code == 422 and "drive letter" in refused.json()["detail"]

    assert web.client.delete(f"/api/devices/{device['id']}").json() == {"ok": True}
    assert web.client.get("/api/devices").json()["items"] == []
    assert len(songs_on_phone(phones)) == 2, "forgetting a phone never touches its files"


def test_drive_letter_devices_are_unaffected_by_phones(web, phones, tmp_path) -> None:
    card = tmp_path / "walkman" / "MUSIC"
    card.mkdir(parents=True)
    phone = add_phone(web)
    drive = web.client.post("/api/devices", json={"path": str(card)}).json()

    items = {d["id"]: d for d in web.client.get("/api/devices").json()["items"]}

    assert items[drive["id"]]["kind"] == "folder" and items[phone["id"]]["kind"] == "mtp"
    shutil.rmtree(card)
    assert web.client.get(f"/api/devices/{drive['id']}").json()["connected"] is False
    assert web.client.get(f"/api/devices/{phone['id']}").json()["connected"] is True


def test_a_phone_that_does_not_report_its_room_can_still_be_synced(web, phones) -> None:
    device = add_phone(web)
    control(phones, free_unknown=True)
    mtp.forget_listing()

    described = web.client.get(f"/api/devices/{device['id']}").json()
    assert described["connected"] is True and described["free"] is None
    job = preview(web, device, FOLK)
    assert job.result["enough_space"] is True
    assert finish(web, run_sync(web, device, job)).result["copied"] == 2


def test_a_phone_that_is_found_but_will_not_open_is_listed_with_the_reason(web, phones) -> None:
    write_device(phones, "LOCKED", name="Locked phone")
    info = json.loads((phones / "LOCKED" / "device.json").read_text())
    info["error"] = "The device is locked"
    (phones / "LOCKED" / "device.json").write_text(json.dumps(info))

    listing = web.client.get("/api/devices/mtp", params={"fresh": True}).json()

    locked = next(d for d in listing["devices"] if d["serial"] == "LOCKED")
    assert (locked["storages"], locked["error"]) == ([], "The device is locked")
    assert [d["serial"] for d in listing["devices"] if d["storages"]] == [SERIAL]
