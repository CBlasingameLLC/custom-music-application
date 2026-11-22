"""The Convert routes: look first, start only for what was looked at, the history, and taking a conversion back."""

from __future__ import annotations

import io
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import soundfile as sf
from PIL import Image

from musictoolkit.convert import store
from musictoolkit.discs.tagging import SongTags, write_tags
from musictoolkit.media import art
from tests import convert_audio as audio
from tests.conftest import build_web, ids_by_title, scan

SCHEME = "{album_artist}/{album}/{track:02d} - {title}.{ext}"
TITLES = ("One", "Two", "Three")


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (240, 240), (30, 90, 200)).save(out, "JPEG")
    return out.getvalue()


COVER = _jpeg()


def make_library(root: Path) -> Path:
    folder = root / "Aurora Vale" / "Northern Lights"
    folder.mkdir(parents=True)
    for number, title in enumerate(TITLES, start=1):
        path = folder / f"{number:02d} {title}.flac"
        sf.write(str(path), audio.music(44100, 5, seed=number), 44100, format="FLAC", subtype="PCM_16")
        write_tags(path, "flac", SongTags(title, "Aurora Vale", "Northern Lights", "Aurora Vale", number, 3, year=2019, genre="Rock"), None)
    path = folder / "04 Four.mp3"
    sf.write(str(path), audio.music(44100, 5, seed=4), 44100, format="MP3", subtype="MPEG_LAYER_III", bitrate_mode="CONSTANT", compression_level=0.19)
    write_tags(path, "mp3", SongTags("Four", "Aurora Vale", "Northern Lights", "Aurora Vale", 4, 3, year=2019, genre="Rock"), None)
    return root


@pytest.fixture
def rig(tmp_path: Path) -> SimpleNamespace:
    music = make_library(tmp_path / "music")
    web = build_web(tmp_path, music)
    scan(web)
    return SimpleNamespace(web=web, client=web.client, ctx=web.ctx, music=music, out=tmp_path / "converted", ids=ids_by_title(web.client), tmp=tmp_path)


def body(rig: SimpleNamespace, **changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"format": "mp3", "quality": "320", "destination": str(rig.out), "song_ids": list(rig.ids.values())}
    return {**base, **changes}


def finish(rig: SimpleNamespace, response) -> Any:
    assert response.status_code == 200, response.text
    return rig.ctx.jobs.wait(response.json()["job"]["id"], timeout=120)


def look(rig: SimpleNamespace, **changes: Any):
    job = finish(rig, rig.client.post("/api/tools/convert/preview", json=body(rig, **changes)))
    assert job.status == "done", job.error
    return job


def convert(rig: SimpleNamespace, **changes: Any):
    look(rig, **changes)
    return finish(rig, rig.client.post("/api/tools/convert/start", json=body(rig, **changes)))


# ---------------------------------------------------------------------------------------- what the screen is told


def test_what_the_screen_is_told_before_anything_is_chosen(rig: SimpleNamespace) -> None:
    got = rig.client.get("/api/tools/convert/info").json()
    assert [f["id"] for f in got["formats"]] == ["mp3", "ogg", "opus", "flac", "wav"]
    mp3 = got["formats"][0]
    assert (mp3["label"], mp3["extension"], mp3["lossy"], mp3["default"], mp3["depth"]) == ("MP3", "mp3", True, "256", False)
    assert [q["id"] for q in mp3["qualities"]] == ["320", "256", "192", "128"] and [f["depth"] for f in got["formats"]] == [False, False, False, True, True]
    assert (got["max_rates"], got["depths"], got["existing"]) == ([0, 48000, 44100], ["16", "keep"], ["copy", "convert", "skip"])
    assert got["default_scheme"] == SCHEME and got["presets"][0]["scheme"] == SCHEME and got["roots"] == [str(rig.music)] and got["last"] == {} and got["busy"] is None
    assert not Path(got["default_destination"]).is_relative_to(rig.music) and set(got["windows_decoding"]) == {"available", "reason"}
    assert {f["name"] for f in got["fields"]} >= {"album_artist", "title", "track:02d", "ext"}


# ---------------------------------------------------------------------------------------- look, start, history, take back


