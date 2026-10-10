import json
import shutil
from pathlib import Path

import mutagen
import pytest

from tests.conftest import FIXTURES_DIR, build_web, ids_by_title, scan


def run_job(web, response) -> dict:
    assert response.status_code == 200, response.text
    job = web.ctx.jobs.wait(response.json()["job"]["id"], timeout=30)
    assert job.status == "done", job.error
    return job.result


def file_tags(path: Path) -> dict:
    tags = mutagen.File(path, easy=True).tags or {}
    return {key: tags[key][0] for key in tags.keys()}


def path_of(web, track_id: int) -> Path:
    with web.ctx.db() as conn:
        return Path(conn.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,)).fetchone()["file_path"])


def row(web, track_id: int):
    with web.ctx.db() as conn:
        return conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()


class TestRead:
    def test_one_song_shows_its_values_and_file(self, web) -> None:
        ids = ids_by_title(web.client)
        data = web.client.post("/api/tags/read", json={"ids": [ids["Glacier"]]}).json()
        assert data["count"] == 1
        assert data["fields"]["title"] == {"value": "Glacier", "mixed": False}
        assert data["fields"]["genre"]["value"] == "Electronic"
        assert data["fields"]["year"]["value"] == 2019
        assert data["path"].endswith("01 - Glacier.mp3")
        assert data["unwritable"] == []

    def test_several_songs_mark_differing_fields_as_mixed(self, web) -> None:
        ids = ids_by_title(web.client)
        data = web.client.post("/api/tags/read", json={"ids": [ids["Glacier"], ids["Polar Night"], ids["Salt and Stone"]]}).json()
        assert data["fields"]["artist"] == {"value": "Aurora Vale", "mixed": False}
        assert data["fields"]["album"]["mixed"] is True and data["fields"]["album"]["value"] is None
        assert data["fields"]["genre"]["mixed"] is True
        assert data["path"] is None

    def test_unknown_songs_are_a_404(self, web) -> None:
        assert web.client.post("/api/tags/read", json={"ids": [99999]}).status_code == 404
        assert web.client.post("/api/tags/read", json={"ids": []}).status_code == 422


class TestEdit:
    def test_edit_writes_files_and_library(self, web) -> None:
        ids = ids_by_title(web.client)
        chosen = [ids["Glacier"], ids["Polar Night"], ids["Drift"]]
        result = run_job(web, web.client.post("/api/tags/edit", json={"ids": chosen, "changes": {"genre": "Ambient", "year": 2020}}))
        assert result["edited"] == 3 and result["unchanged"] == 0 and result["errors"] == []
        for track_id in chosen:
            tags = file_tags(path_of(web, track_id))
            assert tags["genre"] == "Ambient" and tags["date"] == "2020"
            r = row(web, track_id)
            assert (r["genre"], r["year"], r["tag_source"]) == ("Ambient", 2020, "manual")
        assert row(web, ids["Wires"])["genre"] == "Indie Rock"  # others untouched

    def test_the_library_view_reflects_the_edit(self, web) -> None:
        ids = ids_by_title(web.client)
        run_job(web, web.client.post("/api/tags/edit", json={"ids": [ids["Wires"]], "changes": {"title": "Cables", "artist": "Someone Else"}}))
        found = web.client.get("/api/tracks", params={"q": "cables"}).json()["items"]
        assert [(t["title"], t["artist"]) for t in found] == [("Cables", "Someone Else")]

    def test_an_edit_that_changes_nothing_leaves_files_alone(self, web) -> None:
        ids = ids_by_title(web.client)
        path = path_of(web, ids["Glacier"])
        stamp = path.stat().st_mtime_ns
        result = run_job(web, web.client.post("/api/tags/edit", json={"ids": [ids["Glacier"]], "changes": {"genre": "Electronic"}}))
        assert (result["edited"], result["unchanged"]) == (0, 1)
        assert path.stat().st_mtime_ns == stamp
        assert web.client.get("/api/tags/batches").json()["items"] == []

    def test_clearing_a_field(self, web) -> None:
        ids = ids_by_title(web.client)
        run_job(web, web.client.post("/api/tags/edit", json={"ids": [ids["Glacier"]], "changes": {"genre": ""}}))
        assert "genre" not in file_tags(path_of(web, ids["Glacier"]))
        assert row(web, ids["Glacier"])["genre"] is None

    def test_a_rescan_sees_edited_files_as_unchanged(self, web) -> None:
        ids = ids_by_title(web.client)
        run_job(web, web.client.post("/api/tags/edit", json={"ids": [ids["Glacier"], ids["Drift"]], "changes": {"album": "Renamed"}}))
        result = scan(web)
        assert result["updated"] == 0 and result["unchanged"] == 9

    def test_one_bad_file_does_not_stop_the_rest(self, web) -> None:
        ids = ids_by_title(web.client)
        path_of(web, ids["Drift"]).unlink()
        result = run_job(web, web.client.post("/api/tags/edit", json={"ids": [ids["Glacier"], ids["Drift"], ids["Wires"]], "changes": {"genre": "Folk"}}))
        assert result["edited"] == 2
        assert len(result["errors"]) == 1 and result["errors"][0]["track_id"] == ids["Drift"]
        assert "missing" in result["errors"][0]["error"]

    @pytest.mark.parametrize(
        "changes",
        [{}, {"rating": 5}, {"year": "abc"}, {"year": 0}, {"title": "x" * 600}, {"track_number": -3}],
    )
    def test_bad_requests_are_refused(self, web, changes) -> None:
        ids = ids_by_title(web.client)
        response = web.client.post("/api/tags/edit", json={"ids": [ids["Glacier"]], "changes": changes})
        assert response.status_code == 422
        assert response.json()["detail"]

    def test_progress_and_job_title(self, web) -> None:
        ids = ids_by_title(web.client)
        response = web.client.post("/api/tags/edit", json={"ids": list(ids.values()), "changes": {"genre": "Rock"}})
        job = web.ctx.jobs.wait(response.json()["job"]["id"])
        assert job.kind == "tags" and job.title == "Saving tags for 9 songs" and job.done == job.total == 9


