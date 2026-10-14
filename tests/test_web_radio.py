"""/api/radio: stations kept, found in the directory and played through the app, against fake stations on this computer."""

from __future__ import annotations

import io
import json
import time

import pytest
from PIL import Image

from musictoolkit.net import guard
from musictoolkit.radio import directory
from musictoolkit.radio.hub import RadioHub
from tests.fake_streams import radio_script, serve_script

AUDIO = bytes(range(256)) * 80  # 20,480 bytes standing in for a stream (it is passed on, never decoded)


@pytest.fixture(autouse=True)
def fake_stations_are_on_this_computer():
    directory.clear_cache()
    with guard.local_urls_allowed():
        yield
    directory.clear_cache()


def respond(h, body: bytes, status: int = 200, content_type: str = "text/html") -> None:
    h.send_response(status)
    h.send_header("Content-Type", content_type)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def keep(web, url: str, name: str = "Test FM", **more) -> dict:
    response = web.client.post("/api/radio/stations/save", json={"name": name, "stream_url": url, "source": "manual", **more})
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------- keeping a station by its address

def test_a_station_is_kept_from_its_address_and_named_by_what_it_says_about_itself(web) -> None:
    with serve_script(radio_script(AUDIO, name="Smooth Jazz 24")) as radio:
        station = web.client.post("/api/radio/stations", json={"url": radio.url + "/stream"}).json()
    assert station["name"] == "Smooth Jazz 24" and station["codec"] == "MP3" and station["bitrate"] == 128 and station["favorite"] is True
    assert station["tags"] == ["jazz", "smooth"] and station["stream_url"] == radio.url + "/stream"
    listing = web.client.get("/api/radio/stations").json()
    assert [s["name"] for s in listing["items"]] == ["Smooth Jazz 24"] and listing["counts"] == {"all": 1, "favorites": 1, "recent": 0}


def test_a_name_typed_in_wins_and_the_same_address_is_not_kept_twice(web) -> None:
    with serve_script(radio_script(AUDIO)) as radio:
        first = web.client.post("/api/radio/stations", json={"url": radio.url + "/s", "name": "  My   jazz "}).json()
        second = web.client.post("/api/radio/stations", json={"url": radio.url + "/s"}).json()
    assert first["name"] == "My jazz" and second["id"] == first["id"] and len(web.client.get("/api/radio/stations").json()["items"]) == 1


def test_a_playlist_file_is_followed_to_the_stream_inside_it(web) -> None:
    with serve_script(radio_script(AUDIO)) as radio:
        def front(h):
            if h.path == "/listen.pls":
                respond(h, f"[playlist]\nNumberOfEntries=1\nFile1={radio.url}/real\n".encode(), content_type="audio/x-scpls")
            else:
                radio_script(AUDIO)(h)

        with serve_script(front) as server:
            station = web.client.post("/api/radio/stations", json={"url": server.url + "/listen.pls"}).json()
            assert station["stream_url"] == server.url + "/listen.pls", "the playlist's address is the one kept, so a station that moves its stream still works"
            played = web.client.get(f"/api/radio/stations/{station['id']}/stream")
    assert played.status_code == 200 and played.content == AUDIO[:2000]