def test_the_whole_way_from_the_preview_to_taking_the_conversion_back(rig: SimpleNamespace) -> None:
    job = look(rig)
    assert job.kind == "convert-preview"
    summary = job.result
    assert summary["ok"] is True and summary["problems"] == [] and summary["songs"] == 4 and (summary["format"], summary["quality"]) == ("mp3", "320")
    assert summary["counts"] == {"convert": 3, "copy": 1, "skip": 0, "cannot": 0, "exists": 0, "clash": 0} and summary["bytes"] > 0 and summary["destination"] == str(rig.out)
    page = rig.client.get(f"/api/tools/convert/preview/{job.id}", params={"action": "convert", "limit": 2}).json()
    assert page["total"] == 3 and [i["title"] for i in page["items"]] == ["One", "Two"] and page["items"][0]["to"] == "Aurora Vale/Northern Lights/01 - One.mp3"
    assert [i["title"] for i in rig.client.get(f"/api/tools/convert/preview/{job.id}", params={"action": "copy"}).json()["items"]] == ["Four"]
    assert not rig.out.exists()  # (looking writes nothing)

    started = rig.client.post("/api/tools/convert/start", json=body(rig))
    done = finish(rig, started)
    assert done.kind == "convert" and done.title == "Converting 4 songs to MP3" and done.status == "done", done.error
    assert (done.result["status"], done.result["converted"], done.result["copied"], done.result["failed"], done.result["previewed"]) == ("done", 3, 1, 0, 4)
    album = rig.out / "Aurora Vale" / "Northern Lights"
    assert sorted(p.name for p in album.iterdir()) == ["01 - One.mp3", "02 - Two.mp3", "03 - Three.mp3", "04 - Four.mp3"]
    assert sf.info(str(album / "01 - One.mp3")).format == "MP3"

    listed = rig.client.get("/api/tools/convert/batches").json()["batches"]
    assert len(listed) == 1 and (listed[0]["status"], listed[0]["converted"], listed[0]["copied"], listed[0]["format"], listed[0]["destination"]) == ("done", 3, 1, "mp3", str(rig.out))
    detail = rig.client.get(f"/api/tools/convert/batches/{listed[0]['id']}", params={"limit": 2, "offset": 1}).json()
    assert detail["items_total"] == 4 and [i["title"] for i in detail["items"]] == ["Two", "Three"] and detail["options"]["cover"] is True

    undone = finish(rig, rig.client.post("/api/tools/convert/undo", json={"conversion_id": listed[0]["id"]}))
    assert undone.kind == "convert-undo" and undone.status == "done" and undone.result["removed"] == 4
    assert rig.out.is_dir() and list(rig.out.iterdir()) == []  # (the folders the conversion made go with its songs; the destination is the person's own)
    assert rig.client.get(f"/api/tools/convert/batches/{listed[0]['id']}").json()["undone_at"]
    assert rig.client.post("/api/tools/convert/undo", json={"conversion_id": listed[0]["id"]}).status_code == 409


def test_what_the_person_chose_is_remembered_for_next_time(rig: SimpleNamespace) -> None:
    assert rig.client.get("/api/tools/convert/info").json()["last"] == {}
    convert(rig, format="opus", quality="96", max_rate=48000, existing="convert", cover=False, scheme="{artist} - {title}.{ext}")
    assert rig.client.get("/api/tools/convert/info").json()["last"] == {
        "format": "opus", "quality": "96", "destination": str(rig.out), "scheme": "{artist} - {title}.{ext}", "max_rate": 48000, "depth": "16", "existing": "convert", "cover": False,
    }
    assert sorted(p.name for p in rig.out.iterdir()) == [f"Aurora Vale - {t}.opus" for t in ("Four", "One", "Three", "Two")]


def test_the_songs_can_be_chosen_the_way_a_device_is(rig: SimpleNamespace) -> None:
    rig.client.post("/api/library/favorites", json={"ids": [rig.ids["Two"]], "favorite": True})
    got = look(rig, song_ids=[], sources=[{"kind": "all"}]).result
    assert got["songs"] == 4
    assert look(rig, song_ids=[], sources=[{"kind": "genre", "value": "Rock"}]).result["songs"] == 4
    assert look(rig, song_ids=[], sources=[{"kind": "genre", "value": "Jazz"}]).result["problems"] == [
        "There is nothing to convert: every song is already as asked for, left out, or cannot be converted."]
    both = look(rig, song_ids=[rig.ids["One"], rig.ids["One"]], sources=[{"kind": "all"}]).result
    assert both["songs"] == 4  # (a song in two places is converted once)


