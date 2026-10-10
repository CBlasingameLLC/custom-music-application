"""The Organize API: preview first, apply as one batch, undo, and a library that still matches the disk."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from musictoolkit.ingest import moves
from tests.conftest import build_web, jpeg_bytes, make_track, scan

SCHEME = "{album_artist}/{album}/{track:02d} - {title}.{ext}"


@pytest.fixture
def messy(tmp_path: Path):
    """Four songs dumped in one folder with meaningless names, plus a lyrics file and a cover."""
    root = tmp_path / "music"
    raw = root / "Downloads"
    make_track(raw, "a.mp3", title="Glacier", artist="Aurora Vale", albumartist="Aurora Vale", album="Northern Lights", tracknumber=1)
    make_track(raw, "b.mp3", title="Drift", artist="Aurora Vale", albumartist="Aurora Vale", album="Northern Lights", tracknumber=2)
    make_track(raw, "c.mp3", title="Wires", artist="Mara Quinn", albumartist="Mara Quinn", album="Static Hearts", tracknumber=1)
    make_track(raw, "mystery.mp3")
    (raw / "a.lrc").write_text("[00:01.00]hello")
    (raw / "cover.jpg").write_bytes(jpeg_bytes())
    web = build_web(tmp_path, root)
    scan(web)
    web.root = web.config.library.roots[0]
    return web


def finish(web, response, expect: str = "done"):
    assert response.status_code == 200, response.text
    job = web.ctx.jobs.wait(response.json()["job"]["id"], timeout=30)
    assert job.status == expect, job.error
    return job


def preview(web, scheme: str = SCHEME, root: str | None = None):
    return finish(web, web.client.post("/api/tools/organize/preview", json={"root": root or web.root, "scheme": scheme}))


def apply(web, scheme: str = SCHEME, **extra):
    return web.client.post("/api/tools/organize/apply", json={"root": web.root, "scheme": scheme, **extra})


def paths(web) -> list[str]:
    with web.ctx.db() as conn:
        return sorted(r["file_path"] for r in conn.execute("SELECT file_path FROM tracks WHERE is_missing = 0"))


def test_info_describes_the_folders_and_the_layout_choices(messy) -> None:
    info = messy.client.get("/api/tools/organize/info").json()
    assert info["roots"] == [{"path": messy.root, "songs": 4, "available": True}]
    assert info["scheme"] == SCHEME and info["default_scheme"] == SCHEME
    assert info["batches"] == []
    assert all(p["scheme"].endswith("{ext}") for p in info["presets"])
    assert "track:02d" in [f["name"] for f in info["fields"]]


def test_the_live_example_shows_real_looking_paths_and_explains_mistakes(messy) -> None:
    ok = messy.client.get("/api/tools/organize/example", params={"scheme": SCHEME}).json()
    assert ok == {"ok": True, "examples": ["Aurora Vale/Northern Lights/03 - Glacier.flac", "Some_ Band_/Unknown Album/raw-file.mp3"]}
    bad = messy.client.get("/api/tools/organize/example", params={"scheme": "{nonsense}/{title}.{ext}"}).json()
    assert bad["ok"] is False and "Available fields" in bad["error"]
    assert messy.client.get("/api/tools/organize/example", params={"scheme": "../{title}.{ext}"}).json()["ok"] is False


def test_a_preview_counts_what_would_happen_without_touching_anything(messy) -> None:
    before = paths(messy)
    job = preview(messy)
    assert job.result == {
        "moves": 4, "unchanged": 0, "collisions": 0, "too_long": 0, "unknown": 1, "root": messy.root, "scheme": SCHEME,
    }
    assert paths(messy) == before and (Path(messy.root) / "Downloads" / "a.mp3").exists()


def test_the_preview_lists_each_change_relative_to_the_folder(messy) -> None:
    job = preview(messy)
    page = messy.client.get(f"/api/tools/organize/preview/{job.id}").json()
    assert page["total"] == 4
    moves_by_source = {item["from"]: item["to"] for item in page["items"]}
    assert moves_by_source["Downloads/a.mp3"] == "Aurora Vale/Northern Lights/01 - Glacier.mp3"
    assert moves_by_source["Downloads/mystery.mp3"] == "Unknown Artist/Unknown Album/mystery.mp3"
    second = messy.client.get(f"/api/tools/organize/preview/{job.id}", params={"offset": 3, "limit": 10}).json()
    assert len(second["items"]) == 1


def test_a_preview_runs_while_a_scan_or_other_writer_is_busy(messy) -> None:
    gate = threading.Event()
    blocker = messy.ctx.jobs.submit("scan", "Pretend scan", lambda handle: gate.wait(10) and None)
    try:
        job = preview(messy)
        assert job.status == "done" and messy.ctx.jobs.get(blocker.id).status == "running"
    finally:
        gate.set()
        messy.ctx.jobs.wait(blocker.id)


def test_bad_input_is_refused_before_any_job_starts(messy) -> None:
    post = messy.client.post
    assert post("/api/tools/organize/preview", json={"root": messy.root, "scheme": "{nonsense}.{ext}"}).status_code == 422
    assert post("/api/tools/organize/preview", json={"root": messy.root, "scheme": "/abs/{title}.{ext}"}).status_code == 422
    assert post("/api/tools/organize/preview", json={"root": messy.root, "scheme": "{title}"}).status_code == 422
    assert post("/api/tools/organize/preview", json={"root": str(Path(messy.root).parent), "scheme": SCHEME}).status_code == 404
    assert messy.client.get("/api/tools/organize/preview/nope").status_code == 404
    assert [j for j in messy.client.get("/api/jobs").json()["jobs"] if j["kind"] == "organize-preview"] == []


def test_nothing_moves_without_a_matching_preview(messy) -> None:
    assert apply(messy).status_code == 409
    preview(messy)
    assert apply(messy, "{artist}/{title}.{ext}").status_code == 409  # a different layout than was previewed
    assert (Path(messy.root) / "Downloads" / "a.mp3").exists()


def test_applying_moves_the_files_and_their_lyrics_and_cover(messy) -> None:
    preview(messy)
    job = finish(messy, apply(messy))
    result = job.result
    assert (result["moved"], result["skipped"], result["errors"]) == (4, 0, [])
    root = Path(messy.root)
    assert (root / "Aurora Vale" / "Northern Lights" / "01 - Glacier.mp3").exists()
    assert (root / "Aurora Vale" / "Northern Lights" / "01 - Glacier.lrc").read_text() == "[00:01.00]hello"
    assert (root / "Aurora Vale" / "Northern Lights" / "cover.jpg").exists()
    assert (root / "Mara Quinn" / "Static Hearts" / "cover.jpg").exists()
    assert not (root / "Downloads").exists() and result["tidied_folders"] == 1
    assert len(paths(messy)) == 4 and all(Path(p).exists() for p in paths(messy))


def test_a_scan_after_organizing_finds_the_same_library(messy) -> None:
    preview(messy)
    finish(messy, apply(messy))
    again = scan(messy)
    assert (again["added"], again["missing"], again["unchanged"]) == (0, 0, 4)


def test_applying_uses_up_the_preview_and_remembers_the_layout(messy) -> None:
    scheme = "{album_artist}/{year} - {album}/{track:02d} {title}.{ext}"
    preview(messy, scheme)
    finish(messy, apply(messy, scheme))
    assert apply(messy, scheme).status_code == 409
    assert messy.config.library.canonical_scheme == scheme
    assert messy.client.get("/api/settings").json()["library"]["canonical_scheme"] == scheme


def test_the_layout_is_only_remembered_when_asked(messy) -> None:
    scheme = "{artist}/{title}.{ext}"
    preview(messy, scheme)
    finish(messy, apply(messy, scheme, remember=False))
    assert messy.config.library.canonical_scheme == SCHEME


def test_a_batch_can_be_undone_and_only_once(messy) -> None:
    original = paths(messy)
    preview(messy)
    batch = finish(messy, apply(messy)).result["batch_id"]
    listed = messy.client.get("/api/tools/organize/batches").json()["batches"]
    assert [(b["batch_id"], b["files"], b["undoable"]) for b in listed] == [(batch, 4, 4)]

    undone = finish(messy, messy.client.post("/api/tools/organize/undo", json={"batch_id": batch})).result
    assert (undone["moved"], undone["skipped"]) == (4, 0)
    assert paths(messy) == original
    root = Path(messy.root)
    assert (root / "Downloads" / "a.lrc").read_text() == "[00:01.00]hello" and (root / "Downloads" / "cover.jpg").exists()
    assert not (root / "Aurora Vale").exists()
    assert messy.client.post("/api/tools/organize/undo", json={"batch_id": batch}).status_code == 404
    assert scan(messy)["missing"] == 0


def test_a_song_added_after_the_preview_is_handled_by_the_fresh_plan(messy) -> None:
    preview(messy)
    make_track(Path(messy.root) / "Downloads", "late.mp3", title="Late", artist="Aurora Vale", albumartist="Aurora Vale", album="Northern Lights", tracknumber=9)
    scan(messy)
    result = finish(messy, apply(messy)).result
    assert result["previewed"] == 4 and result["moved"] == 5


def test_files_that_cannot_move_are_reported_and_the_rest_still_do(messy) -> None:
    preview(messy)
    (Path(messy.root) / "Downloads" / "c.mp3").unlink()
    result = finish(messy, apply(messy)).result
    assert (result["moved"], result["skipped"], result["errors_total"]) == (3, 1, 1)
    assert "missing" in result["errors"][0]["error"]


def test_collisions_are_listed_and_never_overwritten(tmp_path: Path) -> None:
    root = tmp_path / "music"
    make_track(root / "One", "x.mp3", title="Same", artist="A", albumartist="A", album="B", tracknumber=1)
    make_track(root / "Two", "y.mp3", title="Same", artist="A", albumartist="A", album="B", tracknumber=1)
    web = build_web(tmp_path, root)
    scan(web)
    web.root = web.config.library.roots[0]
    job = preview(web)
    assert (job.result["moves"], job.result["collisions"]) == (1, 1)
    held = web.client.get(f"/api/tools/organize/preview/{job.id}", params={"kind": "collisions"}).json()
    assert held["total"] == 1 and held["items"][0]["to"] == "A/B/01 - Same.mp3"
    assert web.client.get(f"/api/tools/organize/preview/{job.id}", params={"kind": "sideways"}).status_code == 422
    finish(web, apply(web))
    survivors = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.mp3"))
    assert len(survivors) == 2 and "A/B/01 - Same.mp3" in survivors


def test_cancelling_keeps_what_moved_and_it_can_still_be_undone(messy, monkeypatch) -> None:
    started, gate = threading.Event(), threading.Event()
    real_move = moves.Mover.move

    def slow_move(self, *args, **kwargs):
        started.set()
        gate.wait(10)
        return real_move(self, *args, **kwargs)

    monkeypatch.setattr(moves.Mover, "move", slow_move)
    preview(messy)
    job = messy.client.post("/api/tools/organize/apply", json={"root": messy.root, "scheme": SCHEME}).json()["job"]
    assert started.wait(10)
    assert messy.client.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
    gate.set()
    assert messy.ctx.jobs.wait(job["id"]).status == "cancelled"

    batches = messy.client.get("/api/tools/organize/batches").json()["batches"]
    assert len(batches) == 1 and batches[0]["files"] == 1
    monkeypatch.setattr(moves.Mover, "move", real_move)
    undone = finish(messy, messy.client.post("/api/tools/organize/undo", json={"batch_id": batches[0]["batch_id"]})).result
    assert undone["moved"] == 1 and len(list((Path(messy.root) / "Downloads").glob("*.mp3"))) == 4


def test_a_second_apply_is_refused_while_files_are_moving(messy, monkeypatch) -> None:
    started, gate = threading.Event(), threading.Event()
    real_move = moves.Mover.move

    def slow_move(self, *args, **kwargs):
        started.set()
        gate.wait(10)
        return real_move(self, *args, **kwargs)

    monkeypatch.setattr(moves.Mover, "move", slow_move)
    preview(messy)
    job = messy.client.post("/api/tools/organize/apply", json={"root": messy.root, "scheme": SCHEME}).json()["job"]
    assert started.wait(10)
    try:
        assert apply(messy).status_code == 409
    finally:
        gate.set()
        messy.ctx.jobs.wait(job["id"])


def test_a_root_that_is_not_connected_is_refused(messy, tmp_path) -> None:
    gone = tmp_path / "usb-stick"
    gone.mkdir()
    messy.config.library.roots.append(str(gone))
    gone.rmdir()
    response = messy.client.post("/api/tools/organize/preview", json={"root": str(gone), "scheme": SCHEME})
    assert response.status_code == 409 and "drive" in response.json()["detail"]
    info = messy.client.get("/api/tools/organize/info").json()
    assert [r["available"] for r in info["roots"]] == [True, False]
