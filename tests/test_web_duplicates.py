"""The duplicates and missing-files API: search, choose, park in the review folder, restore, delete."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

from musictoolkit.ingest import moves
from musictoolkit.ingest.scanner import QUARANTINE_DIR
from tests.conftest import build_web, make_track, scan

RICH = {"title": "Glacier", "artist": "Aurora Vale", "albumartist": "Aurora Vale", "album": "Northern Lights", "tracknumber": 1,
        "date": "2019", "genre": "Electronic"}
BARE = {"title": "Glacier", "artist": "Aurora Vale"}


@pytest.fixture
def twins(tmp_path: Path):
    """Glacier twice (a well-tagged copy in its album folder, a bare one in Downloads), plus two songs that are fine."""
    root = tmp_path / "music"
    make_track(root / "Aurora Vale" / "Northern Lights", "01 - Glacier.mp3", **RICH)
    make_track(root / "Downloads", "glacier (1).mp3", **BARE)
    (root / "Downloads" / "glacier (1).lrc").write_text("[00:01.00]words")
    make_track(root / "Aurora Vale" / "Northern Lights", "02 - Drift.mp3", title="Drift", artist="Aurora Vale", album="Northern Lights", tracknumber=2)
    make_track(root / "Mix", "other.mp3", title="Static", artist="Mara Quinn")
    web = build_web(tmp_path, root)
    scan(web)
    web.root = Path(web.config.library.roots[0]).resolve()
    return web


def finish(web, response, expect: str = "done"):
    assert response.status_code == 200, response.text
    job = web.ctx.jobs.wait(response.json()["job"]["id"], timeout=30)
    assert job.status == expect, job.error
    return job


def search(web, exact: bool = False):
    return finish(web, web.client.post("/api/tools/duplicates/scan", json={"exact": exact}))


def only_group(web, job):
    page = web.client.get(f"/api/tools/duplicates/groups/{job.id}").json()
    assert page["total"] == 1, page
    return page["items"][0]


def choice(group, keeper=None, remove=None):
    keeper = keeper or group["keeper"]
    return {"key": group["key"], "keeper": keeper, "remove": remove or [m["id"] for m in group["members"] if m["id"] != keeper]}


def quarantine(web, job, group, **kw):
    return web.client.post("/api/tools/duplicates/quarantine", json={"job_id": job.id, "choices": [choice(group, **kw)]})


def library_titles(web) -> list[str]:
    return sorted(i["title"] for i in web.client.get("/api/tracks", params={"limit": 100}).json()["items"])


def playlist_ids(web, playlist_id: int) -> list[int]:
    items = web.client.get("/api/tracks", params={"playlist": playlist_id, "limit": 1000}).json()["items"]
    return [t["id"] for t in items]


def track_id(web, name: str) -> int:
    with web.ctx.db() as conn:
        return conn.execute("SELECT id FROM tracks WHERE file_path LIKE ?", (f"%{name}",)).fetchone()["id"]


@pytest.fixture
def fake_bin(monkeypatch, tmp_path):
    """Keep tests off the real Recycle Bin: 'trashing' moves the file into a folder we can look in."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    thrown: list[str] = []

    def trash(path: str) -> None:
        thrown.append(Path(path).name)
        Path(path).rename(bin_dir / f"{len(thrown)}-{Path(path).name}")

    monkeypatch.setattr("send2trash.send2trash", trash)
    return thrown


def test_searching_finds_the_pair_and_suggests_the_better_tagged_copy(twins) -> None:
    job = search(twins)
    assert job.result == {"groups": 1, "copies": 1, "bytes": job.result["bytes"], "exact": False, "ignored": 0}
    group = only_group(twins, job)
    assert group["label"] == "Same artist, title and length" and group["identical"] is False
    paths = [m["path"] for m in group["members"]]
    assert paths[0].endswith("01 - Glacier.mp3") and paths[1].endswith("glacier (1).mp3")
    assert group["keeper"] == group["members"][0]["id"]
    assert {"id", "path", "format", "bitrate", "size", "plays", "rating", "favorite"} <= set(group["members"][0])
    lanes = [j for j in twins.client.get("/api/jobs").json()["jobs"] if j["kind"] == "dedupe-scan"]
    assert len(lanes) == 1


