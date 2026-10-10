"""The release radar through the API: the list, dismissing, looking now, and looking by itself once a day."""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from musictoolkit.integrations import listenbrainz_client, musicbrainz_client
from musictoolkit.recommend import radar
from musictoolkit.web.radar_job import RadarSchedule, configured
from tests.fake_services import serve

A, B, C = ("aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002", "cccccccc-0000-4000-8000-000000000003")
ARTIST = "a74b1b7f-71a5-4011-9441-d0b5e4122711"


def entry(mbid, artist, title, day, kind="Album", caa=True):
    return {"artist_credit_name": artist, "artist_mbids": [ARTIST], "caa_id": 4614923431 if caa else None, "caa_release_mbid": mbid if caa else None,
            "release_date": day.isoformat(), "release_group_mbid": mbid, "release_group_primary_type": kind, "release_group_secondary_type": None,
            "release_mbid": mbid, "release_name": title}


@pytest.fixture
def listenbrainz(monkeypatch):
    """ListenBrainz on this computer: three releases, one of them still to come."""
    today = date.today()
    answer = {"payload": {"releases": [
        entry(A, "Tycho", "Infinite Mile", today - timedelta(days=30)),
        entry(B, "Bonobo", "Fragments", today - timedelta(days=3), kind="EP", caa=False),
        entry(C, "Emancipator", "Koda", today + timedelta(days=12), kind="Single"),
    ]}}
    with serve({"/user/cayl/fresh_releases": (200, answer)}) as service:
        monkeypatch.setattr(listenbrainz_client, "_BASE_URL", service.url)
        yield service


def set_up(web, **settings) -> None:
    assert web.client.put("/api/settings", json=settings).status_code == 200


def look(web) -> dict:
    response = web.client.post("/api/releases/refresh")
    assert response.status_code == 200, response.text
    return web.ctx.jobs.wait(response.json()["job"]["id"], timeout=30)


# ------------------------------------------------------------------------------------------- the list


def test_before_anything_is_found_the_list_is_empty_and_says_what_could_be_used(web) -> None:
    body = web.client.get("/api/releases").json()

    assert body == {"items": [], "counts": {"new": 0, "dismissed": 0}, "state": {"last_run": None, "source": None, "message": None},
                    "enabled": False, "sources": {"listenbrainz": False, "musicbrainz": False}, "running": False}


def test_a_look_finds_the_releases_newest_first_and_marks_the_one_still_to_come(web, listenbrainz) -> None:
    set_up(web, listenbrainz={"username": "cayl"})

    job = look(web)

    assert job.status == "done" and job.result["new"] == 3 and job.result["source"] == "listenbrainz"
    body = web.client.get("/api/releases").json()
    assert [(i["artist"], i["title"], i["upcoming"]) for i in body["items"]] == [("Emancipator", "Koda", True), ("Bonobo", "Fragments", False), ("Tycho", "Infinite Mile", False)]
    assert body["counts"] == {"new": 3, "dismissed": 0} and body["state"]["source"] == "listenbrainz" and body["state"]["last_run"]
    tycho = body["items"][2]
    assert tycho["kind"] == "Album" and tycho["art"] == f"https://coverartarchive.org/release/{A}/4614923431-250.jpg"
    assert [l["label"] for l in tycho["links"]] == ["MusicBrainz", "Bandcamp", "YouTube"] and tycho["links"][0]["url"].endswith(A)
    assert body["items"][1]["art"] is None


def test_the_service_is_asked_for_this_user_over_the_last_two_months(web, listenbrainz) -> None:
    set_up(web, listenbrainz={"username": "cayl", "user_token": "tok-1"})

    look(web)

    request = listenbrainz.requests[0]
    assert request["path"] == "/user/cayl/fresh_releases" and request["query"]["days"] == "60"
    assert request["headers"]["Authorization"] == "Token tok-1"


