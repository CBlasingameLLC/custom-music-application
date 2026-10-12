"""radio.directory: asking the Radio Browser directory, with a stand-in for it."""

from __future__ import annotations

import json

import pytest

from musictoolkit.net import guard
from musictoolkit.radio import directory
from tests.fake_streams import serve_script


def entry(**more):
    base = {
        "stationuuid": "uuid-1", "name": "  Jazz   FM ", "url": "http://old.example/jazz", "url_resolved": "http://stream.example:8000/jazz",
        "homepage": "https://jazz.example/", "favicon": "https://jazz.example/logo.png", "tags": "Jazz, smooth jazz,,", "country": "France",
        "countrycode": "fr", "language": "french", "votes": 12, "codec": "MP3", "bitrate": 128, "hls": 0, "lastcheckok": 1,
    }
    return {**base, **more}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    directory.clear_cache()
    with guard.local_urls_allowed():
        yield
    directory.clear_cache()


def directory_server(routes):
    def script(h):
        path = h.path.split("?")[0]
        body = routes.get(path)
        payload = json.dumps(body(h.path) if callable(body) else body).encode() if body is not None else b""
        h.send_response(200 if body is not None else 404)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(payload)))
        h.end_headers()
        h.wfile.write(payload)

    return serve_script(script)


def test_a_search_asks_the_directory_and_returns_usable_stations(monkeypatch) -> None:
    with directory_server({"/json/stations/search": [entry()]}) as server:
        monkeypatch.setenv("MTK_RADIO_BROWSER_URL", server.url + "/json")
        found = directory.search(name="jazz", tag="smooth jazz", country_code="fr", order="votes", offset=40)
    assert found["more"] is False
    assert found["items"] == [{
        "source": "radio-browser", "source_id": "uuid-1", "name": "Jazz FM", "stream_url": "http://stream.example:8000/jazz",
        "homepage": "https://jazz.example/", "art_url": "https://jazz.example/logo.png", "tags": ["jazz", "smooth jazz"], "country": "France",
        "country_code": "FR", "language": "french", "codec": "MP3", "bitrate": 128, "votes": 12,
    }]
    asked = server.requests[0]
    assert asked["path"].startswith("/json/stations/search?")
    for part in ("name=jazz", "tag=smooth+jazz", "countrycode=fr", "order=votes", "reverse=true", "hidebroken=true", "offset=40", "limit=40"):
        assert part in asked["path"]
    assert asked["headers"]["User-Agent"].startswith("Music Toolkit/")


def test_stations_that_cannot_be_played_are_left_out(monkeypatch) -> None:
    unusable = [
        entry(hls=1), entry(lastcheckok=0), entry(name="   "), entry(url_resolved="", url=""), entry(url_resolved="ftp://x/y", url="ftp://x/y"),
        entry(url_resolved="http://user:pw@host/stream", url=""), "not a station", None,
    ]
    with directory_server({"/json/stations/search": [*unusable, entry(stationuuid="ok", name="Good")]}) as server:
        monkeypatch.setenv("MTK_RADIO_BROWSER_URL", server.url + "/json")
        found = directory.search(name="x")
    assert [s["name"] for s in found["items"]] == ["Good"]


def test_links_that_are_not_web_addresses_are_dropped_from_a_station_not_the_station(monkeypatch) -> None:
    with directory_server({"/json/stations/search": [entry(homepage="javascript:alert(1)", favicon="data:image/png;base64,AAAA", url_resolved="", url="http://ok.example/s")]}) as server:
        monkeypatch.setenv("MTK_RADIO_BROWSER_URL", server.url + "/json")
        station = directory.search(name="x")["items"][0]
    assert station["stream_url"] == "http://ok.example/s" and station["homepage"] is None and station["art_url"] is None


def test_a_full_page_says_there_may_be_more(monkeypatch) -> None:
    page = [entry(stationuuid=f"u{n}", name=f"Station {n}", url_resolved=f"http://s.example/{n}") for n in range(5)]
    with directory_server({"/json/stations/search": page}) as server:
        monkeypatch.setenv("MTK_RADIO_BROWSER_URL", server.url + "/json")
        assert directory.search(name="x", limit=5)["more"] is True
        directory.clear_cache()
        assert directory.search(name="x", limit=6)["more"] is False


def test_the_same_question_is_not_asked_twice_in_a_few_minutes(monkeypatch) -> None:
    with directory_server({"/json/stations/search": [entry()]}) as server:
        monkeypatch.setenv("MTK_RADIO_BROWSER_URL", server.url + "/json")
        directory.search(name="jazz")
        directory.search(name="jazz")
        directory.search(name="rock")
    assert len(server.requests) == 2


def test_tags_and_countries_are_tidied(monkeypatch) -> None:
    tags = [{"name": "Pop", "stationcount": 900}, {"name": "2020", "stationcount": 5}, {"name": "", "stationcount": 3}, {"name": "x" * 50, "stationcount": 2}, {"name": "rock", "stationcount": 700}]
    countries = [{"name": "France", "iso_3166_1": "fr", "stationcount": 800}, {"name": "Nowhere", "iso_3166_1": "", "stationcount": 1}, {"name": "", "iso_3166_1": "DE", "stationcount": 1}]
    with directory_server({"/json/tags": tags, "/json/countries": countries}) as server:
        monkeypatch.setenv("MTK_RADIO_BROWSER_URL", server.url + "/json")
        assert directory.tags() == [{"name": "pop", "count": 900}, {"name": "rock", "count": 700}]
        assert directory.countries() == [{"name": "France", "code": "FR", "count": 800}]


def test_the_directory_being_down_or_confused_is_reported_in_words(monkeypatch) -> None:
    with directory_server({}) as server:
        monkeypatch.setenv("MTK_RADIO_BROWSER_URL", server.url + "/json")
        with pytest.raises(directory.DirectoryError, match="could not be reached"):
            directory.search(name="x")
    with directory_server({"/json/stations/search": {"error": "not a list"}}) as server:
        monkeypatch.setenv("MTK_RADIO_BROWSER_URL", server.url + "/json")
        with pytest.raises(directory.DirectoryError, match="could not be read"):
            directory.search(name="y")


def test_a_play_is_reported_to_the_directory_without_ever_failing(monkeypatch) -> None:
    uuid = "960e57c5-0601-11e8-ae97-52543be04c81"
    with directory_server({f"/json/url/{uuid}": {"ok": True}}) as server:
        monkeypatch.setenv("MTK_RADIO_BROWSER_URL", server.url + "/json")
        directory.click(uuid)
        for not_an_id in (None, "", "x" * 100, "../../etc", "uuid-1", uuid + "/../x"):
            directory.click(not_an_id)
    assert [r["path"] for r in server.requests] == [f"/json/url/{uuid}"], "only a real station id is passed on"
    monkeypatch.setenv("MTK_RADIO_BROWSER_URL", "http://127.0.0.1:9/json")
    directory.click(uuid)  # nobody there: nothing is raised