def test_songs_known_to_be_missing_are_left_out(rig: SimpleNamespace) -> None:
    with rig.ctx.db() as conn:
        conn.execute("UPDATE tracks SET is_missing = 1 WHERE title = 'Two'")
        conn.commit()
    assert look(rig).result["songs"] == 3 and look(rig, song_ids=[rig.ids["Two"]]).result["songs"] == 0


def test_a_song_id_the_library_does_not_have_is_left_out(rig: SimpleNamespace) -> None:
    assert look(rig, song_ids=[rig.ids["One"], 999_999]).result["songs"] == 1


# ---------------------------------------------------------------------------------------- nothing is written without a preview of the same


def test_nothing_starts_without_a_preview_of_the_same_choices(rig: SimpleNamespace) -> None:
    wanted = "Preview the conversion first. Nothing is written until you have seen what will happen."
    first = rig.client.post("/api/tools/convert/start", json=body(rig))
    assert (first.status_code, first.json()["detail"]) == (409, wanted)
    look(rig)
    for changes in ({"quality": "256"}, {"format": "flac", "quality": None}, {"destination": str(rig.tmp / "elsewhere")}, {"song_ids": [rig.ids["One"]]}, {"cover": False}, {"scheme": "{title}.{ext}"}):
        assert rig.client.post("/api/tools/convert/start", json=body(rig, **changes)).status_code == 409, changes
    assert not rig.out.exists() and not (rig.tmp / "elsewhere").exists()


def test_what_was_shown_is_used_up_by_the_conversion_it_started(rig: SimpleNamespace) -> None:
    look(rig)
    first = rig.client.post("/api/tools/convert/start", json=body(rig))
    finish(rig, first)
    again = rig.client.post("/api/tools/convert/start", json=body(rig))
    assert again.status_code == 409 and "Preview the conversion first" in again.json()["detail"]


def test_a_preview_with_a_problem_cannot_be_started_and_says_the_problem(rig: SimpleNamespace) -> None:
    inside = str(rig.music / "mp3s")
    got = look(rig, destination=inside).result
    assert got["ok"] is False and "outside your library folders" in got["problems"][0]
    refused = rig.client.post("/api/tools/convert/start", json=body(rig, destination=inside))
    assert refused.status_code == 409 and "outside your library folders" in refused.json()["detail"] and not (rig.music / "mp3s").exists()


@pytest.mark.parametrize("where,words", [("", "Choose a folder"), ("relative/folder", "whole path"), ("FILE", "is a file, not a folder")])
def test_a_destination_that_is_not_a_whole_folder_is_a_problem_the_preview_reports(rig: SimpleNamespace, where: str, words: str) -> None:
    if where == "FILE":
        where = str(rig.tmp / "a file")
        Path(where).write_bytes(b"x")
    got = look(rig, destination=where).result
    assert got["ok"] is False and words in got["problems"][0]
    refused = rig.client.post("/api/tools/convert/start", json=body(rig, destination=where))
    assert refused.status_code == 409 and words in refused.json()["detail"] and not rig.out.exists()


def test_a_preview_page_is_only_there_for_the_preview_that_was_made(rig: SimpleNamespace) -> None:
    job = look(rig)
    assert rig.client.get("/api/tools/convert/preview/nonsense").status_code == 404
    assert rig.client.get(f"/api/tools/convert/preview/{job.id}", params={"action": "everything"}).status_code == 422
    newer = look(rig, quality="128")
    assert rig.client.get(f"/api/tools/convert/preview/{job.id}").status_code == 404 and rig.client.get(f"/api/tools/convert/preview/{newer.id}").status_code == 200


@pytest.mark.parametrize("changes,words", [
    ({"format": "aac"}, "format must be one of"), ({"quality": "999"}, "quality for mp3 must be one of"), ({"max_rate": 96000}, "the highest rate must be one of"),
    ({"depth": "32"}, "depth must be one of"), ({"existing": "overwrite"}, "existing songs must be one of"), ({"scheme": "{nonsense}/{title}.{ext}"}, "Invalid template"),
    ({"scheme": "{title}"}, "must end with the file extension"), ({"song_ids": [], "sources": []}, "Choose what to convert"),
    ({"song_ids": [], "sources": [{"kind": "nonsense"}]}, "unknown source"), ({"song_ids": [], "sources": [{"kind": "rating", "min": 9}]}, "minimum of 1 to 5 stars"),
])
def test_a_choice_that_is_not_offered_is_refused_in_words_by_the_preview_and_the_start(rig: SimpleNamespace, changes: dict[str, Any], words: str) -> None:
    for route in ("preview", "start"):
        refused = rig.client.post(f"/api/tools/convert/{route}", json=body(rig, **changes))
        assert refused.status_code == 422 and words in refused.json()["detail"], (route, refused.text)
    assert not rig.out.exists()