def test_the_last_search_is_remembered_for_when_the_page_is_reopened(twins) -> None:
    assert twins.client.get("/api/tools/duplicates/info").json()["scan"] is None
    job = search(twins)
    remembered = twins.client.get("/api/tools/duplicates/info").json()["scan"]
    assert remembered["job_id"] == job.id and remembered["groups"] == 1 and remembered["copies"] == 1
    assert twins.client.get(f"/api/tools/duplicates/groups/{remembered['job_id']}").json()["total"] == 1


def test_searching_needs_a_music_folder(empty_web) -> None:
    assert empty_web.client.post("/api/tools/duplicates/scan", json={}).status_code == 409


def test_an_exact_search_finds_byte_for_byte_copies_whatever_their_tags_say(twins) -> None:
    make_track(twins.root / "Backup", "untagged one.mp3")
    shutil.copy(twins.root / "Backup" / "untagged one.mp3", twins.root / "Backup" / "untagged two.mp3")
    scan(twins)

    assert search(twins).result["groups"] == 1, "tags alone cannot tell these two apart from nothing"
    job = search(twins, exact=True)

    assert job.result["groups"] == 2 and job.result["exact"] is True
    by_label = {g["label"]: g for g in twins.client.get(f"/api/tools/duplicates/groups/{job.id}").json()["items"]}
    exact = by_label["Identical files"]
    assert exact["identical"] and sorted(Path(m["path"]).name for m in exact["members"]) == ["untagged one.mp3", "untagged two.mp3"]
    assert by_label["Same artist, title and length"]["identical"] is False


def test_choices_that_do_not_match_the_list_are_refused(twins) -> None:
    job = search(twins)
    group = only_group(twins, job)
    other = track_id(twins, "other.mp3")
    post = twins.client.post
    base = {"job_id": job.id}
    assert post("/api/tools/duplicates/quarantine", json={**base, "choices": [choice(group, keeper=other)]}).status_code == 422
    assert post("/api/tools/duplicates/quarantine", json={**base, "choices": [choice(group, remove=[other])]}).status_code == 422
    both = [m["id"] for m in group["members"]]
    assert post("/api/tools/duplicates/quarantine", json={**base, "choices": [{"key": group["key"], "keeper": both[0], "remove": both}]}).status_code == 422
    assert post("/api/tools/duplicates/quarantine", json={**base, "choices": [{**choice(group), "key": "nope"}]}).status_code == 422
    assert post("/api/tools/duplicates/quarantine", json={"job_id": "stale", "choices": [choice(group)]}).status_code == 409
    assert (twins.root / "Downloads" / "glacier (1).mp3").exists()


def test_moving_a_copy_parks_it_hides_it_and_keeps_scans_from_reviving_it(twins) -> None:
    job = search(twins)
    group = only_group(twins, job)
    bare_id = group["members"][1]["id"]

    result = finish(twins, quarantine(twins, job, group)).result

    assert (result["moved"], result["skipped"], result["errors"]) == (1, 0, [])
    parked = twins.root / QUARANTINE_DIR / "Downloads" / "glacier (1).mp3"
    assert parked.exists() and parked.with_suffix(".lrc").read_text() == "[00:01.00]words"
    assert (twins.root / "Aurora Vale" / "Northern Lights" / "01 - Glacier.mp3").exists()
    assert not (twins.root / "Downloads").exists()
    assert library_titles(twins).count("Glacier") == 1
    again = scan(twins)
    assert (again["added"], again["missing"]) == (0, 0)
    assert library_titles(twins).count("Glacier") == 1
    info = twins.client.get("/api/tools/duplicates/info").json()
    assert info["review"]["count"] == 1 and info["batches"][0]["files"] == 1
    assert search(twins).result["groups"] == 0
    # the list that was on screen is used up
    assert twins.client.post("/api/tools/duplicates/quarantine", json={"job_id": job.id, "choices": [choice(group)]}).status_code == 409
    assert bare_id