def test_what_the_library_already_has_is_left_out(web, listenbrainz) -> None:
    set_up(web, listenbrainz={"username": "cayl"})
    with web.ctx.db() as conn:
        conn.execute("INSERT INTO tracks (file_path, artist, album, title) VALUES ('t.mp3', 'Tycho', 'Infinite Mile', 'x')")
        conn.commit()

    job = look(web)

    assert job.result["new"] == 2 and job.result["owned"] == 1
    assert "Tycho" not in [i["artist"] for i in web.client.get("/api/releases").json()["items"]]


def test_a_dismissed_release_leaves_the_list_comes_back_on_request_and_stays_dismissed_after_a_look(web, listenbrainz) -> None:
    set_up(web, listenbrainz={"username": "cayl"})
    look(web)
    release_id = web.client.get("/api/releases").json()["items"][0]["id"]

    assert web.client.post(f"/api/releases/{release_id}/status", json={"status": "dismissed"}).json() == {"ok": True}
    look(web)

    shown = web.client.get("/api/releases").json()
    assert release_id not in [i["id"] for i in shown["items"]] and shown["counts"] == {"new": 2, "dismissed": 1}
    assert [i["id"] for i in web.client.get("/api/releases", params={"status": "dismissed"}).json()["items"]] == [release_id]

    web.client.post(f"/api/releases/{release_id}/status", json={"status": "new"})
    assert release_id in [i["id"] for i in web.client.get("/api/releases").json()["items"]]


def test_unknown_releases_and_statuses_are_refused(web) -> None:
    assert web.client.post("/api/releases/999/status", json={"status": "dismissed"}).status_code == 404
    assert web.client.post("/api/releases/1/status", json={"status": "deleted"}).status_code == 422
    assert web.client.get("/api/releases", params={"status": "all"}).status_code == 422


# ------------------------------------------------------------------------------------------- looking now


def test_with_nowhere_to_look_the_job_ends_in_an_error_that_says_what_to_add(web) -> None:
    job = look(web)

    assert job.status == "error" and "ListenBrainz username" in job.error and "MusicBrainz contact" in job.error
    assert web.client.get("/api/releases").json()["state"]["last_run"] is None


def test_a_look_that_is_already_running_is_not_started_twice(web, listenbrainz) -> None:
    set_up(web, listenbrainz={"username": "cayl"})
    release = threading.Event()
    web.ctx.jobs.submit("radar", "Looking for new releases", lambda handle: release.wait(10) and None, lane="network")

    try:
        assert web.client.post("/api/releases/refresh").status_code == 409
        assert web.client.get("/api/releases").json()["running"] is True
    finally:
        release.set()


def test_the_look_runs_in_the_network_lane_so_it_does_not_wait_behind_a_tag_edit(web, listenbrainz) -> None:
    set_up(web, listenbrainz={"username": "cayl"})
    block = threading.Event()
    web.ctx.jobs.submit("tags", "A long tag edit", lambda handle: block.wait(10) and None)  # the main lane, busy for now

    try:
        job = web.client.post("/api/releases/refresh").json()["job"]
        status = web.ctx.jobs.wait(job["id"], timeout=5).status  # read while the tag edit is still blocking
    finally:
        block.set()

    assert (job["kind"], job["title"], status) == ("radar", "Looking for new releases", "done")