@pytest.mark.parametrize(
    "script, message",
    [
        (lambda h: respond(h, b"<html>status page</html>"), "not an audio stream"),
        (lambda h: respond(h, b"gone", 404, "text/plain"), "not at that address any more"),
        (lambda h: respond(h, b"busy", 503, "text/plain"), "error 503"),
        (lambda h: respond(h, b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:10,\nseg1.ts\n", content_type="application/vnd.apple.mpegurl"), "HLS"),
        (lambda h: respond(h, b"#EXTM3U\n# nothing\n", content_type="audio/x-mpegurl"), "does not hold an address"),
    ],
)
def test_an_address_that_does_not_play_is_not_kept_and_the_reason_is_given(web, script, message) -> None:
    with serve_script(script) as server:
        response = web.client.post("/api/radio/stations", json={"url": server.url + "/x"})
    assert response.status_code == 422 and message in response.json()["detail"]
    assert web.client.get("/api/radio/stations").json()["items"] == []


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)", "http://user:pw@example.com/", "not an address", "http://"])
def test_addresses_that_are_not_web_addresses_are_refused(web, url) -> None:
    assert web.client.post("/api/radio/stations", json={"url": url}).status_code == 422


def test_a_station_on_this_computer_or_its_network_is_refused(web) -> None:
    with serve_script(radio_script(AUDIO)) as radio:
        with guard.local_urls_allowed():
            pass
        guard.allow_local(False)  # the fixture had allowed it; this is the app as it really runs
        response = web.client.post("/api/radio/stations", json={"url": radio.url + "/stream"})
        guard.allow_local(True)
    assert response.status_code == 422 and "not on the internet" in response.json()["detail"]
    assert radio.requests == []


# ---------------------------------------------------------------- listening

def test_the_audio_is_passed_on_without_the_titles_and_the_titles_are_remembered(web) -> None:
    with serve_script(radio_script(AUDIO, metaint=1000, titles=["Miles Davis - So What", "Nina Simone - Feeling Good"])) as radio:
        station = keep(web, radio.url + "/stream")
        heard: list = []
        real = web.ctx.radio.heard
        web.ctx.radio.heard = lambda sid, title: (heard.append((sid, title)), real(sid, title))[1]
        response = web.client.get(f"/api/radio/stations/{station['id']}/stream")
    assert response.status_code == 200 and response.headers["content-type"] == "audio/mpeg"
    assert response.content == AUDIO[:2000], "exactly the audio, with no title bytes in it"
    assert heard and heard[-1][1] == "Nina Simone - Feeling Good", "the latest title is the one kept"
    assert radio.requests[0]["headers"]["Icy-MetaData"] == "1"
    assert web.ctx.radio.slots.used == 0, "the stream is given back when it ends"


def test_a_station_that_sends_no_titles_is_passed_on_as_it_is(web) -> None:
    def plain(h):
        h.send_response(200)
        h.send_header("Content-Type", "audio/aacp")
        h.end_headers()
        h.wfile.write(AUDIO)

    with serve_script(plain) as radio:
        station = keep(web, radio.url + "/stream")
        response = web.client.get(f"/api/radio/stations/{station['id']}/stream")
    assert response.content == AUDIO and response.headers["content-type"].startswith("audio/")


def test_a_shoutcast_version_1_server_that_says_icy_instead_of_http_is_understood(web) -> None:
    def shoutcast(h):
        h.wfile.write(b"ICY 200 OK\r\nicy-name:Old School\r\ncontent-type:audio/mpeg\r\n\r\n" + AUDIO[:3000])

    with serve_script(shoutcast) as radio:
        station = keep(web, radio.url + "/stream")
        response = web.client.get(f"/api/radio/stations/{station['id']}/stream")
    assert response.status_code == 200 and response.content == AUDIO[:3000]


def test_the_titles_are_told_to_the_page_split_into_artist_and_track(web) -> None:
    station = keep(web, "http://stream.example/x")
    assert web.client.get(f"/api/radio/stations/{station['id']}/now").json() == {"title": None, "artist": None, "track": None, "since": None}
    web.ctx.radio.heard(station["id"], "Miles Davis - So What")
    now = web.client.get(f"/api/radio/stations/{station['id']}/now").json()
    assert (now["title"], now["artist"], now["track"]) == ("Miles Davis - So What", "Miles Davis", "So What") and now["since"]
    web.ctx.radio.heard(station["id"], None)
    assert web.client.get(f"/api/radio/stations/{station['id']}/now").json()["title"] is None


def test_a_stream_that_fails_is_a_bad_gateway_with_the_reason_and_gives_its_place_back(web) -> None:
    with serve_script(lambda h: respond(h, b"<html>", 200, "text/html")) as radio:
        station = keep(web, radio.url + "/stream")
        response = web.client.get(f"/api/radio/stations/{station['id']}/stream")
    assert response.status_code == 502 and "not an audio stream" in response.json()["detail"] and web.ctx.radio.slots.used == 0


def test_only_so_many_streams_can_be_open_at_once(web) -> None:
    web.ctx.radio = RadioHub(max_streams=0)
    station = keep(web, "http://stream.example/x")
    assert web.client.get(f"/api/radio/stations/{station['id']}/stream").status_code == 429


def test_an_address_that_has_since_come_to_point_inside_is_never_connected_to(web) -> None:
    """The stored address is checked each time it is used, not only when it was added."""
    with serve_script(radio_script(AUDIO)) as radio:
        station = keep(web, radio.url + "/stream")
        guard.allow_local(False)
        response = web.client.get(f"/api/radio/stations/{station['id']}/stream")
        checked = web.client.post(f"/api/radio/stations/{station['id']}/check").json()
        guard.allow_local(True)
    assert response.status_code == 502 and "not on the internet" in response.json()["detail"]
    assert checked["ok"] is False and "not on the internet" in checked["message"]
    assert radio.requests == []


def test_a_station_is_checked_on_request(web) -> None:
    with serve_script(radio_script(AUDIO)) as radio:
        station = keep(web, radio.url + "/stream")
        assert web.client.post(f"/api/radio/stations/{station['id']}/check").json() == {"ok": True, "type": "audio/mpeg", "metadata": True}
    with serve_script(lambda h: respond(h, b"x", 500, "text/plain")) as broken:
        station = keep(web, broken.url + "/stream", name="Broken")
        checked = web.client.post(f"/api/radio/stations/{station['id']}/check").json()
    assert checked["ok"] is False and "error 500" in checked["message"]


def test_unknown_stations_are_not_found(web) -> None:
    for path in ("stream", "now", "art"):
        assert web.client.get(f"/api/radio/stations/999/{path}").status_code == 404, path
    for path in ("check", "played"):
        assert web.client.post(f"/api/radio/stations/999/{path}").status_code == 404, path
    assert web.client.patch("/api/radio/stations/999", json={"favorite": True}).status_code == 404
    assert web.client.delete("/api/radio/stations/999").status_code == 404


# ---------------------------------------------------------------- the kept list

def test_stations_can_be_marked_renamed_played_and_removed(web) -> None:
    with serve_script(lambda h: respond(h, b"{}", content_type="application/json")) as directory_server:
        a = keep(web, "http://stream.example/a", name="Alpha", favorite=False)
        b = keep(web, "http://stream.example/b", name="Beta", favorite=False)
        assert web.client.patch(f"/api/radio/stations/{a['id']}", json={"favorite": True}).json()["favorite"] is True
        assert web.client.patch(f"/api/radio/stations/{b['id']}", json={"name": "  Bravo "}).json()["name"] == "Bravo"
        web.client.post(f"/api/radio/stations/{b['id']}/played")
        listing = web.client.get("/api/radio/stations").json()
        assert [s["name"] for s in listing["items"]] == ["Alpha", "Bravo"] and listing["counts"] == {"all": 2, "favorites": 1, "recent": 1}
        assert [s["name"] for s in web.client.get("/api/radio/stations", params={"scope": "recent"}).json()["items"]] == ["Bravo"]
        assert [s["name"] for s in web.client.get("/api/radio/stations", params={"scope": "favorites"}).json()["items"]] == ["Alpha"]
        assert web.client.delete(f"/api/radio/stations/{a['id']}").json() == {"ok": True}
    assert [s["name"] for s in web.client.get("/api/radio/stations").json()["items"]] == ["Bravo"]
    assert directory_server.requests == [], "a station that did not come from the directory is not reported to it"


def test_saving_from_the_directory_cleans_what_it_is_given(web) -> None:
    saved = web.client.post(
        "/api/radio/stations/save",
        json={"name": " Jazz   FM ", "stream_url": "http://stream.example/jazz", "source": "radio-browser", "source_id": "abc-123", "homepage": "javascript:alert(1)",
              "art_url": "https://jazz.example/logo.png", "tags": ["Jazz", "jazz", "Cool"], "country_code": "fr", "bitrate": 128, "favorite": True},
    ).json()
    assert saved["name"] == "Jazz FM" and saved["homepage"] is None and saved["has_art"] is True and saved["tags"] == ["jazz", "cool"] and saved["country_code"] == "FR"
    for bad in ({"stream_url": "ftp://x/y"}, {"stream_url": "http://u:p@h/x"}, {"name": ""}, {"bitrate": -5}, {"source": "other"}):
        assert web.client.post("/api/radio/stations/save", json={"name": "N", "stream_url": "http://s.example/x", **bad}).status_code in (422,)


def test_a_play_of_a_directory_station_is_counted_by_the_directory_too(web, monkeypatch) -> None:
    uuid = "960e57c5-0601-11e8-ae97-52543be04c81"
    with serve_script(lambda h: respond(h, b"{}", content_type="application/json")) as directory_server:
        monkeypatch.setenv("MTK_RADIO_BROWSER_URL", directory_server.url + "/json")
        station = keep(web, "http://stream.example/dir", source="radio-browser", source_id=uuid)
        web.client.post(f"/api/radio/stations/{station['id']}/played")
        deadline = time.monotonic() + 5
        while not directory_server.requests and time.monotonic() < deadline:
            time.sleep(0.02)
    assert [r["path"] for r in directory_server.requests] == [f"/json/url/{uuid}"]


# ---------------------------------------------------------------- the directory

def entries():
    return [
        {"stationuuid": "u1", "name": "Jazz FM", "url_resolved": "http://stream.example/jazz", "tags": "jazz", "countrycode": "FR", "codec": "MP3", "bitrate": 128, "hls": 0, "lastcheckok": 1},
        {"stationuuid": "u2", "name": "Rock FM", "url_resolved": "http://stream.example/rock", "tags": "rock", "countrycode": "DE", "codec": "AAC", "bitrate": 64, "hls": 0, "lastcheckok": 1},
    ]


def directory_answers(h):
    path = h.path.split("?")[0]
    body = {"/json/stations/search": entries(), "/json/tags": [{"name": "jazz", "stationcount": 9}], "/json/countries": [{"name": "France", "iso_3166_1": "FR", "stationcount": 4}]}.get(path)
    respond(h, json.dumps(body).encode(), 200 if body is not None else 404, "application/json")


def test_the_directory_is_searched_and_says_which_stations_are_already_kept(web, monkeypatch) -> None:
    with serve_script(directory_answers) as server:
        monkeypatch.setenv("MTK_RADIO_BROWSER_URL", server.url + "/json")
        keep(web, "http://stream.example/jazz", name="Jazz FM", favorite=True)
        found = web.client.get("/api/radio/directory", params={"q": "fm", "tag": "jazz", "country": "FR", "order": "votes"}).json()
        assert [(s["name"], s["stored_id"] is not None, s["favorite"]) for s in found["items"]] == [("Jazz FM", True, True), ("Rock FM", False, False)]
        assert found["more"] is False
        assert web.client.get("/api/radio/directory/tags").json() == {"items": [{"name": "jazz", "count": 9}]}
        assert web.client.get("/api/radio/directory/countries").json() == {"items": [{"name": "France", "code": "FR", "count": 4}]}
    assert "tag=jazz" in server.requests[0]["path"] and "countrycode=FR" in server.requests[0]["path"] and "order=votes" in server.requests[0]["path"]


def test_a_directory_that_is_down_is_a_bad_gateway_in_words(web, monkeypatch) -> None:
    with serve_script(lambda h: respond(h, b"", 500, "text/plain")) as server:
        monkeypatch.setenv("MTK_RADIO_BROWSER_URL", server.url + "/json")
        for path in ("/api/radio/directory", "/api/radio/directory/tags", "/api/radio/directory/countries"):
            response = web.client.get(path)
            assert response.status_code == 502 and "could not be reached" in response.json()["detail"], path
    assert web.client.get("/api/radio/directory", params={"order": "sideways"}).status_code == 422


# ---------------------------------------------------------------- pictures

def picture(fmt: str = "PNG", size=(600, 400)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(out, fmt)
    return out.getvalue()


def test_a_stations_picture_is_fetched_once_redrawn_small_and_kept(web) -> None:
    with serve_script(lambda h: respond(h, picture(), content_type="image/png")) as art:
        station = keep(web, "http://stream.example/x", art_url=art.url + "/logo.png")
        assert station["has_art"] is True
        first = web.client.get(f"/api/radio/stations/{station['id']}/art")
        second = web.client.get(f"/api/radio/stations/{station['id']}/art")
    assert first.status_code == 200 and first.headers["content-type"] == "image/jpeg" and first.content == second.content
    with Image.open(io.BytesIO(first.content)) as image:
        assert image.format == "JPEG" and max(image.size) == 256
    assert len(art.requests) == 1, "fetched once"


@pytest.mark.parametrize(
    "answer",
    [
        lambda h: respond(h, b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", content_type="image/svg+xml"),
        lambda h: respond(h, b"not a picture at all", content_type="image/png"),
        lambda h: respond(h, b"", 404, "text/plain"),
        lambda h: respond(h, b"x" * 3_000_000, content_type="image/png"),
    ],
)
def test_a_picture_that_is_not_one_is_never_passed_on_and_is_not_asked_for_again_soon(web, answer) -> None:
    with serve_script(answer) as art:
        station = keep(web, "http://stream.example/x", art_url=art.url + "/logo")
        assert web.client.get(f"/api/radio/stations/{station['id']}/art").status_code == 404
        assert web.client.get(f"/api/radio/stations/{station['id']}/art").status_code == 404
    assert len(art.requests) == 1


def test_a_station_without_a_picture_has_none(web) -> None:
    station = keep(web, "http://stream.example/x")
    assert station["has_art"] is False and web.client.get(f"/api/radio/stations/{station['id']}/art").status_code == 404
