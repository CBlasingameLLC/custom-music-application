"""Sending the in-app player's plays to ListenBrainz: when it may, in what order, and what it does when things go wrong."""

from __future__ import annotations

import time

import pytest

from musictoolkit.integrations.listenbrainz_client import ListenBrainzError
from musictoolkit.web import scrobbler as scrobbler_module
from musictoolkit.web.scrobbler import BATCH, FIRST_BACKOFF, IDLE_POLL, MAX_BACKOFF, Scrobbler
from tests.conftest import ids_by_title

NOW = 1_800_000_000.0


class FakeListenBrainz:
    """Stands in for the ListenBrainz client: records what it is given, and fails on request."""

    def __init__(self) -> None:
        self.batches: list[list[dict]] = []
        self.now_playing: list[dict] = []
        self.failures: list[ListenBrainzError] = []  # raised one per call, oldest first
        self.refuse_title: str | None = None  # answer 400 to any batch holding this track

    def submit_listens(self, token: str, listens: list[dict], listen_type: str = "import") -> None:
        assert token and listen_type == "import"
        if self.failures:
            raise self.failures.pop(0)
        if self.refuse_title and any(l["track_metadata"]["track_name"] == self.refuse_title for l in listens):
            raise ListenBrainzError("one of these listens is invalid", status=400)
        self.batches.append(listens)

    def playing_now(self, token: str, track_metadata: dict) -> None:
        if self.failures:
            raise self.failures.pop(0)
        self.now_playing.append(track_metadata)


@pytest.fixture
def fake() -> FakeListenBrainz:
    return FakeListenBrainz()


@pytest.fixture
def sender(web, fake):
    """A scrobbler wired to the fake client and a clock the test controls, with a token saved."""
    web.ctx.config.listenbrainz.user_token = "token-1"
    clock = {"now": NOW}
    web.ctx.scrobbler = Scrobbler(web.ctx, client=fake, clock=lambda: clock["now"])
    web.clock = clock
    return web.ctx.scrobbler


def play(web, title: str, at: float | None = None) -> int:
    ids = ids_by_title(web.client)
    body = {"track_id": ids[title], "ms_played": 200_000}
    if at is not None:
        body["started_at"] = at
    response = web.client.post("/api/plays", json=body)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def flags(web) -> dict[int, int]:
    with web.ctx.db() as conn:
        return {r["id"]: r["listenbrainz_submitted"] for r in conn.execute("SELECT id, listenbrainz_submitted FROM play_history")}


def queue_many(web, count: int) -> None:
    with web.ctx.db() as conn:
        conn.executemany(
            "INSERT INTO play_history (track_id, source, played_at_epoch, ms_played, raw_artist_name, raw_track_name, raw_album_name) "
            "VALUES (NULL, 'future_scrobble', ?, 1000, 'Some Artist', ?, 'Some Album')",
            [(1_700_000_000 + i, f"Song {i}") for i in range(count)],
        )
        conn.commit()


# ------------------------------------------------------------------------------------------- when it may send


@pytest.mark.parametrize("setting", ["user_token", "scrobble", "enabled"])
def test_nothing_is_sent_unless_there_is_a_token_and_scrobbling_is_on(web, sender, fake, setting) -> None:
    play(web, "Glacier")
    cfg = web.ctx.config.listenbrainz
    setattr(cfg, setting, "" if setting == "user_token" else False)

    assert sender.run_once() == IDLE_POLL

    assert fake.batches == []
    status = sender.status()
    assert status["state"] == "off" and status["active"] is False and status["pending"] == 1


def test_plays_go_out_oldest_first_with_their_names_and_are_marked_sent(web, sender, fake) -> None:
    play(web, "Drift", at=NOW - 300)
    play(web, "Glacier", at=NOW - 900)
    play(web, "Wires", at=NOW - 600)
    assert sender.status()["pending"] == 3

    assert sender.run_once() == IDLE_POLL

    (batch,) = fake.batches
    assert [l["track_metadata"]["track_name"] for l in batch] == ["Glacier", "Wires", "Drift"]
    assert [l["listened_at"] for l in batch] == [int(NOW - 900), int(NOW - 600), int(NOW - 300)]
    first = batch[0]["track_metadata"]
    assert first["artist_name"] == "Aurora Vale" and first["release_name"] == "Northern Lights"
    assert first["additional_info"]["submission_client"] == "Music Toolkit"
    assert set(flags(web).values()) == {1}
    status = sender.status()
    assert (status["state"], status["pending"], status["sent"], status["last_error"]) == ("idle", 0, 3, None)
    assert status["last_sent_at"] == NOW

    sender.run_once()
    assert len(fake.batches) == 1, "nothing is sent twice"