# ------------------------------------------------------------------------------------------- by itself


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def asked(monkeypatch):
    """Replace the look itself, and record when it was asked for."""
    calls = []

    def fake(conn, cfg, **_kwargs):
        calls.append(1)
        radar._remember(conn, "last_run", clock.now.isoformat())
        conn.commit()
        return {"source": "listenbrainz", "found": 0, "new": 0, "known": 0, "owned": 0, "artists": 0, "messages": []}

    clock = Clock(datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(radar, "refresh", fake)
    return type("Asked", (), {"calls": calls, "clock": clock})


def wait_for_jobs(web) -> None:
    for job in list(web.ctx.jobs.active()):
        web.ctx.jobs.wait(job.id, timeout=10)


def test_nothing_happens_by_itself_until_the_switch_is_on(web, asked) -> None:
    set_up(web, listenbrainz={"username": "cayl"})

    assert RadarSchedule(web.ctx, asked.clock).tick() is False and asked.calls == []


def test_the_switch_alone_is_not_enough_without_anywhere_to_look(web, asked) -> None:
    set_up(web, app={"release_radar": True})

    assert RadarSchedule(web.ctx, asked.clock).tick() is False and asked.calls == []
    assert not configured(web.ctx.config)


def test_with_the_switch_on_it_looks_once_then_waits_about_a_day(web, asked) -> None:
    set_up(web, app={"release_radar": True}, musicbrainz={"contact": "me@example.com"})
    schedule = RadarSchedule(web.ctx, asked.clock)

    assert schedule.tick() is True
    wait_for_jobs(web)
    assert asked.calls == [1]

    asked.clock.now += timedelta(hours=19)
    assert schedule.tick() is False

    asked.clock.now += timedelta(hours=2)
    assert schedule.tick() is True
    wait_for_jobs(web)
    assert asked.calls == [1, 1]


def test_it_does_not_start_a_second_look_while_one_is_running(web, asked) -> None:
    set_up(web, app={"release_radar": True}, musicbrainz={"contact": "me@example.com"})
    release = threading.Event()
    web.ctx.jobs.submit("radar", "Looking for new releases", lambda handle: release.wait(10) and None, lane="network")

    try:
        assert RadarSchedule(web.ctx, asked.clock).tick() is False
    finally:
        release.set()


def test_the_schedule_thread_starts_and_stops_promptly(web) -> None:
    schedule = RadarSchedule(web.ctx)

    schedule.start()
    schedule.start()  # starting twice is harmless
    schedule.stop()

    assert schedule._thread is None


def test_the_switch_is_remembered(web) -> None:
    assert web.client.get("/api/settings").json()["app"]["release_radar"] is False

    set_up(web, app={"release_radar": True})

    assert web.client.get("/api/settings").json()["app"]["release_radar"] is True


# ------------------------------------------------------------------------------------------- the clients


def test_the_user_name_cannot_change_the_path_it_is_asked_about(monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(listenbrainz_client, "_send", lambda method, path, token, **kw: sent.append((method, path, kw)) or type("R", (), {"status_code": 200, "json": lambda self: {"payload": {"releases": []}}})())

    listenbrainz_client.get_fresh_releases("a/b?x=1", days=500)
    listenbrainz_client.get_cf_recommendations("../etc", None)

    assert sent[0][1] == "/user/a%2Fb%3Fx%3D1/fresh_releases" and sent[0][2]["params"]["days"] == 90, "and the days are capped at what the service allows"
    assert sent[1][1] == "/cf/recommendation/user/..%2Fetc/recording"


def test_a_refusal_from_listenbrainz_is_an_error_with_its_status(monkeypatch) -> None:
    monkeypatch.setattr(listenbrainz_client, "_send", lambda *a, **k: type("R", (), {"status_code": 404, "text": "no such user", "headers": {}})())

    with pytest.raises(listenbrainz_client.ListenBrainzError) as caught:
        listenbrainz_client.get_fresh_releases("nobody")

    assert caught.value.status == 404 and "no such user" in str(caught.value)


def test_a_release_group_search_needs_the_app_to_have_introduced_itself_first(monkeypatch) -> None:
    monkeypatch.setattr(musicbrainz_client, "_configured", False)

    with pytest.raises(RuntimeError, match="configure"):
        musicbrainz_client.search_release_groups("artist:x")


def test_a_release_group_search_returns_the_list_from_musicbrainz(monkeypatch) -> None:
    monkeypatch.setattr(musicbrainz_client, "_configured", True)
    seen = {}

    def fake(query, limit):
        seen.update(query=query, limit=limit)
        return {"release-group-list": [{"id": A}], "release-group-count": 1}

    monkeypatch.setattr(musicbrainz_client.musicbrainzngs, "search_release_groups", fake)

    assert musicbrainz_client.search_release_groups('artist:"Tycho"', limit=7) == [{"id": A}]
    assert seen == {"query": 'artist:"Tycho"', "limit": 7}
