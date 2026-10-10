"""The two small reachability checks behind the Test connection buttons."""

from __future__ import annotations

import pytest
import requests

from musictoolkit.integrations import lastfm_client, musicbrainz_client


class Answer:
    def __init__(self, status: int = 200, data=None, bad_json: bool = False) -> None:
        self.status_code = status
        self._data = data
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._data


@pytest.fixture
def get(monkeypatch):
    state = type("Get", (), {})()
    state.calls = []
    state.answer = Answer(200, {})
    state.raises = None

    def fake(url, params=None, headers=None, timeout=None):
        state.calls.append({"url": url, "params": params or {}, "headers": headers or {}, "timeout": timeout})
        if state.raises:
            raise state.raises
        return state.answer

    monkeypatch.setattr(requests, "get", fake)
    return state


def test_lastfm_accepts_a_key_it_answers_normally_for(get) -> None:
    get.answer = Answer(200, {"artists": {"artist": []}})
    assert lastfm_client.check_key("key-1") == (True, "Last.fm accepted the key.")
    assert get.calls[0]["params"]["api_key"] == "key-1" and get.calls[0]["url"].startswith("https://ws.audioscrobbler.com/")


def test_lastfm_relays_why_a_key_was_refused(get) -> None:
    get.answer = Answer(403, {"error": 10, "message": "Invalid API key - You must be granted a valid key by last.fm"})
    ok, message = lastfm_client.check_key("nope")
    assert not ok and "Invalid API key" in message


@pytest.mark.parametrize("answer", [Answer(502, bad_json=True), Answer(500, [])])
def test_lastfm_trouble_is_reported_not_raised(get, answer) -> None:
    get.answer = answer
    ok, message = lastfm_client.check_key("key-1")
    assert not ok and "error" in message


def test_lastfm_offline_is_reported(get) -> None:
    get.raises = requests.ConnectionError("dns")
    ok, message = lastfm_client.check_key("key-1")
    assert not ok and "Could not reach Last.fm" in message


def test_musicbrainz_is_asked_the_way_it_wants_to_be_asked(get) -> None:
    ok, message = musicbrainz_client.ping("custom-music-application", "0.4.0", "me@example.com")
    assert ok and "reachable" in message
    assert get.calls[0]["headers"]["User-Agent"] == "custom-music-application/0.4.0 ( me@example.com )"
    assert get.calls[0]["url"].startswith("https://musicbrainz.org/ws/2/")


@pytest.mark.parametrize(("status", "words"), [(503, "busy"), (429, "busy"), (404, "error 404")])
def test_musicbrainz_trouble_is_reported(get, status, words) -> None:
    get.answer = Answer(status)
    ok, message = musicbrainz_client.ping("app", "1", "me@example.com")
    assert not ok and words in message


def test_musicbrainz_offline_is_reported(get) -> None:
    get.raises = requests.Timeout("slow")
    ok, message = musicbrainz_client.ping("app", "1", "me@example.com")
    assert not ok and "Could not reach MusicBrainz" in message