def test_the_review_folder_lists_where_each_copy_came_from_and_restores_it(twins) -> None:
    job = search(twins)
    group = only_group(twins, job)
    finish(twins, quarantine(twins, job, group))

    listing = twins.client.get("/api/tools/duplicates/review").json()
    [item] = listing["items"]
    assert listing["total"] == 1 and listing["count"] == 1 and item["exists"]
    assert item["original"] == str(twins.root / "Downloads" / "glacier (1).mp3")

    result = finish(twins, twins.client.post("/api/tools/duplicates/restore", json={"track_ids": [item["id"]]})).result
    assert (result["moved"], result["skipped"]) == (1, 0)
    assert (twins.root / "Downloads" / "glacier (1).mp3").exists() and (twins.root / "Downloads" / "glacier (1).lrc").exists()
    assert library_titles(twins).count("Glacier") == 2
    assert twins.client.get("/api/tools/duplicates/review").json()["total"] == 0


def test_a_whole_batch_can_be_undone_and_only_once(twins) -> None:
    job = search(twins)
    group = only_group(twins, job)
    batch = finish(twins, quarantine(twins, job, group)).result["batch_id"]

    undone = finish(twins, twins.client.post("/api/tools/duplicates/undo", json={"batch_id": batch})).result
    assert undone["moved"] == 1 and library_titles(twins).count("Glacier") == 2
    assert twins.client.post("/api/tools/duplicates/undo", json={"batch_id": batch}).status_code == 404
    assert twins.client.get("/api/tools/duplicates/batches").json()["batches"][0]["undoable"] == 0


def test_plays_and_playlists_go_to_the_kept_copy_and_come_back_on_restore(twins) -> None:
    job = search(twins)
    group = only_group(twins, job)
    keeper, dup = group["members"][0]["id"], group["members"][1]["id"]
    playlist = twins.client.post("/api/playlists", json={"name": "Chill", "track_ids": [dup]}).json()
    with twins.ctx.db() as conn:
        for n in range(3):
            conn.execute("INSERT INTO play_history (track_id, source, played_at_epoch) VALUES (?, 'future_scrobble', ?)", (dup, 1_700_000_000 + n))
        conn.execute("UPDATE tracks SET rating = 5, favorite = 1 WHERE id = ?", (dup,))
        conn.commit()

    finish(twins, quarantine(twins, job, group))

    kept = twins.client.get(f"/api/tracks/{keeper}").json()
    assert (kept["plays"], kept["rating"], kept["favorite"]) == (3, 5, True)
    assert playlist_ids(twins, playlist["id"]) == [keeper]

    finish(twins, twins.client.post("/api/tools/duplicates/restore", json={"track_ids": [dup]}))
    assert twins.client.get(f"/api/tracks/{keeper}").json()["plays"] == 0
    assert twins.client.get(f"/api/tracks/{dup}").json()["plays"] == 3
    assert playlist_ids(twins, playlist["id"]) == [dup]


def test_saying_a_group_is_not_duplicates_removes_it_for_good(twins) -> None:
    job = search(twins)
    group = only_group(twins, job)

    assert twins.client.post("/api/tools/duplicates/ignore", json={"keys": [group["key"]]}).json() == {"ignored": 1, "total_ignored": 1}
    assert twins.client.get(f"/api/tools/duplicates/groups/{job.id}").json()["total"] == 0
    again = search(twins)
    assert again.result["groups"] == 0 and again.result["ignored"] == 1
    assert twins.client.post("/api/tools/duplicates/ignore/reset").json() == {"cleared": 1}
    assert search(twins).result["groups"] == 1


