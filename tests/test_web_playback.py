import io

from PIL import Image

from tests.conftest import add_cover, ids_by_title, jpeg_bytes, make_track


def test_stream_serves_the_file_with_the_right_type(web) -> None:
    track_id = ids_by_title(web.client)["Glacier"]
    response = web.client.get(f"/api/tracks/{track_id}/stream")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.content[:3] == b"ID3" or response.content[:2] == b"\xff\xfb"


def test_stream_answers_range_requests_for_seeking(web) -> None:
    track_id = ids_by_title(web.client)["Glacier"]
    whole = web.client.get(f"/api/tracks/{track_id}/stream").content
    part = web.client.get(f"/api/tracks/{track_id}/stream", headers={"Range": "bytes=10-19"})
    assert part.status_code == 206
    assert part.headers["content-range"] == f"bytes 10-19/{len(whole)}"
    assert part.content == whole[10:20]
    tail = web.client.get(f"/api/tracks/{track_id}/stream", headers={"Range": "bytes=-5"})
    assert tail.content == whole[-5:]


def test_a_file_deleted_from_disk_is_a_404_and_gets_hidden(web) -> None:
    from pathlib import Path

    track_id = ids_by_title(web.client)["Wires"]
    Path(web.client.get(f"/api/tracks/{track_id}").json()["path"]).unlink()
    assert web.client.get(f"/api/tracks/{track_id}/stream").status_code == 404
    assert "Wires" not in ids_by_title(web.client)  # flagged missing, so the library stops listing it
    assert web.client.get("/api/tracks/424242/stream").status_code == 404


def test_embedded_cover_art_is_served_as_a_resized_jpeg(web) -> None:
    track_id = ids_by_title(web.client)["Glacier"]
    response = web.client.get(f"/api/art/track/{track_id}", params={"size": 64})
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    image = Image.open(io.BytesIO(response.content))
    assert max(image.size) == 64  # the 300px source was scaled down
    big = Image.open(io.BytesIO(web.client.get(f"/api/art/track/{track_id}", params={"size": 256}).content))
    assert max(big.size) == 256 or max(big.size) == 300  # never upscaled past the source


def test_tracks_without_art_are_404_so_the_ui_can_draw_a_placeholder(web) -> None:
    track_id = ids_by_title(web.client)["Wires"]
    assert web.client.get(f"/api/art/track/{track_id}").status_code == 404
    assert web.client.get(f"/api/art/track/{track_id}").status_code == 404  # negative result is cached, still 404


def test_folder_cover_image_is_used_when_the_file_has_none(web, tmp_path) -> None:
    folder = tmp_path / "music" / "Mara Quinn" / "Static Hearts"
    (folder / "cover.jpg").write_bytes(jpeg_bytes((10, 120, 200)))
    track_id = ids_by_title(web.client)["Wires"]
    assert web.client.get(f"/api/art/track/{track_id}", params={"size": 64}).status_code == 200


def test_album_art_falls_back_to_any_track_that_has_it(web) -> None:
    key = next(a["key"] for a in web.client.get("/api/albums").json()["items"] if a["album"] == "Northern Lights")
    assert web.client.get(f"/api/art/album/{key}", params={"size": 128}).status_code == 200
    plain = next(a["key"] for a in web.client.get("/api/albums").json()["items"] if a["album"] == "Back Roads")
    assert web.client.get(f"/api/art/album/{plain}").status_code == 404


def test_unsupported_thumbnail_size_snaps_to_a_known_one(web) -> None:
    track_id = ids_by_title(web.client)["Glacier"]
    assert web.client.get(f"/api/art/track/{track_id}", params={"size": 99999}).status_code == 200


def test_track_info_reports_format_details_and_playability(web) -> None:
    track_id = ids_by_title(web.client)["Glacier"]
    info = web.client.get(f"/api/tracks/{track_id}/info").json()
    assert info["playable"] is True and info["has_art"] is True
    assert info["size"] > 0 and info["sample_rate"]
    assert info["replaygain_track_db"] is None


def test_lyrics_from_a_sidecar_lrc_file_are_timed(web, tmp_path) -> None:
    track_id = ids_by_title(web.client)["Glacier"]
    path = web.client.get(f"/api/tracks/{track_id}").json()["path"]
    lrc = path.rsplit(".", 1)[0] + ".lrc"
    open(lrc, "w", encoding="utf-8").write("[ar:Aurora Vale]\n[00:01.00]First line\n[00:05.50]Second line\n")
    lyrics = web.client.get(f"/api/tracks/{track_id}/lyrics").json()
    assert lyrics["source"] == "lrc-file" and lyrics["plain"] is None
    assert lyrics["synced"] == [{"t": 1.0, "text": "First line"}, {"t": 5.5, "text": "Second line"}]


def test_lyrics_embedded_in_the_file_are_found(web) -> None:
    from mutagen.id3 import ID3, USLT

    track_id = ids_by_title(web.client)["Drift"]
    path = web.client.get(f"/api/tracks/{track_id}").json()["path"]
    tags = ID3(path)
    tags.add(USLT(encoding=3, lang="eng", desc="", text="Just floating\nalong"))
    tags.save()
    lyrics = web.client.get(f"/api/tracks/{track_id}/lyrics").json()
    assert lyrics["source"] == "embedded" and lyrics["plain"] == "Just floating\nalong" and lyrics["synced"] is None


def test_no_lyrics_is_an_empty_result_not_an_error(web) -> None:
    track_id = ids_by_title(web.client)["Wires"]
    assert web.client.get(f"/api/tracks/{track_id}/lyrics").json() == {"source": None, "synced": None, "plain": None}


class TestPlayLogging:
    def test_a_play_is_recorded_with_names_copied_in(self, web) -> None:
        track_id = ids_by_title(web.client)["Neon Rain"]
        result = web.client.post("/api/plays", json={"track_id": track_id, "ms_played": 45_000}).json()
        assert result["plays"] == 1
        with web.ctx.db() as conn:
            row = conn.execute("SELECT * FROM play_history").fetchone()
        assert (row["source"], row["raw_artist_name"], row["raw_track_name"], row["raw_album_name"]) == (
            "future_scrobble", "Mara Quinn", "Neon Rain", "Static Hearts")
        assert row["ms_played"] == 45_000 and row["listenbrainz_submitted"] == 0

    def test_play_counts_accumulate_and_show_in_listings(self, web) -> None:
        track_id = ids_by_title(web.client)["Neon Rain"]
        for _ in range(3):
            web.client.post("/api/plays", json={"track_id": track_id, "ms_played": 30_000})
        listing = {t["title"]: t for t in web.client.get("/api/tracks").json()["items"]}
        assert listing["Neon Rain"]["plays"] == 3 and listing["Neon Rain"]["last_played"]
        assert listing["Wires"]["plays"] == 0

    def test_explicit_start_time_is_kept(self, web) -> None:
        track_id = ids_by_title(web.client)["Neon Rain"]
        web.client.post("/api/plays", json={"track_id": track_id, "ms_played": 1000, "started_at": 1_700_000_000})
        with web.ctx.db() as conn:
            assert conn.execute("SELECT played_at_epoch FROM play_history").fetchone()[0] == 1_700_000_000

    def test_unknown_track_and_bad_input_are_rejected(self, web) -> None:
        assert web.client.post("/api/plays", json={"track_id": 424242, "ms_played": 10}).status_code == 404
        assert web.client.post("/api/plays", json={"track_id": 1, "ms_played": -5}).status_code == 422
