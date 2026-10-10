"""The ListenBrainz client: what it sends, and how it reports what went wrong."""

from __future__ import annotations

import pytest
import requests

from musictoolkit.integrations import listenbrainz_client as lb


class Answer:
    def __init__(self, status: int = 200, data: dict | None = None, text: str = "", headers: dict | None = None) -> None:
        self.status_code = status
        self._data = data or {}
        self.text = text
        self.headers = headers or {}

    def json(self) -> dict:
        return self._data


@pytest.fixture
def wire(monkeypatch):
    """Replace the network: `wire.answer` is what comes back, `wire.calls` what was asked."""
    state = type("Wire", (), {})()
    state.calls = []
    state.answer = Answer()
    state.raises = None

    def request(method, url, headers=None, timeout=None, **kwargs):
        state.calls.append({"method": method, "url": url, "headers": headers or {}, "timeout": timeout, **kwargs})
        if state.raises:
            raise state.raises
        return state.answer

    monkeypatch.setattr(lb.requests, "request", request)
    return state


def test_listens_are_posted_with_the_token_and_as_an_import_by_default(wire) -> None:
    listens = [{"listened_at": 1, "track_metadata": {"artist_name": "A", "track_name": "T"}}]

    lb.submit_listens("secret", listens)

    (call,) = wire.calls
    assert call["method"] == "POST" and call["url"].endswith("/1/submit-listens")
    assert call["headers"]["Authorization"] == "Token secret"
    assert call["json"] == {"listen_type": "import", "payload": listens} and call["timeout"] == lb.TIMEOUT


def test_a_batch_over_the_limit_is_a_programming_error(wire) -> None:
    with pytest.raises(ValueError):
        lb.submit_listens("secret", [{}] * (lb.MAX_LISTENS_PER_REQUEST + 1))
    assert wire.calls == []


def test_now_playing_is_a_playing_now_listen_without_a_timestamp(wire) -> None:
    lb.playing_now("secret", {"artist_name": "A", "track_name": "T"})
    assert wire.calls[0]["json"] == {"listen_type": "playing_now", "payload": [{"track_metadata": {"artist_name": "A", "track_name": "T"}}]}


@pytest.mark.parametrize(
    ("status", "token_rejected", "transient"),
    [(400, False, False), (401, True, False), (404, False, False), (429, False, True), (500, False, True), (503, False, True)],
)
def test_a_refusal_says_what_kind_it_was(wire, status, token_rejected, transient) -> None:
    wire.answer = Answer(status, text="nope", headers={"Retry-After": "12"})

    with pytest.raises(lb.ListenBrainzError) as caught:
        lb.submit_listens("secret", [])

    error = caught.value
    assert error.status == status and "nope" in str(error)
    assert (error.token_rejected, error.transient) == (token_rejected, transient)
    assert error.retry_after == 12.0


def test_no_answer_at_all_counts_as_worth_trying_again(wire) -> None:
    wire.raises = requests.ConnectionError("dns")

    with pytest.raises(lb.ListenBrainzError) as caught:
        lb.submit_listens("secret", [])

    assert caught.value.status is None and caught.value.transient and not caught.value.token_rejected
    assert "ConnectionError" in str(caught.value)


def test_a_rate_limit_header_without_retry_after_is_understood(wire) -> None:
    wire.answer = Answer(429, headers={"X-RateLimit-Reset-In": "7"})
    with pytest.raises(lb.ListenBrainzError) as caught:
        lb.submit_listens("secret", [])
    assert caught.value.retry_after == 7.0


def test_a_token_check_reports_the_user_or_that_the_token_is_not_valid(wire) -> None:
    wire.answer = Answer(200, {"valid": True, "user_name": "bob", "message": "Token valid."})
    assert lb.validate_token("secret") == {"valid": True, "user_name": "bob", "message": "Token valid."}
    assert wire.calls[0]["method"] == "GET" and wire.calls[0]["url"].endswith("/1/validate-token")

    wire.answer = Answer(200, {"valid": False, "message": "Token invalid."})
    assert lb.validate_token("secret")["valid"] is False

    wire.answer = Answer(401)
    assert lb.validate_token("secret")["valid"] is False

    wire.answer = Answer(503, text="down")
    with pytest.raises(lb.ListenBrainzError) as caught:
        lb.validate_token("secret")
    assert caught.value.transient


def test_recommendations_come_back_as_the_list_of_recordings(wire) -> None:
    wire.answer = Answer(200, {"payload": {"mbids": [{"recording_mbid": "x", "score": 1.5}]}})
    assert lb.get_cf_recommendations("bob", None, count=5) == [{"recording_mbid": "x", "score": 1.5}]
    assert wire.calls[0]["params"] == {"count": 5, "offset": 0} and "Authorization" not in wire.calls[0]["headers"]

    wire.answer = Answer(404, text="no such user")
    with pytest.raises(lb.ListenBrainzError) as caught:
        lb.get_cf_recommendations("bob", None)
    assert caught.value.status == 404