def test_deleting_needs_the_word_delete_and_goes_through_the_bin(twins, fake_bin) -> None:
    job = search(twins)
    group = only_group(twins, job)
    finish(twins, quarantine(twins, job, group))
    [item] = twins.client.get("/api/tools/duplicates/review").json()["items"]
    post = twins.client.post

    assert post("/api/tools/duplicates/delete", json={"track_ids": [item["id"]]}).status_code == 422
    assert post("/api/tools/duplicates/delete", json={"track_ids": [item["id"]], "confirm": "delete"}).status_code == 422
    assert post("/api/tools/duplicates/delete", json={"confirm": "DELETE"}).status_code == 422  # no selection
    assert Path(item["path"]).exists()

    result = finish(twins, post("/api/tools/duplicates/delete", json={"track_ids": [item["id"]], "confirm": "DELETE"})).result

    assert (result["moved"], result["skipped"]) == (1, 0) and sorted(fake_bin) == ["glacier (1).lrc", "glacier (1).mp3"]
    assert not Path(item["path"]).exists() and not (twins.root / QUARANTINE_DIR).exists()
    assert twins.client.get("/api/tools/duplicates/review").json()["total"] == 0
    assert (twins.root / "Aurora Vale" / "Northern Lights" / "01 - Glacier.mp3").exists()


def test_everything_in_the_review_folder_can_be_restored_or_deleted_at_once(twins, fake_bin) -> None:
    make_track(twins.root / "Spare", "static copy.mp3", title="Static", artist="Mara Quinn")
    scan(twins)
    job = search(twins)
    groups = twins.client.get(f"/api/tools/duplicates/groups/{job.id}").json()["items"]
    assert len(groups) == 2
    body = {"job_id": job.id, "choices": [choice(g) for g in groups]}
    finish(twins, twins.client.post("/api/tools/duplicates/quarantine", json=body))
    assert twins.client.get("/api/tools/duplicates/review").json()["count"] == 2

    finish(twins, twins.client.post("/api/tools/duplicates/restore", json={"everything": True}))
    assert twins.client.get("/api/tools/duplicates/review").json()["count"] == 0

    job = search(twins)
    groups = twins.client.get(f"/api/tools/duplicates/groups/{job.id}").json()["items"]
    finish(twins, twins.client.post("/api/tools/duplicates/quarantine", json={"job_id": job.id, "choices": [choice(g) for g in groups]}))
    finish(twins, twins.client.post("/api/tools/duplicates/delete", json={"everything": True, "confirm": "DELETE"}))
    assert twins.client.get("/api/tools/duplicates/review").json()["count"] == 0 and len(fake_bin) >= 2


def test_other_file_moves_are_refused_while_one_is_running(twins, monkeypatch) -> None:
    started, gate = threading.Event(), threading.Event()
    real_move = moves.Mover.move

    def slow_move(self, *args, **kwargs):
        started.set()
        gate.wait(10)
        return real_move(self, *args, **kwargs)

    monkeypatch.setattr(moves.Mover, "move", slow_move)
    job = search(twins)
    group = only_group(twins, job)
    running = quarantine(twins, job, group).json()["job"]
    assert started.wait(10)
    try:
        post = twins.client.post
        assert post("/api/tools/duplicates/restore", json={"track_ids": [1]}).status_code == 409
        assert post("/api/tools/duplicates/delete", json={"track_ids": [1], "confirm": "DELETE"}).status_code == 409
        assert post("/api/tools/organize/undo", json={"batch_id": "x"}).status_code == 404  # unknown batch: nothing to undo
    finally:
        gate.set()
        twins.ctx.jobs.wait(running["id"])


def test_restoring_everything_from_an_empty_review_folder_says_so(twins) -> None:
    assert twins.client.post("/api/tools/duplicates/restore", json={"everything": True}).status_code == 422