def test_a_long_queue_goes_out_in_batches_without_waiting_between_them(web, sender, fake) -> None:
    queue_many(web, BATCH * 2 + 20)

    waits = [sender.run_once() for _ in range(3)]

    assert [len(b) for b in fake.batches] == [BATCH, BATCH, 20]
    assert waits == [0.0, 0.0, IDLE_POLL]
    assert sender.status()["pending"] == 0


def test_only_named_plays_from_the_app_are_sent(web, sender, fake) -> None:
    play(web, "Glacier")
    with web.ctx.db() as conn:
        conn.execute(
            "INSERT INTO play_history (track_id, source, played_at_epoch, ms_played, raw_artist_name, raw_track_name) "
            "VALUES (NULL, 'spotify_import', 1700000000, 1000, 'Old Artist', 'Old Song')"
        )
        conn.execute(
            "INSERT INTO play_history (track_id, source, played_at_epoch, ms_played, raw_artist_name, raw_track_name) "
            "VALUES (NULL, 'future_scrobble', 1700000001, 1000, NULL, 'Nameless')"
        )
        conn.commit()

    assert sender.status()["pending"] == 1
    sender.run_once()

    (batch,) = fake.batches
    assert [l["track_metadata"]["track_name"] for l in batch] == ["Glacier"]
    with web.ctx.db() as conn:
        rows = {r["raw_track_name"]: r["listenbrainz_submitted"] for r in conn.execute("SELECT raw_track_name, listenbrainz_submitted FROM play_history")}
    assert rows["Old Song"] == 0 and rows["Nameless"] == 0, "imported history and unnamed plays are not the scrobbler's to send"


# ------------------------------------------------------------------------------------------- when things go wrong


def test_trouble_keeps_the_plays_and_backs_off_with_growing_pauses(web, sender, fake) -> None:
    play(web, "Glacier")
    fake.failures = [ListenBrainzError("no answer", status=None)] * 8

    waits = [sender.run_once() for _ in range(7)]

    assert waits == [FIRST_BACKOFF * 2**i for i in range(5)] + [MAX_BACKOFF, MAX_BACKOFF]
    status = sender.status()
    assert status["state"] == "waiting" and status["pending"] == 1 and "Can't reach ListenBrainz" in status["last_error"]
    assert status["next_attempt_at"] == NOW + MAX_BACKOFF

    fake.failures.clear()
    assert sender.run_once() == IDLE_POLL
    assert sender.status()["state"] == "idle" and sender.status()["last_error"] is None and sender.status()["pending"] == 0
    play(web, "Drift")
    fake.failures = [ListenBrainzError("down", status=503)]
    assert sender.run_once() == FIRST_BACKOFF, "after a success the pauses start over"
    assert "error 503" in sender.status()["last_error"]


def test_a_request_to_slow_down_is_honoured(web, sender, fake) -> None:
    play(web, "Glacier")
    fake.failures = [ListenBrainzError("slow down", status=429, retry_after=200)]
    assert sender.run_once() == 200
    assert "slow down" in sender.status()["last_error"]


def test_a_rejected_token_stops_everything_until_it_changes_or_the_person_tries_again(web, sender, fake) -> None:
    play(web, "Glacier")
    fake.failures = [ListenBrainzError("unauthorized", status=401)]

    assert sender.run_once() == IDLE_POLL
    status = sender.status()
    assert status["state"] == "rejected" and "token" in status["last_error"] and status["pending"] == 1

    assert sender.run_once() == IDLE_POLL and fake.batches == [] and sender.status()["state"] == "rejected"

    web.ctx.config.listenbrainz.user_token = "token-2"  # a new token deserves a try
    sender.run_once()
    assert len(fake.batches) == 1 and sender.status()["state"] == "idle"

    play(web, "Drift")
    fake.failures = [ListenBrainzError("unauthorized", status=401)]
    sender.run_once()
    assert sender.status()["state"] == "rejected"
    sender.reset()  # "Try again", same token
    sender.run_once()
    assert len(fake.batches) == 2 and sender.status()["state"] == "idle"


def test_one_refused_play_does_not_hold_up_the_others(web, sender, fake) -> None:
    for i, title in enumerate(["Glacier", "Drift", "Wires", "Polar Night", "Gravel Dust"]):
        play(web, title, at=NOW - 1000 + i)
    fake.refuse_title = "Wires"

    sender.run_once()

    sent = [l["track_metadata"]["track_name"] for batch in fake.batches for l in batch]
    assert sorted(sent) == ["Drift", "Glacier", "Gravel Dust", "Polar Night"]
    with web.ctx.db() as conn:
        marks = {r["raw_track_name"]: r["listenbrainz_submitted"] for r in conn.execute("SELECT raw_track_name, listenbrainz_submitted FROM play_history")}
    assert marks["Wires"] == 2 and {v for k, v in marks.items() if k != "Wires"} == {1}
    status = sender.status()
    assert status["pending"] == 0 and status["sent"] == 4 and (status["refused"], status["last_refused"]) == (1, "Wires")
    assert sender.run_once() == IDLE_POLL and len(fake.batches) >= 2, "the bad play is never offered again"


