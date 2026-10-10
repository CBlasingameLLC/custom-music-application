"""Importing a Spotify export from the app: read it first, add it, undo it, and send it on to ListenBrainz."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from musictoolkit.history import spotify_import
from musictoolkit.integrations.listenbrainz_client import ListenBrainzError
from musictoolkit.web.routers import history as history_router
from tests.test_spotify_import import SAMPLE_RECORDS, _write_export_folder, _write_export_zip


def finish(web, response, expect: str = "done"):
    assert response.status_code == 200, response.text
    job = web.ctx.jobs.wait(response.json()["job"]["id"], timeout=30)
    assert job.status == expect, job.error
    return job


def read(web, path: Path, expect: str = "done"):
    return finish(web, web.client.post("/api/history/import/preview", json={"path": str(path)}), expect)


def add(web, preview):
    return finish(web, web.client.post("/api/history/import/apply", json={"job_id": preview.id}))


def plays(web) -> int:
    with web.ctx.db() as conn:
        return conn.execute("SELECT COUNT(*) FROM play_history").fetchone()[0]


@pytest.fixture
def export_zip(tmp_path: Path) -> Path:
    return _write_export_zip(tmp_path, SAMPLE_RECORDS)


def test_an_export_is_read_first_then_added_then_listed_and_undone(empty_web, export_zip) -> None:
    web = empty_web
    summary = read(web, export_zip).result

    assert (summary["music_rows"], summary["podcast_rows_skipped"], summary["total_rows_seen"]) == (2, 1, 3)
    assert summary["top_artists"] == [{"name": "Artist A", "plays": 2}]
    assert summary["date_range"][0].startswith("2024-01-15") and summary["date_range"][1].startswith("2024-01-16")
    assert plays(web) == 0, "reading an export adds nothing"

    result = add(web, read(web, export_zip)).result
    assert (result["inserted"], result["duplicates_skipped"]) == (2, 0) and result["batch_id"]
    assert plays(web) == 2
    assert not list((web.ctx.data_dir / "tmp").iterdir()), "the unpacked ZIP is cleaned up"

    listing = web.client.get("/api/history/imports").json()
    (batch,) = listing["items"]
    assert (batch["plays"], batch["sent"], batch["batch_id"]) == (2, 0, result["batch_id"])
    assert batch["imported_at"].startswith("20") and listing["unsent"] == 2 and listing["can_send"] is False

    undone = web.client.delete(f"/api/history/imports/{result['batch_id']}").json()
    assert undone == {"removed": 2, "already_sent": 0} and plays(web) == 0
    assert web.client.get("/api/history/imports").json()["items"] == []
    assert web.client.delete(f"/api/history/imports/{result['batch_id']}").status_code == 404


def test_a_folder_works_and_importing_twice_adds_nothing_twice(empty_web, tmp_path) -> None:
    folder = _write_export_folder(tmp_path, SAMPLE_RECORDS)
    assert add(empty_web, read(empty_web, folder)).result["inserted"] == 2

    again = add(empty_web, read(empty_web, folder)).result

    assert (again["inserted"], again["duplicates_skipped"]) == (0, 2) and plays(empty_web) == 2


def test_adding_needs_a_fresh_look_at_the_export(empty_web, tmp_path, export_zip) -> None:
    web = empty_web
    assert web.client.post("/api/history/import/apply", json={"job_id": "nope"}).status_code == 409

    preview = read(web, export_zip)
    assert web.client.post("/api/history/import/apply", json={"job_id": "someone-elses"}).status_code == 409

    with zipfile.ZipFile(export_zip, "a") as archive:  # the file changes after it was looked at
        archive.writestr("Spotify Extended Streaming History/Streaming_History_Audio_2024_1.json", json.dumps(SAMPLE_RECORDS[:1]))
    stale = web.client.post("/api/history/import/apply", json={"job_id": preview.id})
    assert stale.status_code == 409 and "changed" in stale.json()["detail"]
    assert plays(web) == 0


def test_a_wrong_choice_is_refused_or_explained(empty_web, tmp_path) -> None:
    web = empty_web
    assert web.client.post("/api/history/import/preview", json={"path": str(tmp_path / "nowhere.zip")}).status_code == 422
    notes = tmp_path / "notes.txt"
    notes.write_text("hello")
    assert "ZIP file Spotify sent" in web.client.post("/api/history/import/preview", json={"path": str(notes)}).json()["detail"]

    account_data = tmp_path / "account.zip"  # "Account data", not the extended streaming history
    with zipfile.ZipFile(account_data, "w") as archive:
        archive.writestr("Userdata.json", "{}")
    job = read(web, account_data, expect="error")
    assert "Extended streaming history" in job.error

    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"this is not a zip file")
    assert "not a valid ZIP" in read(web, broken, expect="error").error


def test_something_far_too_big_to_be_an_export_is_refused(empty_web, export_zip, monkeypatch) -> None:
    monkeypatch.setattr(spotify_import, "MAX_EXPORT_BYTES", 100)
    assert "far bigger" in read(empty_web, export_zip, expect="error").error


def test_stopping_an_import_part_way_adds_nothing(tmp_path, monkeypatch) -> None:
    from musictoolkit.db.connection import connect

    monkeypatch.setattr(spotify_import, "PROGRESS_EVERY", 1)
    folder = _write_export_folder(tmp_path, SAMPLE_RECORDS)
    conn = connect(tmp_path / "test.db")

    def stop(done: int, total: int) -> None:
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError):
        spotify_import.import_history(conn, folder, tmp_path / "work", on_progress=stop)
    conn.close()

    check = connect(tmp_path / "test.db")
    assert check.execute("SELECT COUNT(*) FROM play_history").fetchone()[0] == 0
    check.close()


def test_batches_are_listed_newest_first(empty_web, tmp_path) -> None:
    web = empty_web
    with web.ctx.db() as conn:
        spotify_import.import_history(conn, _write_export_folder(tmp_path, SAMPLE_RECORDS[:1]), tmp_path / "w1", import_batch_id="20240101T000000Z")
    (tmp_path / "export" / "Streaming_History_Audio_2024_0.json").write_text(json.dumps(SAMPLE_RECORDS[1:2]))
    with web.ctx.db() as conn:
        spotify_import.import_history(conn, tmp_path / "export", tmp_path / "w2", import_batch_id="20240202T000000Z")

    items = web.client.get("/api/history/imports").json()["items"]

    assert [i["batch_id"] for i in items] == ["20240202T000000Z", "20240101T000000Z"]
    assert items[1]["imported_at"] == "2024-01-01T00:00:00+00:00"


# ------------------------------------------------------------------------------------------- sending it to ListenBrainz


@pytest.fixture
def imported(empty_web, export_zip):
    add(empty_web, read(empty_web, export_zip))
    return empty_web


def backfill(web, expect: str = "done"):
    return finish(web, web.client.post("/api/history/listenbrainz/backfill"), expect)


def test_sending_needs_a_token_and_a_switched_on_service(imported) -> None:
    refused = imported.client.post("/api/history/listenbrainz/backfill")
    assert refused.status_code == 409 and "token in Settings" in refused.json()["detail"]
    imported.ctx.config.listenbrainz.user_token = "tok"
    imported.ctx.config.listenbrainz.enabled = False
    assert imported.client.post("/api/history/listenbrainz/backfill").status_code == 409


def test_imported_plays_go_to_listenbrainz_and_are_remembered_as_sent(imported, monkeypatch) -> None:
    imported.ctx.config.listenbrainz.user_token = "tok"
    sent: list[list[dict]] = []
    monkeypatch.setattr(spotify_import.listenbrainz_client, "submit_listens", lambda token, listens, listen_type="import": sent.append(listens))
    assert imported.client.get("/api/history/imports").json()["can_send"] is True

    result = backfill(imported).result

    assert result == {"sent": 2, "refused": 0, "remaining": 0}
    assert [l["track_metadata"]["track_name"] for l in sent[0]] == ["Song One", "Song Two"]
    listing = imported.client.get("/api/history/imports").json()
    assert listing["unsent"] == 0 and listing["items"][0]["sent"] == 2
    assert backfill(imported).result["sent"] == 0, "nothing is sent twice"
    removal = imported.client.delete(f"/api/history/imports/{listing['items'][0]['batch_id']}").json()
    assert removal["already_sent"] == 2, "the screen can warn that these are on ListenBrainz already"


def test_a_rejected_token_is_explained(imported, monkeypatch) -> None:
    imported.ctx.config.listenbrainz.user_token = "wrong"

    def refuse(token, listens, listen_type="import"):
        raise ListenBrainzError("unauthorized", status=401)

    monkeypatch.setattr(spotify_import.listenbrainz_client, "submit_listens", refuse)

    job = backfill(imported, expect="error")

    assert "does not accept this token" in job.error
    assert imported.client.get("/api/history/imports").json()["unsent"] == 2, "nothing is lost; it can be tried again"


def test_a_busy_service_is_waited_for_and_one_unacceptable_play_is_skipped(imported, monkeypatch) -> None:
    imported.ctx.config.listenbrainz.user_token = "tok"
    waits: list[float] = []
    monkeypatch.setattr(history_router, "_pause", lambda handle, seconds: waits.append(seconds))
    answers = [ListenBrainzError("busy", status=503), ListenBrainzError("slow down", status=429, retry_after=30)]
    sent: list[str] = []

    def flaky(token, listens, listen_type="import"):
        if answers:
            raise answers.pop(0)
        if any(l["track_metadata"]["track_name"] == "Song Two" for l in listens):
            raise ListenBrainzError("invalid listen", status=400)
        sent.extend(l["track_metadata"]["track_name"] for l in listens)

    monkeypatch.setattr(spotify_import.listenbrainz_client, "submit_listens", flaky)

    result = backfill(imported).result

    assert result == {"sent": 1, "refused": 1, "remaining": 0} and sent == ["Song One"]
    assert waits == [5, 30], "a short wait after the first trouble, and as long as the service asked for the second time"