def test_the_job_keeps_what_moved_when_cancelled(twins, monkeypatch) -> None:
    started, gate = threading.Event(), threading.Event()
    real_move = moves.Mover.move

    def slow_move(self, *args, **kwargs):
        started.set()
        gate.wait(10)
        return real_move(self, *args, **kwargs)

    make_track(twins.root / "Spare", "static copy.mp3", title="Static", artist="Mara Quinn")
    scan(twins)
    job = search(twins)
    groups = twins.client.get(f"/api/tools/duplicates/groups/{job.id}").json()["items"]
    monkeypatch.setattr(moves.Mover, "move", slow_move)
    running = twins.client.post("/api/tools/duplicates/quarantine", json={"job_id": job.id, "choices": [choice(g) for g in groups]}).json()["job"]
    assert started.wait(10)
    assert twins.client.post(f"/api/jobs/{running['id']}/cancel").status_code == 200
    gate.set()
    assert twins.ctx.jobs.wait(running["id"]).status == "cancelled"
    monkeypatch.setattr(moves.Mover, "move", real_move)
    assert twins.client.get("/api/tools/duplicates/review").json()["count"] == 1
    assert twins.client.get("/api/tools/duplicates/batches").json()["batches"][0]["undoable"] == 1


# ------------------------------------------------------------------------------------------------ missing files


@pytest.fixture
def gone(tmp_path: Path):
    root = tmp_path / "music"
    make_track(root / "Band", "keep.mp3", title="Keep", artist="Band")
    lost = make_track(root / "Band", "lost.mp3", title="Lost", artist="Band")
    lost2 = make_track(root / "Old", "lost2.mp3", title="Lost Too", artist="Band")
    web = build_web(tmp_path, root)
    scan(web)
    lost.unlink()
    lost2.unlink()
    assert scan(web)["missing"] == 2
    return web


def test_the_missing_page_counts_songs_by_folder(gone) -> None:
    data = gone.client.get("/api/tools/missing/summary").json()
    assert data["total"] == 2 and data["roots"][0]["count"] == 2 and data["roots"][0]["available"] is True
    page = gone.client.get("/api/tools/missing/items").json()
    assert page["total"] == 2 and {i["title"] for i in page["items"]} == {"Lost", "Lost Too"}
    assert gone.client.get("/api/tools/missing/items", params={"offset": 1, "limit": 1}).json()["items"][0]["title"] == "Lost Too"


def test_forgetting_needs_confirmation_and_a_target(gone) -> None:
    post = gone.client.post
    assert post("/api/tools/missing/forget", json={"everything": True}).status_code == 422
    assert post("/api/tools/missing/forget", json={"confirm": True}).status_code == 422
    assert gone.client.get("/api/tools/missing/summary").json()["total"] == 2


def test_forgetting_removes_songs_playlist_entries_and_lets_the_library_move_on(gone) -> None:
    with gone.ctx.db() as conn:
        lost_id = conn.execute("SELECT id FROM tracks WHERE title = 'Lost'").fetchone()["id"]
        keep_id = conn.execute("SELECT id FROM tracks WHERE title = 'Keep'").fetchone()["id"]
    playlist = gone.client.post("/api/playlists", json={"name": "Mix", "track_ids": [keep_id, lost_id]}).json()

    outcome = gone.client.post("/api/tools/missing/forget", json={"track_ids": [lost_id], "confirm": True}).json()

    assert outcome == {"forgotten": 1, "kept": 0}
    assert gone.client.get("/api/tools/missing/summary").json()["total"] == 1
    assert playlist_ids(gone, playlist["id"]) == [keep_id]
    everything = gone.client.post("/api/tools/missing/forget", json={"everything": True, "confirm": True}).json()
    assert everything["forgotten"] == 1 and gone.client.get("/api/tools/missing/summary").json()["total"] == 0
