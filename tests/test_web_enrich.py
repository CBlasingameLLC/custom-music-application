import json
from pathlib import Path

import mutagen
import pytest

from musictoolkit.ingest import tagger
from tests.conftest import build_web, make_track, scan

RECORDINGS = {
    # seed title -> what MusicBrainz "finds"
    "Glacier": {"id": "mb-glacier", "title": "Glacier", "ext:score": "98", "artist-credit-phrase": "Aurora Vale",
                "release-list": [{"id": "rel-nl", "title": "Northern Lights"}]},
    "Some Song": {"id": "mb-some", "title": "Some Song", "ext:score": "72", "artist-credit-phrase": "Artist Name",
                  "release-list": [{"id": "rel-x", "title": "Their Album"}]},
    "Has Title": {"id": "mb-has", "title": "Has Title (Remastered)", "ext:score": "90", "artist-credit-phrase": "Known Artist",
                  "release-list": [{"id": "rel-h", "title": "Found Album"}]},
}


@pytest.fixture
def sparse(tmp_path: Path, monkeypatch):
    root = tmp_path / "music"
    make_track(root, "01 - Glacier.mp3")
    make_track(root, "Artist Name - Some Song.mp3")
    make_track(root, "mystery.mp3")
    make_track(root, "has_title.mp3", title="Has Title", artist="Known Artist")  # only the album is missing
    make_track(root, "complete.mp3", title="Whole", artist="A", album="B")  # nothing to fix
    web = build_web(tmp_path, root)
    web.ctx.config.musicbrainz.contact = "me@example.com"
    scan(web)
    monkeypatch.setattr(tagger.musicbrainz_client, "best_match", lambda artist, title: RECORDINGS.get(title))
    return web


def run(web, response) -> dict:
    assert response.status_code == 200, response.text
    job = web.ctx.jobs.wait(response.json()["job"]["id"], timeout=30)
    assert job.status == "done", job.error
    return job.result


def tags_of(path: Path) -> dict:
    tags = mutagen.File(path, easy=True).tags or {}
    return {key: tags[key][0] for key in tags.keys()}


def track(web, name: str):
    with web.ctx.db() as conn:
        return conn.execute("SELECT * FROM tracks WHERE file_path LIKE ?", (f"%{name}",)).fetchone()


def lookup(web) -> dict:
    return run(web, web.client.post("/api/tools/enrich/start", json={}))


def test_summary_counts_what_needs_attention(sparse) -> None:
    data = sparse.client.get("/api/tools/enrich/summary").json()
    assert data == {"to_look_up": 4, "pending": 0, "applied": 0, "dismissed": 0, "no_match": 0, "contact_set": True}


def test_a_lookup_needs_a_contact_email(sparse) -> None:
    sparse.ctx.config.musicbrainz.contact = " "
    response = sparse.client.post("/api/tools/enrich/start", json={})
    assert response.status_code == 409 and "Settings" in response.json()["detail"]
    assert sparse.client.get("/api/tools/enrich/summary").json()["contact_set"] is False


def test_lookup_runs_in_the_network_lane_and_stores_proposals(sparse) -> None:
    result = lookup(sparse)
    assert result == {"found": 3, "no_match": 1, "errors": 0}
    summary = sparse.client.get("/api/tools/enrich/summary").json()
    assert (summary["pending"], summary["no_match"], summary["to_look_up"]) == (3, 1, 0)
    lookups = [j for j in sparse.client.get("/api/jobs").json()["jobs"] if j["kind"] == "enrich"]
    assert len(lookups) == 1 and lookups[0]["done"] == lookups[0]["total"] == 4


def test_the_review_list_is_best_matches_first_and_shows_what_would_be_written(sparse) -> None:
    lookup(sparse)
    items = sparse.client.get("/api/tools/enrich/proposals").json()["items"]
    assert [i["proposed"]["title"] for i in items] == ["Glacier", "Has Title (Remastered)", "Some Song"]
    glacier, has_title, some = items
    assert glacier["confidence"] == 0.98 and glacier["file"] == "01 - Glacier.mp3"
    assert glacier["will_write"] == {"title": "Glacier", "artist": "Aurora Vale", "album": "Northern Lights"}
    # an existing title is not replaced unless asked: only the missing album is filled
    assert has_title["will_write"] == {"album": "Found Album"}
    replaced = sparse.client.get("/api/tools/enrich/proposals", params={"overwrite": True}).json()["items"][1]
    assert replaced["will_write"] == {"title": "Has Title (Remastered)", "album": "Found Album"}
    assert some["current"] == {"title": None, "artist": None, "album": None}