def test_the_body_must_have_the_right_shape(rig: SimpleNamespace) -> None:
    assert rig.client.post("/api/tools/convert/preview", json={"quality": "320"}).status_code == 422
    assert rig.client.post("/api/tools/convert/preview", json=body(rig, song_ids=["x"])).status_code == 422
    assert rig.client.post("/api/tools/convert/preview", json=body(rig, destination="x" * 2000)).status_code == 422


# ---------------------------------------------------------------------------------------- only one at a time


def test_a_start_is_refused_while_another_conversion_or_a_tidying_of_the_library_is_going(rig: SimpleNamespace) -> None:
    import threading

    look(rig)
    release = threading.Event()
    blocker = rig.ctx.jobs.submit("convert", "Another conversion", lambda handle: release.wait(10) and None, lane="convert")
    try:
        refused = rig.client.post("/api/tools/convert/start", json=body(rig))
        assert (refused.status_code, refused.json()["detail"]) == (409, "Songs are already being converted.")
        assert rig.client.get("/api/tools/convert/info").json()["busy"] == "Songs are already being converted."
    finally:
        release.set()
        rig.ctx.jobs.wait(blocker.id)
    release2 = threading.Event()
    moving = rig.ctx.jobs.submit("organize", "Moving files", lambda handle: release2.wait(10) and None)
    try:
        refused = rig.client.post("/api/tools/convert/start", json=body(rig))
        assert refused.status_code == 409 and "being moved" in refused.json()["detail"]
    finally:
        release2.set()
        rig.ctx.jobs.wait(moving.id)
    allowed = rig.client.post("/api/tools/convert/start", json=body(rig))  # (the preview was not used up by the refusals)
    assert finish(rig, allowed).status == "done"


# ---------------------------------------------------------------------------------------- the history


def test_the_history_knows_what_is_not_there(rig: SimpleNamespace) -> None:
    assert rig.client.get("/api/tools/convert/batches").json() == {"batches": []}
    assert rig.client.get("/api/tools/convert/batches/99").status_code == 404
    assert rig.client.post("/api/tools/convert/undo", json={"conversion_id": 99}).status_code == 404
    with rig.ctx.db() as conn:
        conversion_id, _ = store.create_conversion(conn, batch_id="empty", title="none", fmt="mp3", quality="256", destination=str(rig.out), scheme=SCHEME, options={}, left_out=0, items=[])
        store.finish_conversion(conn, conversion_id, "done")
    refused = rig.client.post("/api/tools/convert/undo", json={"conversion_id": conversion_id})
    assert (refused.status_code, refused.json()["detail"]) == (409, "That conversion made no files to take back.")


def test_a_conversions_songs_can_be_narrowed_to_those_that_were_not_made(rig: SimpleNamespace) -> None:
    def item(title: str) -> dict[str, Any]:
        return {"track_id": None, "title": title, "source": f"/s/{title}", "target": str(rig.out / f"{title}.mp3"), "action": "convert"}

    with rig.ctx.db() as conn:
        conversion_id, ids = store.create_conversion(
            conn, batch_id="mixed", title="x", fmt="mp3", quality="256", destination=str(rig.out), scheme=SCHEME, options={}, left_out=0,
            items=[item(t) for t in ("made", "failed", "left", "also made", "also failed")],
        )
        for item_id, status in zip(ids, ("done", "failed", "skipped", "done", "failed")):
            store.set_item(conn, item_id, status=status, message=None if status == "done" else f"{status} because")
        assert [i["title"] for i in store.get_conversion(conn, conversion_id, only="not_made")["items"]] == ["failed", "left", "also failed"]
    everything = rig.client.get(f"/api/tools/convert/batches/{conversion_id}").json()
    assert everything["items_total"] == 5 and len(everything["items"]) == 5
    narrowed = rig.client.get(f"/api/tools/convert/batches/{conversion_id}", params={"only": "not_made", "limit": 2, "offset": 1}).json()
    assert narrowed["items_total"] == 3 and [(i["title"], i["status"], i["message"]) for i in narrowed["items"]] == [("left", "skipped", "skipped because"), ("also failed", "failed", "failed because")]
    refused = rig.client.get(f"/api/tools/convert/batches/{conversion_id}", params={"only": "everything"})
    assert (refused.status_code, refused.json()["detail"]) == (422, "only must be one of: not_made")
    assert rig.client.get("/api/tools/convert/batches/99", params={"only": "not_made"}).status_code == 404