class TestUndo:
    def test_undo_restores_files_and_library(self, web) -> None:
        ids = ids_by_title(web.client)
        chosen = [ids["Glacier"], ids["Wires"]]
        before = {i: file_tags(path_of(web, i)) for i in chosen}
        edit = run_job(web, web.client.post("/api/tags/edit", json={"ids": chosen, "changes": {"genre": "Metal", "title": "X"}}))

        batches = web.client.get("/api/tags/batches").json()["items"]
        assert [(b["batch_id"], b["tracks"], b["fields"]) for b in batches] == [(edit["batch_id"], 2, ["genre", "title"])]

        undone = run_job(web, web.client.post("/api/tags/undo", json={"batch_id": edit["batch_id"]}))
        assert undone["edited"] == 2 and undone["errors"] == []
        for track_id in chosen:
            assert file_tags(path_of(web, track_id)) == before[track_id]
        assert row(web, ids["Wires"])["title"] == "Wires" and row(web, ids["Wires"])["genre"] == "Indie Rock"
        assert web.client.get("/api/tags/batches").json()["items"] == []

    def test_undo_follows_a_file_that_was_moved(self, web) -> None:
        ids = ids_by_title(web.client)
        original = path_of(web, ids["Glacier"])
        edit = run_job(web, web.client.post("/api/tags/edit", json={"ids": [ids["Glacier"]], "changes": {"genre": "Metal"}}))
        moved = original.with_name("renamed.mp3")
        original.rename(moved)
        with web.ctx.db() as conn:
            conn.execute("UPDATE tracks SET file_path = ? WHERE id = ?", (str(moved), ids["Glacier"]))
            conn.commit()
        run_job(web, web.client.post("/api/tags/undo", json={"batch_id": edit["batch_id"]}))
        assert file_tags(moved)["genre"] == "Electronic"

    def test_an_unknown_batch_is_a_404(self, web) -> None:
        assert web.client.post("/api/tags/undo", json={"batch_id": "nope"}).status_code == 404

    def test_only_the_newest_batches_are_kept(self, web, monkeypatch) -> None:
        from musictoolkit.ingest import tagedit

        monkeypatch.setattr(tagedit, "KEEP_BATCHES", 3)
        ids = ids_by_title(web.client)
        for n in range(5):
            run_job(web, web.client.post("/api/tags/edit", json={"ids": [ids["Glacier"]], "changes": {"album": f"Take {n}"}}))
        assert len(web.client.get("/api/tags/batches").json()["items"]) == 3


class TestOtherFormats:
    @pytest.fixture
    def mixed(self, tmp_path: Path):
        root = tmp_path / "music"
        root.mkdir()
        for name in ("tiny.flac", "tiny.m4a", "tiny.ogg", "tiny.opus", "complete_tags.mp3"):
            shutil.copy(FIXTURES_DIR / name, root / name)
        (root / "unsupported.wav").write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 30)
        web = build_web(tmp_path, root)
        scan(web)
        return web

    def test_edits_work_in_every_supported_format(self, mixed) -> None:
        listing = mixed.client.get("/api/tracks", params={"limit": 50}).json()["items"]
        writable = [t["id"] for t in listing if t["format"] != "wav"]
        assert len(writable) == 5
        result = run_job(mixed, mixed.client.post("/api/tags/edit", json={"ids": writable, "changes": {"album": "One Album", "genre": "Test"}}))
        assert result["edited"] == 5 and result["errors"] == []
        for track_id in writable:
            tags = file_tags(path_of(mixed, track_id))
            assert (tags["album"], tags["genre"]) == ("One Album", "Test")

    def test_unsupported_formats_are_reported_not_corrupted(self, mixed) -> None:
        wav = next(t for t in mixed.client.get("/api/tracks", params={"limit": 50}).json()["items"] if t["format"] == "wav")
        data = mixed.client.post("/api/tags/read", json={"ids": [wav["id"]]}).json()
        assert [u["format"] for u in data["unwritable"]] == ["wav"]
        before = path_of(mixed, wav["id"]).read_bytes()
        result = run_job(mixed, mixed.client.post("/api/tags/edit", json={"ids": [wav["id"]], "changes": {"genre": "x"}}))
        assert result["edited"] == 0 and "can't be edited" in result["errors"][0]["error"]
        assert path_of(mixed, wav["id"]).read_bytes() == before


def test_the_undo_log_stores_raw_values(web) -> None:
    ids = ids_by_title(web.client)
    run_job(web, web.client.post("/api/tags/edit", json={"ids": [ids["Glacier"]], "changes": {"track_number": 9}}))
    with web.ctx.db() as conn:
        edit = conn.execute("SELECT before_json, after_json FROM tag_edits").fetchone()
    assert json.loads(edit["before_json"]) == {"tracknumber": "1"} and json.loads(edit["after_json"]) == {"tracknumber": "9"}
