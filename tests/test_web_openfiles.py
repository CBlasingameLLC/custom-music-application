"""POST /api/library/open-files: songs handed over from outside the app are added to the library and answered as tracks."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import ids_by_title, make_track


def test_an_opened_song_joins_the_library_and_comes_back_ready_to_play(web, tmp_path: Path) -> None:
    song = make_track(tmp_path / "elsewhere", "far.mp3", title="Far Away", artist="Someone", album="Somewhere")

    answer = web.client.post("/api/library/open-files", json={"paths": [str(song)]}).json()

    assert [t["title"] for t in answer["tracks"]] == ["Far Away"] and answer["skipped"] == [] and answer["added"] == 1
    assert web.client.get("/api/about").json()["tracks"] == 10, "the nine songs of the library and this one"
    stream = web.client.get(f"/api/tracks/{answer['tracks'][0]['id']}/stream")
    assert stream.status_code == 200 and stream.headers["content-type"].startswith("audio/")


def test_a_song_already_in_the_library_is_not_added_twice(web) -> None:
    ids = ids_by_title(web.client)
    path = web.client.get(f"/api/tracks/{ids['Glacier']}").json()["path"]

    answer = web.client.post("/api/library/open-files", json={"paths": [path]}).json()

    assert [t["id"] for t in answer["tracks"]] == [ids["Glacier"]] and answer["added"] == 0
    assert web.client.get("/api/about").json()["tracks"] == 9


def test_songs_come_back_in_the_order_given_without_repeats_and_what_cannot_be_used_is_explained(web, tmp_path: Path) -> None:
    folder = tmp_path / "elsewhere"
    one, two = make_track(folder, "a.mp3", title="A"), make_track(folder, "b.mp3", title="B")
    notes = folder / "notes.txt"
    notes.write_text("hello")

    answer = web.client.post(
        "/api/library/open-files",
        json={"paths": [str(two), str(notes), str(one), str(two), str(folder / "gone.mp3"), "relative.mp3"]},
    ).json()

    assert [t["title"] for t in answer["tracks"]] == ["B", "A"]
    assert answer["skipped"] == [
        {"path": str(notes), "reason": "Not an audio file"},
        {"path": str(folder / "gone.mp3"), "reason": "File not found"},
        {"path": "relative.mp3", "reason": "Could not be read"},
    ]


def test_a_folder_opens_as_the_songs_inside_it(web, tmp_path: Path) -> None:
    folder = tmp_path / "album"
    for n in (1, 2, 3):
        make_track(folder, f"{n}.mp3", title=f"Song {n}", tracknumber=n)

    answer = web.client.post("/api/library/open-files", json={"paths": [str(folder)]}).json()

    assert sorted(t["title"] for t in answer["tracks"]) == ["Song 1", "Song 2", "Song 3"] and answer["added"] == 3


def test_the_request_is_checked(web) -> None:
    assert web.client.post("/api/library/open-files", json={"paths": []}).status_code == 422
    assert web.client.post("/api/library/open-files", json={"paths": ["x"] * 501}).status_code == 422
    assert web.client.post("/api/library/open-files", json={"paths": [3]}).status_code == 422
    assert web.client.post("/api/library/open-files", json={}).status_code == 422