def test_the_list_can_be_filtered_and_paged(sparse) -> None:
    lookup(sparse)
    data = sparse.client.get("/api/tools/enrich/proposals", params={"min_confidence": 0.9}).json()
    assert data["total"] == 2
    page = sparse.client.get("/api/tools/enrich/proposals", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 3 and [i["proposed"]["title"] for i in page["items"]] == ["Has Title (Remastered)"]


def test_applying_writes_files_and_the_library_and_can_be_undone(sparse) -> None:
    lookup(sparse)
    glacier = track(sparse, "01 - Glacier.mp3")
    result = run(sparse, sparse.client.post("/api/tools/enrich/apply", json={"track_ids": [glacier["id"]]}))
    assert result["edited"] == 1 and result["errors"] == []

    assert tags_of(Path(glacier["file_path"])) == {"title": "Glacier", "artist": "Aurora Vale", "album": "Northern Lights"}
    row = track(sparse, "01 - Glacier.mp3")
    assert (row["title"], row["artist"], row["album"]) == ("Glacier", "Aurora Vale", "Northern Lights")
    assert (row["tag_source"], row["musicbrainz_recording_id"], row["mb_match_confidence"]) == ("musicbrainz", "mb-glacier", 0.98)
    assert sparse.client.get("/api/tools/enrich/summary").json()["applied"] == 1
    assert sparse.client.get("/api/tools/enrich/summary").json()["pending"] == 2

    undone = run(sparse, sparse.client.post("/api/tags/undo", json={"batch_id": result["batch_id"]}))
    assert undone["edited"] == 1
    assert "title" not in tags_of(Path(glacier["file_path"]))


def test_apply_everything_above_a_confidence(sparse) -> None:
    lookup(sparse)
    result = run(sparse, sparse.client.post("/api/tools/enrich/apply", json={"min_confidence": 0.9}))
    assert result["edited"] == 2
    assert sparse.client.get("/api/tools/enrich/summary").json()["pending"] == 1  # the 72% match waits
    assert track(sparse, "Artist Name - Some Song.mp3")["title"] is None


def test_overwrite_replaces_existing_tags(sparse) -> None:
    lookup(sparse)
    row = track(sparse, "has_title.mp3")
    run(sparse, sparse.client.post("/api/tools/enrich/apply", json={"track_ids": [row["id"]], "overwrite": True}))
    assert tags_of(Path(row["file_path"]))["title"] == "Has Title (Remastered)"


def test_a_proposal_whose_file_cannot_be_written_stays_for_another_try(sparse) -> None:
    lookup(sparse)
    row = track(sparse, "01 - Glacier.mp3")
    Path(row["file_path"]).unlink()
    # a vanished file is dropped from the review list as soon as a scan notices; here we go straight to apply
    result = run(sparse, sparse.client.post("/api/tools/enrich/apply", json={"track_ids": [row["id"]]}))
    assert result["edited"] == 0 and len(result["errors"]) == 1
    assert sparse.client.get("/api/tools/enrich/summary").json()["applied"] == 0


def test_dismissing_keeps_tags_and_stops_further_lookups(sparse) -> None:
    lookup(sparse)
    some = track(sparse, "Artist Name - Some Song.mp3")
    assert sparse.client.post("/api/tools/enrich/dismiss", json={"track_ids": [some["id"]]}).json() == {"dismissed": 1}
    assert track(sparse, "Artist Name - Some Song.mp3")["tag_source"] == "musicbrainz_rejected"
    assert sparse.client.get("/api/tools/enrich/summary").json()["dismissed"] == 1
    assert lookup(sparse) == {"found": 0, "no_match": 0, "errors": 0}  # nothing left to look up


def test_dismiss_everything_below_a_confidence(sparse) -> None:
    lookup(sparse)
    assert sparse.client.post("/api/tools/enrich/dismiss", json={"max_confidence": 0.8}).json() == {"dismissed": 1}


def test_retry_lets_dead_ends_be_looked_up_again(sparse, monkeypatch) -> None:
    lookup(sparse)
    sparse.client.post("/api/tools/enrich/dismiss", json={"max_confidence": 0.8})
    assert sparse.client.get("/api/tools/enrich/summary").json()["no_match"] == 1
    assert sparse.client.post("/api/tools/enrich/retry").json() == {"reset": 2}  # the no-match and the dismissed one
    assert sparse.client.get("/api/tools/enrich/summary").json()["to_look_up"] == 2
    monkeypatch.setitem(RECORDINGS, "mystery", {"id": "m", "title": "Mystery", "ext:score": "80", "artist-credit-phrase": "X"})
    assert lookup(sparse)["found"] == 2


def test_a_second_lookup_is_refused_while_one_runs(sparse, monkeypatch) -> None:
    import threading

    gate = threading.Event()
    monkeypatch.setattr(tagger.musicbrainz_client, "best_match", lambda artist, title: gate.wait(5) and None)
    first = sparse.client.post("/api/tools/enrich/start", json={})
    assert first.status_code == 200
    assert sparse.client.post("/api/tools/enrich/start", json={}).status_code == 409
    gate.set()
    sparse.ctx.jobs.wait(first.json()["job"]["id"])


def test_a_lookup_can_be_cancelled_and_keeps_what_it_found(sparse, monkeypatch) -> None:
    import threading

    started, gate = threading.Event(), threading.Event()

    def slow(artist, title):
        started.set()
        gate.wait(5)
        return RECORDINGS.get(title)

    monkeypatch.setattr(tagger.musicbrainz_client, "best_match", slow)
    job = sparse.client.post("/api/tools/enrich/start", json={}).json()["job"]
    started.wait(5)
    assert sparse.client.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
    gate.set()
    finished = sparse.ctx.jobs.wait(job["id"])
    assert finished.status == "cancelled"
    assert sparse.client.get("/api/tools/enrich/summary").json()["pending"] >= 1  # what was found is still there


def test_apply_needs_a_selection(sparse) -> None:
    assert sparse.client.post("/api/tools/enrich/apply", json={}).status_code == 422
    assert sparse.client.post("/api/tools/enrich/apply", json={"track_ids": [999]}).status_code == 404


def test_proposals_survive_a_restart(sparse, tmp_path) -> None:
    lookup(sparse)
    reopened = build_web(tmp_path, tmp_path / "music")
    assert reopened.client.get("/api/tools/enrich/summary").json()["pending"] == 3
    assert json.loads(reopened.client.get("/api/tools/enrich/proposals").text)["total"] == 3