def test_taking_back_while_something_is_converting_waits(rig: SimpleNamespace) -> None:
    import threading

    convert(rig)
    release = threading.Event()
    blocker = rig.ctx.jobs.submit("convert", "Another conversion", lambda handle: release.wait(10) and None, lane="convert")
    try:
        refused = rig.client.post("/api/tools/convert/undo", json={"conversion_id": 1})
        assert (refused.status_code, refused.json()["detail"]) == (409, "Songs are already being converted.")
    finally:
        release.set()
        rig.ctx.jobs.wait(blocker.id)


def test_a_file_that_is_already_there_is_left_alone_by_the_conversion(rig: SimpleNamespace) -> None:
    album = rig.out / "Aurora Vale" / "Northern Lights"
    album.mkdir(parents=True)
    (album / "02 - Two.mp3").write_bytes(b"the person's own")
    done = convert(rig)
    assert (done.result["converted"], done.result["copied"], done.result["left_out"]) == (2, 1, 1)
    assert (album / "02 - Two.mp3").read_bytes() == b"the person's own" and sorted(p.name for p in album.iterdir()) == ["01 - One.mp3", "02 - Two.mp3", "03 - Three.mp3", "04 - Four.mp3"]
    page = rig.client.get(f"/api/tools/convert/batches/{done.result['conversion_id']}").json()
    assert page["left_out"] == 1 and sorted(i["title"] for i in page["items"]) == ["Four", "One", "Three"]  # (the one that was there is not in the history: nothing of it was made)


def test_the_cover_goes_with_the_songs_unless_the_person_said_not(rig: SimpleNamespace) -> None:
    (rig.music / "Aurora Vale" / "Northern Lights" / "cover.jpg").write_bytes(COVER)
    convert(rig, cover=True)
    convert(rig, cover=False, destination=str(rig.tmp / "plain"))
    with_cover = rig.out / "Aurora Vale" / "Northern Lights"
    without = rig.tmp / "plain" / "Aurora Vale" / "Northern Lights"
    assert all(art.embedded_art(with_cover / f"{n:02d} - {t}.mp3") == COVER for n, t in ((1, "One"), (2, "Two"), (3, "Three")))
    assert all(art.embedded_art(without / f"{n:02d} - {t}.mp3") is None for n, t in ((1, "One"), (2, "Two"), (3, "Three")))
    assert art.embedded_art(with_cover / "04 - Four.mp3") is None  # (a song that is only copied is copied as it is)


def test_a_conversion_the_app_was_closed_in_the_middle_of_is_marked_when_it_starts_again(rig: SimpleNamespace) -> None:
    from musictoolkit.web.app import create_app

    rig.out.mkdir()
    half = rig.out / "x.mp3.part"
    half.write_bytes(b"half")
    with rig.ctx.db() as conn:
        conversion_id, ids = store.create_conversion(conn, batch_id="cut", title="x", fmt="mp3", quality="256", destination=str(rig.out), scheme=SCHEME, options={}, left_out=0,
                                                     items=[{"track_id": 1, "title": "X", "source": "/s", "target": str(rig.out / "x.mp3"), "action": "convert"}])
        store.set_item(conn, ids[0], status="working")
    create_app(db_path=rig.ctx.db_path, config_path=rig.ctx.config_path, config=rig.ctx.config)
    with rig.ctx.db() as conn:
        saved = store.get_conversion(conn, conversion_id)
    assert (saved["status"], saved["failed"]) == ("interrupted", 1) and not half.exists()


def test_the_converted_files_are_never_in_the_library(rig: SimpleNamespace) -> None:
    convert(rig)
    assert os.path.commonpath([str(rig.out), str(rig.music)]) != str(rig.music)
    assert rig.client.get("/api/tracks", params={"limit": 1000}).json()["total"] == 4  # (the library still holds only what it held)