# ------------------------------------------------------------------------------------------- now playing


def test_now_playing_is_quiet_unless_switched_on_and_never_raises(web, sender, fake) -> None:
    track = {"id": 1, "artist": "Aurora Vale", "title": "Glacier", "album": "Northern Lights"}
    assert sender.now_playing(track) is False and fake.now_playing == []

    web.ctx.config.listenbrainz.now_playing = True
    assert sender.now_playing(track) is True
    assert fake.now_playing[0]["track_name"] == "Glacier" and fake.now_playing[0]["release_name"] == "Northern Lights"

    assert sender.now_playing(track) is False, "the same song again within seconds is not repeated"
    web.clock["now"] += 11
    assert sender.now_playing(track) is True
    assert sender.now_playing({"id": 2, "artist": "A", "title": "B", "album": None}) is True, "a different song goes straight out"

    fake.failures = [ListenBrainzError("down", status=500)]
    assert sender.now_playing({"id": 3, "artist": "A", "title": "C", "album": None}) is False
    fake.failures = [ListenBrainzError("unauthorized", status=401)]
    assert sender.now_playing({"id": 4, "artist": "A", "title": "D", "album": None}) is False
    assert sender.status()["state"] == "rejected"
    assert sender.now_playing({"id": 5, "artist": "A", "title": "E", "album": None}) is False, "a rejected token is not tried again"


# ------------------------------------------------------------------------------------------- the thread and the API


def test_the_thread_sends_a_new_play_straight_away_and_stops_cleanly(web, fake, monkeypatch) -> None:
    web.ctx.config.listenbrainz.user_token = "token-1"
    monkeypatch.setattr(scrobbler_module, "IDLE_POLL", 30.0)  # far longer than the test: only a wake-up can explain a quick send
    thread = Scrobbler(web.ctx, client=fake)
    web.ctx.scrobbler = thread
    thread.start()
    try:
        time.sleep(0.3)  # let it find the queue empty and go to sleep
        play(web, "Glacier")  # the endpoint wakes the scrobbler
        deadline = time.time() + 5
        while not fake.batches and time.time() < deadline:
            time.sleep(0.05)
        assert [l["track_metadata"]["track_name"] for l in fake.batches[0]] == ["Glacier"]
    finally:
        thread.stop()
    assert thread._thread is None


def test_the_status_endpoint_never_shows_the_token_and_retry_clears_a_rejection(web, sender, fake) -> None:
    play(web, "Glacier")
    fake.failures = [ListenBrainzError("unauthorized", status=401)]
    sender.run_once()

    status = web.client.get("/api/scrobbler").json()

    assert status["state"] == "rejected" and status["active"] is True and status["has_token"] is True and status["pending"] == 1
    assert "token-1" not in str(status)
    after = web.client.post("/api/scrobbler/retry").json()
    sender.run_once()
    assert len(fake.batches) == 1 and after["has_token"] is True


def test_saving_the_listenbrainz_settings_lets_a_rejected_token_be_tried_again(web, sender, fake) -> None:
    play(web, "Glacier")
    fake.failures = [ListenBrainzError("unauthorized", status=401)]
    sender.run_once()
    assert sender.status()["state"] == "rejected"

    response = web.client.put("/api/settings", json={"listenbrainz": {"user_token": "token-1", "now_playing": True}})

    assert response.status_code == 200 and response.json()["listenbrainz"]["now_playing"] is True
    assert "user_token" not in response.json()["listenbrainz"], "secrets are never echoed"
    sender.run_once()
    assert len(fake.batches) == 1


def test_the_now_playing_endpoint_reports_whether_anything_was_sent(web, sender, fake) -> None:
    ids = ids_by_title(web.client)
    assert web.client.post("/api/now-playing", json={"track_id": ids["Glacier"]}).json() == {"sent": False}

    web.ctx.config.listenbrainz.now_playing = True
    assert web.client.post("/api/now-playing", json={"track_id": ids["Glacier"]}).json() == {"sent": True}
    assert fake.now_playing[0]["artist_name"] == "Aurora Vale"
    assert web.client.post("/api/now-playing", json={"track_id": 999_999}).json() == {"sent": False}
