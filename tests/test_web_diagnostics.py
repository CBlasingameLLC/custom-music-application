"""The diagnostics report: enough to work out what is wrong, and never a secret."""

from __future__ import annotations

import json
import sys

import pytest

from musictoolkit.sync import device_detect, mtp
from tests.test_mtp import FAKE, SERIAL, control, write_device

SECRETS = ("tok-SECRET-1", "key-SECRET-2", "secret-SECRET-3", "contact-SECRET@example.com")


def test_the_report_describes_the_installation_without_any_secret(web) -> None:
    cfg = web.ctx.config
    cfg.listenbrainz.user_token, cfg.listenbrainz.username = SECRETS[0], "cayl"
    cfg.lastfm.api_key, cfg.lastfm.api_secret = SECRETS[1], SECRETS[2]
    cfg.musicbrainz.contact = SECRETS[3]

    report = web.client.get("/api/system/diagnostics").json()

    assert report["app"]["version"] and report["app"]["python"] and report["app"]["platform"]
    assert report["paths"]["database"].endswith("library.db") and report["paths"]["home"]
    assert report["database"]["songs"] == 9 and report["database"]["missing"] == 0 and report["database"]["schema_version"] >= 6
    assert report["database"]["size"] > 0
    assert report["services"]["listenbrainz"]["has_token"] is True and report["services"]["lastfm"]["has_key"] is True
    assert report["services"]["musicbrainz"]["has_contact"] is True
    assert report["services"]["listenbrainz"]["scrobbler"]["state"] in ("off", "idle", "sending", "waiting", "rejected")
    text = str(report)
    assert not any(secret in text for secret in SECRETS)


def test_library_folders_say_whether_they_can_be_reached(web, music_dir, tmp_path) -> None:
    web.ctx.config.library.roots.append(str(tmp_path / "unplugged"))

    folders = {f["path"]: f for f in web.client.get("/api/system/diagnostics").json()["library"]}

    assert folders[str(music_dir)]["available"] is True and folders[str(music_dir)]["free"] > 0
    assert folders[str(tmp_path / "unplugged")] == {"path": str(tmp_path / "unplugged"), "available": False, "free": None, "total": None}


def test_drives_and_devices_are_listed(web, tmp_path, monkeypatch) -> None:
    card = tmp_path / "card"
    card.mkdir()
    monkeypatch.setattr(device_detect, "list_candidate_devices", lambda: [device_detect.DeviceCandidate(str(card), "E:", "exFAT", 1000, 400, True)])
    web.client.post("/api/devices", json={"path": str(card)})

    report = web.client.get("/api/system/diagnostics").json()

    assert report["drives"] == [{"mount_path": str(card), "fs": "exFAT", "total": 1000, "free": 400, "removable": True}]
    (device,) = report["devices"]
    assert device["connected"] is True and device["synced"] == 0 and device["path"] == str(card)


def test_failed_jobs_are_reported_and_one_broken_check_does_not_spoil_the_rest(web, monkeypatch) -> None:
    def explode(handle):
        raise RuntimeError("the disk went away")

    web.ctx.jobs.wait(web.ctx.jobs.submit("test", "A job that fails", explode).id, timeout=10)

    def broken():
        raise OSError("drive enumeration failed")

    monkeypatch.setattr(device_detect, "list_candidate_devices", broken)

    report = web.client.get("/api/system/diagnostics").json()

    failure = report["jobs"]["recent_failures"][0]
    assert failure["title"] == "A job that fails" and "the disk went away" in failure["error"] and failure["finished_at"]
    assert report["drives"] == {"error": "OSError: drive enumeration failed"}
    assert report["database"]["songs"] == 9, "the other parts are still there"


@pytest.fixture
def phone(tmp_path, monkeypatch):
    root = tmp_path / "phones"
    write_device(root)
    monkeypatch.setenv("MTK_MTP_HELPER", json.dumps([sys.executable, str(FAKE), str(root)]))
    mtp.forget_listing()
    yield root
    mtp.forget_listing()


def test_a_phone_is_connected_when_it_is_plugged_in_and_never_judged_by_a_path(web, phone) -> None:
    web.client.post("/api/devices/mtp", json={"serial": SERIAL, "storage": "s1"})

    (found,) = web.client.get("/api/system/diagnostics").json()["devices"]
    assert (found["kind"], found["path"], found["connected"], found["label"]) == ("mtp", None, True, "Pixel")

    control(phone, unplugged=[SERIAL])
    mtp.forget_listing()
    (gone,) = web.client.get("/api/system/diagnostics").json()["devices"]
    assert gone["connected"] is False


def test_the_report_says_whether_phones_can_be_reached_at_all(web, phone, monkeypatch) -> None:
    ready = web.client.get("/api/system/diagnostics").json()["phones"]
    assert ready == {"supported": sys.platform == "win32", "available": True, "reason": None}

    monkeypatch.delenv("MTK_MTP_HELPER")
    monkeypatch.setattr(mtp, "helper_command", lambda: None)
    missing = web.client.get("/api/system/diagnostics").json()["phones"]
    assert missing["available"] is False and missing["reason"]
